import hashlib
import secrets
from datetime import timedelta
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from .models import SessionToken
from .tenant import access_state

SESSION_DAYS = 30

def create_session(user, request):
    raw = secrets.token_urlsafe(48)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    SessionToken.objects.create(
        user=user,
        token_hash=digest,
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
        ip=request.META.get("REMOTE_ADDR"),
        expires_at=timezone.now() + timedelta(days=SESSION_DAYS),
    )
    return raw

def revoke_session(raw_token):
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    SessionToken.objects.filter(token_hash=digest, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )

def revoke_all_sessions(user):
    SessionToken.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )

class BearerTokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return None
        raw = header[7:].strip()
        digest = hashlib.sha256(raw.encode()).hexdigest()
        session = SessionToken.objects.select_related("user").filter(
            token_hash=digest,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
            user__is_active=True,
        ).first()
        if not session:
            raise AuthenticationFailed({"error": "invalid_session"})
        state = access_state(session.user)
        if state != "ok":
            raise AuthenticationFailed({"error": f"access_{state}"})
        return (session.user, session)

    def authenticate_header(self, request):
        # Makes DRF return 401 (not 403) when the token is missing/invalid
        return "Bearer"
