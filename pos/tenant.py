"""Which business a user belongs to, what they may do, and whether their access is still valid."""
from django.utils import timezone
from .models import Business

OWNER, MANAGER, CASHIER = "owner", "manager", "cashier"

# Everything a staff member can be given, one entry per page/feature.
STAFF_PERMS = ["dashboard", "checkout", "orders", "refunds", "inventory",
               "customers", "finance", "analytics", "floor_plan"]
# Only the owner ever has these (staff can never be granted them).
OWNER_ONLY = ["users", "settings", "tables_edit"]
ALL_PERMS = STAFF_PERMS + OWNER_ONLY

ROLE_DEFAULTS = {
    MANAGER: ["dashboard", "checkout", "orders", "refunds", "inventory",
              "customers", "finance", "analytics", "floor_plan"],
    CASHIER: ["checkout", "orders", "customers", "floor_plan"],
}


def business_of(user):
    try:
        return user.business            # owner
    except Business.DoesNotExist:
        pass
    m = getattr(user, "membership", None) if user.pk else None
    return m.business if m else None    # staff


def role_of(user):
    m = getattr(user, "membership", None)
    return m.role if m else OWNER


def perms_of(user):
    m = getattr(user, "membership", None)
    if not m:
        return list(ALL_PERMS)
    return [p for p in (m.permissions or []) if p in STAFF_PERMS]


def has_any_perm(user, wanted):
    if not wanted:
        return True
    have = set(perms_of(user))
    return any(p in have for p in wanted)


def account_owner(user):
    """The user whose approval / subscription window governs this login."""
    m = getattr(user, "membership", None)
    return m.business.owner if m else user


def access_state(user):
    """'ok' | 'pending' | 'rejected' | 'expired'. Superusers always pass."""
    if user.is_superuser:
        return "ok"
    owner = account_owner(user)
    if owner.approval_status == "pending":
        return "pending"
    if owner.approval_status == "rejected":
        return "rejected"
    if owner.access_expires_at and owner.access_expires_at <= timezone.now():
        return "expired"
    return "ok"
