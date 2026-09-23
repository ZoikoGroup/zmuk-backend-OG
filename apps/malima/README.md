# `malima` — Django app

Backs the two endpoints the Next.js checkout proxy hits:

| Method | Path                              | View                       |
| ------ | --------------------------------- | -------------------------- |
| POST   | `/api/v1/malima/allocate-sims/`   | `AllocateSimsView`         |
| POST   | `/api/v1/malima-orders/`          | `MalimaOrderView`          |
| POST   | `/api/v1/malima/release/`         | `ReleaseReservationsView`  |

Plus an admin UI, two management commands (`import_sims`, `expire_reservations`),
and a normalised data model so SIM inventory can be reloaded without
disturbing past orders.

## Install

1. **Copy the `malima/` folder** into your Django project alongside your
   other apps.

2. **Register the app** in your project settings:

   ```python
   INSTALLED_APPS = [
       ...
       "rest_framework",
       "malima",
   ]
   ```

   Requires Django ≥ 4.2 and `djangorestframework`. See `requirements.txt`
   for tested versions.

3. **Mount the URLs** in your root `urls.py`:

   ```python
   from django.urls import include, path

   urlpatterns = [
       ...
       path("api/v1/", include("malima.urls")),
   ]
   ```

   The paths match the env-var defaults in the Next.js patch
   (`DJANGO_ALLOCATE_SIMS_PATH=/api/v1/malima/allocate-sims/`,
   `DJANGO_MALIMA_ORDER_PATH=/api/v1/malima-orders/`).

4. **Migrate**:

   ```bash
   python manage.py migrate malima
   ```

5. **Load SIM inventory** (otherwise allocation always fails):

   ```bash
   python manage.py import_sims path/to/sims.csv --batch Q2-2026-FR
   ```

   CSV columns: `msisdn,imsi,iccid,sim_type,roaming_zone,batch,notes`
   (only the first three are required).

## Data model

```
SimInventory       — the pool. status: available | reserved | activated
                                       | suspended | retired | test
SimReservation     — links a SIM to a cart unit for the duration of an order.
                     status: active | consumed | released | expired
MalimaOrder        — capture record per checkout.
                     status: pending | success | partial | failed
MalimaOrderLine    — one per cart unit. Stores the verbatim Orange request
                     and response, plus the Orange order id.
```

### Why a separate reservation table

The reservation acts as a **hold** between allocation and the save-order
callback. Two failure modes need it:

1. Stripe declines after we've allocated → release reservation, SIM goes
   back to the pool.
2. Browser dies between Orange call and save-order → the cron job
   `expire_reservations` releases anything older than 15 minutes.

Without a hold table you'd either double-book SIMs or have to do
read-modify-write on `SimInventory` from the order view, which is racier.

## Endpoint reference

### POST `/api/v1/malima/allocate-sims/`

**Request** (from the Next.js proxy):

```json
{
  "source": "zoiko-orbit-checkout",
  "requested_at": "2026-05-13T14:30:00Z",
  "cart": [
    {"cartIndex": 0, "unitIndex": 0, "planId": "123", "simType": "eSIM", "roamingZone": "Europe"},
    {"cartIndex": 0, "unitIndex": 1, "planId": "123", "simType": "eSIM", "roamingZone": "Europe"},
    {"cartIndex": 1, "unitIndex": 0, "planId": "456", "simType": "pSIM", "roamingZone": "France"}
  ]
}
```

**Response** — `200 OK` (success or success:false with reason):

```json
{
  "success": true,
  "message": "Allocated 3 SIM(s).",
  "allocations": [
    {"cartIndex": 0, "unitIndex": 0, "msisdn": "337…", "imsi": "208…", "iccid": "893…", "reservation_id": "res-…"},
    ...
  ]
}
```

On insufficient inventory the response is `200 OK` with
`success: false, allocations: []` — the Next.js client expects 200 either
way and inspects `success`. On a validation error it's `400`.

#### Zone matching is one-way

A request with `"roamingZone": "Europe"` prefers SIMs tagged
`roaming_zone="Europe"`, then falls back to zone-less SIMs
(`roaming_zone=""`). A request **without** a zone matches **only**
zone-less SIMs — it won't borrow a zone-tagged SIM. Tag SIMs only when
they're committed to that zone; leave the field blank for general pool
inventory.

