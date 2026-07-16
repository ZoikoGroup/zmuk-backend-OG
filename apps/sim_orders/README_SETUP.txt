SIM ORDERS APP — SETUP
======================
Folder goes at: apps/sim_orders/   (keep management/commands/ and migrations/)

1) core/settings.py -> INSTALLED_APPS:
       "apps.sim_orders",

2) core/urls.py (with the other includes):
       path("api/sim/", include("apps.sim_orders.urls")),

3) Stripe keys are shared with the recharge app. If not added yet, near the
   bottom of settings.py:
       import os
       STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
       STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
       STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

4) Install + migrate + seed:
       pip install stripe
       python manage.py makemigrations sim_orders
       python manage.py migrate
       python manage.py seed_sim_products      # a few test SIM products

5) Local Stripe webhook (same CLI as recharge — one listener can serve both if
   you point Stripe at each path, or run two):
       stripe listen --forward-to localhost:8000/api/sim/webhook/

ENDPOINTS
  GET  /api/sim/products/   list SIMs for sale
  POST /api/sim/buy/        {product_id|slug, email, customer_name, success_url, cancel_url}
                            -> {order_ref, checkout_url}  (redirect user to checkout_url)
  POST /api/sim/webhook/    Stripe calls this; on paid -> issues ZM###### + emails it
  GET  /api/sim/orders/     recent SIM orders

WHAT HAPPENS ON PAYMENT
  Stripe confirms payment -> webhook marks order Completed -> issues a ZM######
  code (valid 24h, single-use) tied to the order + email -> emails it (falls back
  to printing in the terminal if SMTP is blocked). This is what the activation
  page will validate against (NEXT STEP).

NEXT STEP (not in this app yet)
  Update the 'activation' app to VALIDATE the entered code+email against
  SimActivationCode (24h, single-use, attempt-limited), then call Transatel
  activate and redirect to the dashboard. Import:
       from apps.sim_orders.models import SimActivationCode
