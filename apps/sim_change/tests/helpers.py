from datetime import timedelta

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone

from apps.add_line.models import Line
from apps.sim_change.models import SimTypeChangeCase, StatusSource, StepUpGrant, SupplierVerificationEvidence
from apps.sim_change.services import commands
from apps.sim_change.services.transitions import set_fulfilment, transition
from apps.sims.models import Sim

TEST_KEYS = override_settings(
    SIM_CHANGE_FINGERPRINT_KEY="k" * 40,
    SIM_CHANGE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
)

S = SimTypeChangeCase.Status
SYS, SUP, CUST, POLL = StatusSource.SYSTEM, StatusSource.SUPPORT, StatusSource.CUSTOMER, StatusSource.SUPPLIER_POLL

_counter = {"n": 0}


def make_user(name="alice", staff=False):
    user = User.objects.create_user(name, f"{name}@example.com", "pw-123456")
    if staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])
    return user


def make_sim(esim=False):
    _counter["n"] += 1
    return Sim.objects.create(
        iccid=f"89440000000000{_counter['n']:05d}", type_of_sim="eUICC" if esim else "UICC",
    )


def make_line(owner, esim=False, msisdn="447700900123"):
    return Line.objects.create(
        owner=owner, sim=make_sim(esim=esim), status=Line.Status.ACTIVE, current_msisdn=msisdn,
    )


def make_grant(case, verified_by, **overrides):
    values = dict(
        case=case, line=case.line, subject=case.owner, purpose=StepUpGrant.Purpose.SIM_TYPE_CHANGE,
        verification_method=StepUpGrant.Method.SUPERVISED_SUPPORT, assurance_result=StepUpGrant.Assurance.PASSED,
        policy_version="draft-1", verified_by=verified_by, expires_at=timezone.now() + timedelta(minutes=10),
    )
    values.update(overrides)
    return StepUpGrant.objects.create(**values)


def make_evidence(case, confirmed=True, **overrides):
    values = dict(
        case=case, source="subscriber_read", fetched_at=timezone.now(),
        msisdn_matches=confirmed, subscriber_matches=confirmed, current_serial_is_target=confirmed,
        old_serial_detached=confirmed, service_active=confirmed, commercial_terms_unchanged=confirmed,
        supplier_status="Active" if confirmed else "",
    )
    values.update(overrides)
    return SupplierVerificationEvidence.objects.create(**values)


class Staff:
    """Three distinct staff members for the maker-checker steps."""

    def __init__(self):
        self.agent = make_user("agent", staff=True)
        self.authoriser = make_user("authoriser", staff=True)
        self.executor = make_user("executor", staff=True)


def to_verified(case, staff: Staff):
    case = transition(case, S.ELIGIBILITY_PENDING, source=SYS)
    case = transition(case, S.VERIFY_REQUIRED, source=SYS)
    return transition(case, S.VERIFIED, source=SUP, actor=staff.agent, step_up=make_grant(case, staff.agent))


def to_stock_reserved(case, staff: Staff):
    case = to_verified(case, staff)
    case = transition(case, S.QUOTED, source=SYS)
    case = transition(case, S.AWAITING_CONFIRMATION, source=SYS)
    case = transition(case, S.CASE_ACCEPTED, source=CUST)
    case.new_sim = make_sim(esim=case.to_type == "esim")
    case.save(update_fields=["new_sim"])
    return transition(case, S.STOCK_RESERVED, source=SUP)


def deliver_physical(case, staff: Staff):
    case = set_fulfilment(case, "physical", "AWAITING_DISPATCH", source=SUP, actor=staff.authoriser)
    case = set_fulfilment(case, "physical", "SHIPPED", source=SUP, actor=staff.authoriser,
                          carrier="Royal Mail", tracking_reference="RM123456789GB")
    case = set_fulfilment(case, "physical", "DELIVERED", source=SUP)
    return set_fulfilment(case, "physical", "RECEIPT_CONFIRMED", source=CUST)


def to_swap_in_progress(case, staff: Staff):
    """From STOCK_RESERVED (physical: receipt confirmed) through the two-person swap."""
    command = commands.authorise_swap(case, authoriser=staff.authoriser)
    case.refresh_from_db()
    commands.record_manual_execution(
        command, executor=staff.executor, target_iccid=case.new_sim.iccid, ticket_reference="OPS-1",
        correlation_id="TSL-TX-1",
    )
    case.refresh_from_db()
    return case
