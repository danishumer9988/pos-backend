"""Usage:  python manage.py test_email you@example.com

Sends one real email using your current settings and prints the exact error if it fails.
"""
import os
import smtplib
import socket
from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError

HINTS = [
    (smtplib.SMTPAuthenticationError,
     "The server rejected the login. Gmail needs an *App Password* (Google account > Security > 2-Step "
     "Verification > App passwords), not your normal password. Check EMAIL_HOST_USER and EMAIL_HOST_PASSWORD."),
    (smtplib.SMTPSenderRefused,
     "The sender address was refused. Set DEFAULT_FROM_EMAIL to an address your provider has verified "
     "(for Gmail: the same address as EMAIL_HOST_USER)."),
    (smtplib.SMTPRecipientsRefused, "The recipient address was refused. Check the spelling of the address."),
    (socket.gaierror, "The host name could not be found. Check EMAIL_HOST for typos."),
    (TimeoutError, "Timed out connecting. Check EMAIL_HOST and EMAIL_PORT (587 with TLS, or 465 with SSL) and "
                   "that your network/firewall allows outgoing SMTP."),
    (ConnectionRefusedError, "Connection refused. Check EMAIL_HOST and EMAIL_PORT."),
    (smtplib.SMTPServerDisconnected, "The server closed the connection. Usually a wrong port or TLS/SSL mismatch: "
                                      "use 587 with EMAIL_USE_TLS=True, or 465 with EMAIL_USE_SSL=True."),
]


class Command(BaseCommand):
    help = "Send a test email and show exactly why it fails, if it does."

    def add_arguments(self, parser):
        parser.add_argument("to", help="address that should receive the test email")

    def report_env_file(self):
        """Most 'my settings are ignored' problems are the .env file: wrong place, wrong name, or never restarted."""
        path = settings.DOTENV_PATH
        email_keys = [k for k in settings.DOTENV_KEYS if k.startswith("EMAIL_")]
        if path.exists():
            self.stdout.write(f".env    : {path}  (found, {len(settings.DOTENV_KEYS)} settings, "
                              f"{len(email_keys)} of them EMAIL_*)")
            if "EMAIL_HOST" not in settings.DOTENV_KEYS:
                self.stdout.write(self.style.WARNING("          EMAIL_HOST is not in that file."))
            if not os.getenv("EMAIL_HOST") and "EMAIL_HOST" in settings.DOTENV_KEYS:
                self.stdout.write(self.style.WARNING("          EMAIL_HOST is in the file but empty - put the value after the = sign."))
        else:
            self.stdout.write(self.style.WARNING(f".env    : NOT FOUND at {path}"))
            self.stdout.write(self.style.WARNING("          It must be named exactly `.env` and sit next to manage.py."))
            for odd in (".env.txt", "env", ".env.example.txt", "env.txt"):
                if (settings.BASE_DIR / odd).exists():
                    self.stdout.write(self.style.WARNING(
                        f"          Found `{odd}` instead. Windows hides extensions - rename it to exactly `.env` "
                        "(PowerShell:  Rename-Item .env.txt .env )."))
        shadow = [k for k in ("EMAIL_BACKEND", "EMAIL_HOST") if os.getenv(k) and k not in settings.DOTENV_KEYS]
        if shadow:
            self.stdout.write(self.style.WARNING(
                f"          {', '.join(shadow)} come from your system/IDE environment, not from .env "
                "(PyCharm run configuration or Windows environment variables)."))

    def handle(self, *args, **opts):
        s = settings
        backend = s.EMAIL_BACKEND.rsplit(".", 2)[-2]
        self.report_env_file()
        self.stdout.write(f"Backend : {backend}")
        self.stdout.write(f"Host    : {s.EMAIL_HOST or '(not set)'}:{s.EMAIL_PORT}  TLS={s.EMAIL_USE_TLS} SSL={s.EMAIL_USE_SSL}")
        self.stdout.write(f"Login   : {s.EMAIL_HOST_USER or '(not set)'}   password set: {bool(s.EMAIL_HOST_PASSWORD)}")
        self.stdout.write(f"From    : {s.DEFAULT_FROM_EMAIL}")
        if backend == "console":
            self.stdout.write(self.style.WARNING(
                "\nThe CONSOLE backend is active: emails are only printed below, never delivered.\n"
                "To send real email set EMAIL_HOST (and EMAIL_HOST_USER / EMAIL_HOST_PASSWORD) and remove any\n"
                "EMAIL_BACKEND=...console... line from your .env file.\n"))
        try:
            send_mail("POS test email", "If you can read this, email sending works.", None, [opts["to"]], fail_silently=False)
        except Exception as e:
            hint = next((h for cls, h in HINTS if isinstance(e, cls)), "")
            self.stdout.write(self.style.ERROR(f"\nFAILED: {type(e).__name__}: {e}"))
            if hint:
                self.stdout.write(self.style.WARNING(f"Likely fix: {hint}"))
            raise CommandError("Email could not be sent.")
        self.stdout.write(self.style.SUCCESS(
            f"\nSent to {opts['to']}." + (" (Console backend: look above for the message.)" if backend == "console" else " Check the inbox and the spam folder.")))
