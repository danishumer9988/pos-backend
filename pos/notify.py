"""Emails for the approval workflow. Failures never break the admin action."""
import logging
from django.conf import settings
from django.core.mail import send_mail

log = logging.getLogger(__name__)


def _send(subject, body, to):
    try:
        send_mail(subject, body, None, [to], fail_silently=False)
        return True
    except Exception:  # SMTP down etc. - the approval itself must still succeed
        log.exception("Could not send email to %s", to)
        return False


def send_approved_email(user):
    days = ""
    if user.access_expires_at:
        days = f"\nYour access is active until {user.access_expires_at:%d %b %Y}."
    link = f"{settings.FRONTEND_URL.rstrip('/')}/login"
    return _send(
        "Your POS is ready to use",
        f"Hi {user.full_name},\n\nGood news - your account has been approved and your POS is ready for use.{days}\n\n"
        f"Log in here: {link}\n\nThank you!",
        user.email,
    )


def send_rejected_email(user):
    return _send(
        "Your POS request",
        f"Hi {user.full_name},\n\nUnfortunately we could not approve your request at this time. "
        "Please contact us if you think this is a mistake.",
        user.email,
    )


def notify_admin_new_signup(user):
    to = getattr(settings, "ADMIN_NOTIFY_EMAIL", "")
    if to:
        _send("New POS signup waiting for approval",
              f"{user.full_name} <{user.email}> verified their email and is waiting for approval in the admin panel.", to)
