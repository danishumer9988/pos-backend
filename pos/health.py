from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Open  https://YOUR-BACKEND/api/health  in a browser to see what is wrong with a deployment.
    Shows no secrets: only whether each part works and which website addresses are allowed."""
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = []

    def get(self, request):
        out = {"ok": True}
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
            out["database"] = "ok"
        except Exception:
            out["database"] = "ERROR - check DATABASE_URL"
            out["ok"] = False
        if out["database"] == "ok":
            try:
                ex = MigrationExecutor(connection)
                pending = len(ex.migration_plan(ex.loader.graph.leaf_nodes()))
                out["migrations_pending"] = pending
                if pending:
                    out["ok"] = False
                    out["fix"] = "Run `python manage.py migrate` against this database."
            except Exception:
                out["migrations_pending"] = "unknown"
        try:
            cache.set("health_probe", 1, 10)
            out["rate_limit_store"] = "ok" if cache.get("health_probe") == 1 else "unavailable (rate limits are off)"
        except Exception:
            out["rate_limit_store"] = "unavailable (rate limits are off)"
        out["email"] = settings.EMAIL_BACKEND.rsplit(".", 2)[-2]   # smtp = real emails, console = only printed in logs
        out["debug"] = settings.DEBUG
        out["frontend_url"] = settings.FRONTEND_URL
        out["cors_allowed_origins"] = list(settings.CORS_ALLOWED_ORIGINS)
        origin = request.headers.get("Origin")
        if origin:
            out["this_origin_allowed"] = settings.CORS_ALLOW_ALL_ORIGINS or origin in settings.CORS_ALLOWED_ORIGINS
        return Response(out, status=200 if out["ok"] else 503)