#### Concurrency

The allocation transaction uses `SELECT ... FOR UPDATE SKIP LOCKED` so
concurrent checkouts grab different rows without blocking. On PostgreSQL
this gives you true contention-free allocation. SQLite (dev) falls back
to serial execution but still works correctly.

### POST `/api/v1/malima-orders/`

**Request** (from the Next.js save-order proxy):

```json
{
  "source": "zoiko-orbit-checkout",
  "captured_at": "2026-05-13T14:30:05Z",
  "data": {
    "malima": {
      "status": true,
      "message": "...",
      "results": [
        {
          "cartIndex": 0, "unitIndex": 0,
          "planId": 123, "simType": "eSIM",
          "status": 201, "ok": true,
          "request": { /* exact body sent to Orange */ },
          "response": { "id": "ORD-12345", "status": "acknowledged", ... },
          "orangeOrderId": "ORD-12345",
          "msisdn": "337…", "imsi": "208…", "iccid": "893…"
        }
      ]
    },
    "allocations": [ /* what allocate-sims returned */ ],
    "billingAddress": { "email": "...", "firstName": "...", ... },
    "shippingAddress": { ... },
    "cart": [ /* original cart products */ ],
    "totals": { "subtotal": "39.98", "shipping": "5.00", "discount": "0",
                "activationFee": "0", "total": "44.98" },
    "stripe_payment_intent_id": "pi_…",
    "paymentMethod": "stripe",
    "currency": "USD"
  }
}
```

**Response** — `201 Created`:

```json
{
  "success": true,
  "message": "Order captured.",
  "order_id": 42,
  "status_": "success",
  "line_count": 1
}
```

**Side-effects:**

- Creates one `MalimaOrder` + one `MalimaOrderLine` per result.
- For every line with `ok: true` → reservation marked `consumed`, SIM marked `activated`.
- For every line with `ok: false` (and any allocated reservation not referenced in `results`) → reservation marked `released`, SIM marked `available`.
- SIMs that ops manually `suspended`/`retired` while a reservation was open are **not** reset — admin actions win.

### POST `/api/v1/malima/release/`

Manual ops endpoint for releasing reservations that got stuck somehow.

```json
{ "reservation_ids": ["res-…", "res-…"] }
```

Same SIM-status logic as the failure path in save-order.

## Ops

### Bulk-load inventory

```bash
python manage.py import_sims sims.csv               # uses csv's `batch` column
python manage.py import_sims sims.csv --batch X     # override batch tag for all
python manage.py import_sims sims.csv --dry-run     # validate only
```

Existing rows (matched by `msisdn`) are skipped — edit via the admin if
you need to update.

### Expire stuck reservations

Run every 5 minutes:

```cron
*/5 * * * * /path/to/venv/bin/python /path/to/manage.py expire_reservations
```

### Admin

Mounted under the standard Django admin. From there you can:

- Browse / search SIM inventory by msisdn / iccid / batch
- Bulk-mark SIMs as available / test / retired (admin actions)
- Drill into any order to see its lines, request/response payloads, and the reservation chain back to inventory
- Manually retire SIMs that came back damaged

### Logs

The views log to the `malima` logger. Recommended config in settings.py:

```python
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "malima": {"handlers": ["console"], "level": "INFO"},
    },
}
```

## Auth

Both views default to `AllowAny` so the Next.js proxy (which does its own
trust) can call them without ceremony. If you want to lock them down:

1. Set `permission_classes = [IsAuthenticated]` on the views.
2. Use DRF's `TokenAuthentication`.
3. Set `DJANGO_API_TOKEN` in the Next.js `.env.local` — the proxy
   already sends `Authorization: Token <…>` when that var is set.

## Tests

The `test_e2e.py` in the patch root exercises the full happy + sad paths
against in-memory SQLite. Adapt it into your project's test suite:

```bash
python manage.py test malima
```

If you want a starter test file: see `test_e2e.py` for the cases (allocate,
save with mixed success/failure, out-of-inventory, validation errors).
