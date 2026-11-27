"""Staff roles for Change SIM type (CTO Addendum v2.1 A3).

Each role is a Django group, created (or brought up to date) after every
`migrate` and by `manage.py setup_sim_change_roles`. Admins put staff in the
right group; a person also needs `is_staff` to reach the admin.

QA / Audit can only view: no role can manufacture a supplier confirmation.
"""

from django.contrib.auth.models import Group, Permission

VIEW_ALL = [
    "view_simtypechangecase", "view_simtypechangeevent", "view_stepupgrant", "view_providercommand",
    "view_supplierverificationevidence", "view_swapsimstock", "view_lineoperationlock",
    "view_simchangeplaneligibility",
]

ROLES = {
    "SIM change: Security Support Agent": ["verify_customer", "manage_cases"] + VIEW_ALL,
    "SIM change: Network Ops Authoriser": ["authorise_swap", "manage_stock", "manage_fulfilment"] + VIEW_ALL,
    "SIM change: Network Ops Executor": ["execute_swap", "manage_fulfilment"] + VIEW_ALL,
    "SIM change: Security / Fraud": ["approve_verification", "review_fraud", "manage_cases"] + VIEW_ALL,
    "SIM change: QA / Audit": VIEW_ALL,
}


def ensure_roles() -> dict:
    """Create the groups and set exactly their sim_change permissions. Idempotent."""
    created = {}
    for name, codenames in ROLES.items():
        group, was_created = Group.objects.get_or_create(name=name)
        perms = list(Permission.objects.filter(content_type__app_label="sim_change", codename__in=codenames))
        missing = set(codenames) - {p.codename for p in perms}
        if missing:
            raise RuntimeError(f"Permissions not found (run migrate first): {sorted(missing)}")
        others = group.permissions.exclude(content_type__app_label="sim_change")
        group.permissions.set(list(others) + perms)
        created[name] = was_created
    return created
