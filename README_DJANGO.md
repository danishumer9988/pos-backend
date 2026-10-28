# Tillo POS — Django Conversion

This version converts the original **Node.js + Express + TypeScript + Drizzle** backend to:

- Python
- Django
- Django REST Framework
- PostgreSQL/Neon in production
- SQLite fallback for local development
- Bearer-token authentication compatible with the existing React frontend
- Email OTP verification and password reset
- Multi-tenant business isolation
- Products and categories APIs
- Django Admin

## Architecture

React/Vite frontend (kept from the original project)
        ↓
Django REST API
        ↓
Django ORM
        ↓
PostgreSQL / Neon

The React frontend can continue using the same `/api/...` endpoints. Set:

VITE_API_URL=http://127.0.0.1:8000

in the frontend `.env`.

## Setup

### 1. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure environment

Copy `.env.example` to `.env` and set `DATABASE_URL` if using Neon/PostgreSQL.

For local testing, leave `DATABASE_URL` empty and Django will use SQLite.

### 3. Migrate

```powershell
python manage.py migrate
```

### 4. Seed business categories

```powershell
python manage.py seed_categories
```

### 5. Create Django admin

```powershell
python manage.py createsuperuser
```

### 6. Start Django

```powershell
python manage.py runserver 8000
```

API:
- `http://127.0.0.1:8000/api/auth/...`
- `http://127.0.0.1:8000/api/me/products`
- `http://127.0.0.1:8000/api/me/categories`
- `http://127.0.0.1:8000/api/me/orders` (checkout, list, pay/cancel/refund)
- `http://127.0.0.1:8000/api/me/customers`
- `http://127.0.0.1:8000/api/me/analytics?days=7`
- `http://127.0.0.1:8000/api/me/finance`
- `http://127.0.0.1:8000/api/business/profile` (PATCH)
- `http://127.0.0.1:8000/api/business/categories`

Admin:
- `http://127.0.0.1:8000/admin/`

## Quick start (copy-paste)

```powershell
# Terminal 1 — backend
python -m venv .venv ; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python manage.py migrate
python manage.py seed_categories
python manage.py runserver 8000

# Terminal 2 — frontend
cd frontend
npm install
npm run dev
```

**Where is my signup code?** With the default settings no real email is sent. The 6-digit
verification code is **printed in the Terminal 1 window** (the one running `runserver`).
Copy it from there into the verify page. For real emails, set the SMTP variables below.

Run the automated tests with `python manage.py test`.

## Not receiving the signup code?

1. Run `python manage.py test_email you@gmail.com` - it prints your email settings and the exact error.
2. With no `EMAIL_HOST` set, the code is only printed in the backend terminal (no email is sent).
3. If you copied an older `.env.example`, delete the line `EMAIL_BACKEND=...console...` from your `.env`.
4. Gmail needs an **App Password** (Google account > Security > 2-Step Verification), not your normal password.

## Email OTP

Development uses Django's console email backend by default. When signup is tested, the OTP appears in the Django terminal.

For real email, configure SMTP variables in `.env`:

```env
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=...
EMAIL_HOST_PASSWORD=...
EMAIL_USE_TLS=True
DEFAULT_FROM_EMAIL=no-reply@example.com
```

## Existing frontend

The original React/Vite frontend is intentionally retained because Django is the backend framework conversion. This avoids rewriting a working React UI into server-rendered templates while preserving the same API contract.

The original frontend source is in `frontend/`.

## Important security behavior retained

- Business records are always filtered by the authenticated user's business.
- Bearer tokens are stored hashed in the database.
- Sessions expire and can be revoked.
- OTPs are hashed, expire, and have attempt limits.
- Money remains stored in minor units (`price_cents`, `total_cents`).
- Email verification is required before protected POS APIs.

## Signup approval and access periods

1. A new person signs up and verifies their email. They are **not** logged in. They see
   "Your request has been sent to the admin".
2. Create your admin login once: `python manage.py createsuperuser`, then open
   `http://127.0.0.1:8000/admin/` and go to **Users**.
3. Select the waiting user(s) (filter: Approval status = Pending) and use an action such as
   **Approve / extend by 10 days** (also 7, 20, 30, 90, 365). Or open the user, set
   *Approval status* = Approved and *Days of access*, then save.
4. The user gets the "your POS is ready" email with the login link (`FRONTEND_URL`).
5. When the period ends, that user and all their staff are blocked automatically.
   Use **Approve / extend** again to give more days. **Stop access now** ends it immediately.

Without SMTP settings, emails are printed in the backend terminal.

## Roles

| Role | Can do |
|------|--------|
| Owner | Everything, including Users and Settings |
| Manager | Inventory, stock, reports (Finance/Analytics), refunds, customers, checkout |
| Cashier | Checkout, orders (no refunds), customers (no delete) |

Roles are enforced by the API, not just hidden in the menu. The owner adds staff under **Users**;
staff sign in with the email and password the owner sets. Disabling a staff member logs them out immediately.

## Features

- Checkout with discount (amount or %), tax, cash/card, change calculation, open orders
- Printable receipts (browser print, 80mm friendly)
- Optional stock tracking per product, automatic deduction, restock on cancel/refund, full stock history
- Orders: pay, cancel, refund. Customers. Finance and Analytics (paid orders only; refunds excluded)
- Rate limits: login 10/min, signup 20/hour, OTP 20/hour, demo form 10/hour (per IP)

## Not built yet (be aware)

- Password-reset email for staff (the owner resets it under Users instead).
- Split payments, partial refunds, and printing to a dedicated thermal printer (uses the browser print dialog).
- Barcode scanning, multiple branches/locations, offline mode.
- CSV/PDF export of reports.
- Tested on SQLite only. Test on PostgreSQL before production.
