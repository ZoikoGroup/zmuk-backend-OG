"""SIM reactivation service.

Called after Stripe confirms payment. Idempotent — safe to call from both
the webhook and the synchronous response without double-reactivating.

Uses the existing apps.sims.transatel client for the actual API call,
but adds the /reactivate endpoint that the activation flow doesn't have.

The /reactivate endpoint is confirmed from the WordPress wc-recharge-topup
plugin (class-wc-recharge-module.php line 181):

    POST /connectivity-management/subscribers/api/subscribers/sim-serial/{serial}/reactivate
    Body: {"ratePlan": "MVNA Wholesale PAYM 7"}
"""

import logging
import time
from urllib.parse import quote

from django.db import IntegrityError
from django.utils import timezone

from apps.sims.transatel import TransatelAPIError, TransatelError
from apps.sims.transatel.client import APIClient
from apps.sims.transatel.config import get_config
from apps.sims.models import Sim

from .models import ReactivationAttempt, RechargeOrder, TransatelLog
from .serializers import mask_identifier

logger = logging.getLogger("apps.recharge")

MAX_ATTEMPTS = 5

_SUBSCRIBER_BASE = "/connectivity-management/subscribers/api/subscribers/sim-serial/"


# ── Transatel /reactivate call ────────────────────────────────────────────

def reactivate_sim_api(sim_serial, rate_plan, idempotency_key, order=None):
    """Call Transatel POST /reactivate for a suspended SIM.

    Uses the same APIClient as the rest of apps.sims.transatel — OAuth token
    is handled automatically (including 401 → refresh → retry).
    """
    config = get_config()
    client = APIClient(config=config)

    endpoint = _SUBSCRIBER_BASE + quote(str(sim_serial), safe="") + "/reactivate"
    payload = {"ratePlan": rate_plan}

    start = time.time()
    log_entry = TransatelLog(
        order=order,
        action="reactivate",
        sim_serial_masked=mask_identifier(sim_serial),
        request_url=config.get_api_url(endpoint),
        request_method="POST",
        request_body={"ratePlan": rate_plan},
    )

    try:
        # APIClient.post() sends JSON with Bearer auth, retries once on 401.
        # Returns {"success": True, "status_code": 2xx, "data": {...}}
        result = client.post(endpoint, data=payload)

        duration = int((time.time() - start) * 1000)
        log_entry.response_status = result.get("status_code", 200)
        log_entry.response_body = result.get("data", {})
        log_entry.success = True
        log_entry.duration_ms = duration
        log_entry.save()

        return result.get("data", {})

    except TransatelAPIError as exc:
        duration = int((time.time() - start) * 1000)
        log_entry.response_status = exc.status_code
        log_entry.response_body = exc.response if isinstance(exc.response, dict) else {}
        log_entry.error_message = str(exc)
        log_entry.success = False
        log_entry.duration_ms = duration
        log_entry.save()
        raise

    except Exception as exc:
        duration = int((time.time() - start) * 1000)
        log_entry.error_message = str(exc)
        log_entry.success = False
        log_entry.duration_ms = duration
        log_entry.save()
        raise


# ── Reactivation orchestrator ─────────────────────────────────────────────

