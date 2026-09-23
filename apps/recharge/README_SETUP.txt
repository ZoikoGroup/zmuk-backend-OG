# ============================================================
# 1) Add to INSTALLED_APPS in core/settings.py
# ============================================================
    "apps.recharge",

# ============================================================
# 2) Add to core/urls.py (with the other path(...) includes)
# ============================================================
    path("api/recharge/", include("apps.recharge.urls")),

# ============================================================
# 3) Add a STRIPE block near the bottom of core/settings.py
#    Read from environment; never hardcode live keys in git.
# ============================================================
import os
STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")        # sk_test_... (Django only)
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")   # pk_test_... (safe to expose)
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")    # whsec_...  (Django only)

# On Windows PowerShell, set test keys for the current session like:
#   $env:STRIPE_SECRET_KEY="sk_test_xxx"
#   $env:STRIPE_WEBHOOK_SECRET="whsec_xxx"
# (or add python-dotenv + a .env so they load automatically)

# ============================================================
# 4) Install the Stripe library, then migrate + seed
# ============================================================
#   pip install stripe
#   python manage.py makemigrations recharge
#   python manage.py migrate
#   python manage.py seed_recharge_modules      # creates the 3 modules, all Enabled

# ============================================================
# 5) Local webhook testing (Django can't be reached by Stripe on 127.0.0.1)
#    Install Stripe CLI, then:
#   stripe listen --forward-to localhost:8000/api/recharge/webhook/
#    It prints a whsec_... -> put that in STRIPE_WEBHOOK_SECRET for local testing.
