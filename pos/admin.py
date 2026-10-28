from datetime import timedelta
from django.contrib import admin, messages
from django.utils import timezone
from .models import *
from .notify import send_approved_email, send_rejected_email
from .tenant import role_of, account_owner

admin.site.site_header = "POS Administration"
admin.site.site_title = "POS Admin"
admin.site.index_title = "Signups, approvals and access periods"


def _approve(user, days):
    """Approve (or extend) an account for `days` days from now (or from its current expiry)."""
    now = timezone.now()
    base = user.access_expires_at if user.approval_status == "approved" and user.access_expires_at and user.access_expires_at > now else now
    user.approval_status = "approved"
    user.access_days = days
    user.access_expires_at = base + timedelta(days=days)
    user.approved_at = user.approved_at or now
    user.save()


def _make_approve_action(days):
    @admin.action(description=f"Approve / extend by {days} days (emails the user)")
    def action(modeladmin, request, queryset):
        n = 0
        for u in queryset:
            first_time = u.approval_status != "approved"
            _approve(u, days)
            if first_time:
                send_approved_email(u)
            n += 1
        modeladmin.message_user(request, f"{n} account(s) approved or extended by {days} days.", messages.SUCCESS)
    action.__name__ = f"approve_{days}_days"
    return action


@admin.action(description="Reject selected (emails the user)")
def reject_selected(modeladmin, request, queryset):
    for u in queryset.exclude(is_superuser=True):
        u.approval_status = "rejected"
        u.save(update_fields=["approval_status", "updated_at"])
        send_rejected_email(u)
    modeladmin.message_user(request, "Selected accounts rejected.", messages.WARNING)


@admin.action(description="Stop access now (expire immediately)")
def expire_now(modeladmin, request, queryset):
    queryset.exclude(is_superuser=True).update(access_expires_at=timezone.now())
    modeladmin.message_user(request, "Access ended for the selected accounts.", messages.WARNING)


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    ordering = ("-created_at",)
    list_display = ("email", "full_name", "kind", "approval_status", "email_verified",
                    "access_expires_at", "days_left", "created_at")
    list_filter = ("approval_status", "email_verified", "is_active")
    search_fields = ("email", "full_name", "phone")
    readonly_fields = ("created_at", "updated_at", "approved_at", "last_login")
    fieldsets = (
        ("Account", {"fields": ("email", "full_name", "phone", "email_verified", "is_active")}),
        ("Approval & access period", {
            "description": "Set status to Approved and choose 'Days of access' - the end date is filled in "
                           "automatically and the user gets the 'POS is ready' email. "
                           "To set an exact end date instead, edit 'Access expires at' too. Leave it empty for no expiry.",
            "fields": ("approval_status", "access_days", "access_expires_at", "approved_at")}),
        ("Admin rights", {"classes": ("collapse",), "fields": ("is_staff", "is_superuser")}),
        ("Dates", {"classes": ("collapse",), "fields": ("created_at", "updated_at", "last_login")}),
    )
    actions = [_make_approve_action(d) for d in (7, 10, 20, 30, 90, 365)] + [reject_selected, expire_now]

    def has_add_permission(self, request):
        return False  # accounts are created by signing up (or by the owner for staff)

    @admin.display(description="Type")
    def kind(self, obj):
        return "admin" if obj.is_superuser else role_of(obj)

    @admin.display(description="Days left")
    def days_left(self, obj):
        o = account_owner(obj)
        if o.approval_status != "approved":
            return "-"
        if not o.access_expires_at:
            return "no expiry"
        d = (o.access_expires_at - timezone.now()).days
        return "expired" if o.access_expires_at <= timezone.now() else d

    def save_model(self, request, obj, form, change):
        old = User.objects.filter(pk=obj.pk).values_list("approval_status", flat=True).first() if change else None
        became_approved = obj.approval_status == "approved" and old != "approved"
        if became_approved:
            obj.approved_at = timezone.now()
            if "access_expires_at" not in form.changed_data and obj.access_days:
                obj.access_expires_at = timezone.now() + timedelta(days=obj.access_days)
        super().save_model(request, obj, form, change)
        if became_approved:
            ok = send_approved_email(obj)
            self.message_user(request, "Approval email sent." if ok else
                              "Approved, but the email could not be sent - check the email settings.",
                              messages.SUCCESS if ok else messages.WARNING)
        elif obj.approval_status == "rejected" and old != "rejected":
            send_rejected_email(obj)


@admin.register(Business)
class BusinessAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "created_at")
    search_fields = ("name", "owner__email")


@admin.register(DemoRequest)
class DemoRequestAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "business_type", "created_at")
    ordering = ("-created_at",)


admin.site.register([EmailOTP, SessionToken, SuperCategory, BusinessType, ProductCategory, Product,
                     Customer, DiningTable, Order, OrderItem, Payment, StockMovement, StaffMember, Expense])