def reactivate_for_order(order: RechargeOrder, force=False):
    """Reactivate the SIM attached to a paid order. Idempotent.

    Returns the ReactivationAttempt row. Safe to call multiple times —
    a successful reactivation is never re-run.
    """
    sim_serial = order.sim_serial
    if not sim_serial:
        logger.error("Order %s has no sim_serial — cannot reactivate", order.order_ref)
        return None

    if order.status == RechargeOrder.STATUS_PENDING:
        logger.error("Refusing to reactivate unpaid order %s", order.order_ref)
        return None

    # Determine rate plan: product.rate_plan → config default
    rate_plan = ""
    if order.product and order.product.rate_plan:
        rate_plan = order.product.rate_plan
    if not rate_plan:
        try:
            rate_plan = get_config().rate_plan
        except Exception:
            rate_plan = "MVNA Wholesale PAYM 7"

    # Get or create the attempt row (idempotency guard)
    idem_key = ReactivationAttempt.make_idempotency_key(order.id, sim_serial)
    try:
        attempt, created = ReactivationAttempt.objects.get_or_create(
            order=order,
            sim_serial=sim_serial,
            defaults={
                "rate_plan": rate_plan,
                "status": ReactivationAttempt.STATUS_PENDING,
                "idempotency_key": idem_key,
            },
        )
    except IntegrityError:
        attempt = ReactivationAttempt.objects.get(order=order, sim_serial=sim_serial)
        created = False

    # Already succeeded — never re-run
    if attempt.status == ReactivationAttempt.STATUS_SUCCESS:
        logger.info("Order %s already reactivated — skipping", order.order_ref)
        return attempt

    # Max attempts guard
    if attempt.attempts >= MAX_ATTEMPTS and not force:
        logger.error("Order %s hit max reactivation attempts (%d)", order.order_ref, MAX_ATTEMPTS)
        return attempt

    # Check the SIM exists in our local inventory
    sim_qs = Sim.objects.filter(serial_number=sim_serial) | Sim.objects.filter(iccid=sim_serial)
    if not sim_qs.exists():
        attempt.status = ReactivationAttempt.STATUS_SKIPPED
        attempt.error = "SIM serial not found in local inventory."
        attempt.save()
        logger.warning("Reactivation skipped for %s: SIM not in inventory", order.order_ref)
        return attempt

    # Already Active on Transatel — calling /reactivate on an Active SIM
    # returns 400 (confirmed via live test). Nothing to fix, so skip the
    # call and just mark the order complete.
    current_status = (sim_qs.first().provisioning_status or "").strip().lower()
    if current_status == "active":
        attempt.status = ReactivationAttempt.STATUS_SKIPPED
        attempt.error = "SIM already Active — reactivate not needed."
        attempt.save()

        order.status = RechargeOrder.STATUS_COMPLETED
        order.completed_at = timezone.now()
        order.save(update_fields=["status", "completed_at", "updated_at"])

        logger.info(
            "Order %s: SIM already Active — skipped reactivate, marked completed",
            order.order_ref,
        )
        return attempt

    # Increment attempt counter
    attempt.attempts += 1
    attempt.rate_plan = rate_plan
    attempt.save()

    # Call Transatel /reactivate
    try:
        response = reactivate_sim_api(
            sim_serial=sim_serial,
            rate_plan=rate_plan,
            idempotency_key=idem_key,
            order=order,
        )
    except (TransatelAPIError, TransatelError) as exc:
        attempt.status = ReactivationAttempt.STATUS_FAILED
        attempt.error = str(exc)[:2000]
        attempt.save()

        order.status = RechargeOrder.STATUS_FAILED
        order.save(update_fields=["status", "updated_at"])

        logger.error("Reactivation FAILED for order %s: %s", order.order_ref, exc)
        return attempt

    except Exception as exc:
        attempt.status = ReactivationAttempt.STATUS_FAILED
        attempt.error = f"Unexpected error: {exc}"
        attempt.save()
        logger.exception("Reactivation unexpected error for order %s", order.order_ref)
        return attempt

    # Success — extract transaction ID from response
    tx_id = ""
    if isinstance(response, dict):
        tx_id = str(
            response.get("transactionId", "")
            or response.get("transaction_id", "")
            or ""
        )

    attempt.status = ReactivationAttempt.STATUS_SUCCESS
    attempt.provider_transaction_id = tx_id
    attempt.response_data = response
    attempt.error = ""
    attempt.save()

    # Update the SIM's local status to Active
    sim_qs.update(provisioning_status="Active")

    # Mark order completed
    order.status = RechargeOrder.STATUS_COMPLETED
    order.completed_at = timezone.now()
    order.save(update_fields=["status", "completed_at", "updated_at"])

    logger.info(
        "Order %s reactivated successfully (txn=%s)",
        order.order_ref, tx_id or "n/a",
    )
    return attempt


def retry_failed_reactivations(limit=50):
    """Called by management command / celery beat. Retries failed/pending attempts."""
    attempts = ReactivationAttempt.objects.filter(
        status__in=[ReactivationAttempt.STATUS_FAILED, ReactivationAttempt.STATUS_PENDING],
        attempts__lt=MAX_ATTEMPTS,
    ).select_related("order").order_by("updated_at")[:limit]

    results = {"succeeded": 0, "failed": 0, "skipped": 0}

    for attempt in attempts:
        order = attempt.order
        if order.status == RechargeOrder.STATUS_PENDING:
            results["skipped"] += 1
            continue

        result = reactivate_for_order(order)
        if result and result.status == ReactivationAttempt.STATUS_SUCCESS:
            results["succeeded"] += 1
        elif result and result.status == ReactivationAttempt.STATUS_SKIPPED:
            results["skipped"] += 1
        else:
            results["failed"] += 1

    return results