from rest_framework.permissions import BasePermission, SAFE_METHODS
from .tenant import has_any_perm, access_state


class IsVerifiedUser(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.email_verified and access_state(u) == "ok")


class HasPerm(BasePermission):
    """Views declare read_perm / write_perm as tuples of permission keys (any-of).
    Missing = any signed-in user. write_perm falls back to read_perm.
    The owner holds every permission."""

    def has_permission(self, request, view):
        u = request.user
        if not (u and u.is_authenticated and u.email_verified and access_state(u) == "ok"):
            return False
        read = getattr(view, "read_perm", None)
        write = getattr(view, "write_perm", read)
        wanted = read if request.method in SAFE_METHODS else write
        return has_any_perm(u, wanted)
