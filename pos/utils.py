import hashlib
import logging
import secrets
from datetime import timedelta
from django.core.mail import send_mail
from django.contrib.auth.hashers import make_password, check_password
from django.utils import timezone
from .models import EmailOTP

log = logging.getLogger(__name__)

OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5

def generate_otp():
    return f"{secrets.randbelow(1000000):06d}"

def create_otp(user, purpose):
    EmailOTP.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True).update(
        consumed_at=timezone.now()
    )
    code = generate_otp()
    row = EmailOTP.objects.create(
        user=user,
        otp_hash=make_password(code),
        purpose=purpose,
        expires_at=timezone.now() + timedelta(minutes=OTP_TTL_MINUTES),
    )
    subject = {"verify": "Your POS email verification code", "reset": "Your POS password reset code"}.get(
        purpose, f"Your POS {purpose} code")
    try:
        send_mail(
            subject,
            f"Your verification code is {code}. It expires in {OTP_TTL_MINUTES} minutes.",
            None,
            [user.email],
            fail_silently=False,
        )
    except Exception:
        # Callers turn this into a generic "email_delivery_failed"; the real reason goes to the log.
        from django.conf import settings
        log.exception("Could not send %s code to %s (backend=%s host=%s port=%s user=%s)", purpose, user.email,
                      settings.EMAIL_BACKEND.rsplit(".", 2)[-2], settings.EMAIL_HOST, settings.EMAIL_PORT,
                      settings.EMAIL_HOST_USER)
        raise
    return row

def verify_otp(row, code):
    if row.attempts >= OTP_MAX_ATTEMPTS or row.expires_at <= timezone.now():
        return False
    row.attempts += 1
    row.save(update_fields=["attempts"])
    return check_password(code, row.otp_hash)
