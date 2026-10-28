# Deploying the POS: React on Vercel, Django on Vercel (two separate projects)

You host the **frontend** and **backend** separately. They talk through the internet.
Order matters: database -> backend -> frontend -> connect them.

## Read this first (costs and limits)
- **Vercel's free "Hobby" plan is for non-commercial use only.** A POS you sell to shops is commercial,
  so you need the paid **Pro** plan (about $20 per user per month when I checked; confirm at vercel.com/pricing).
- Vercel cannot use SQLite. You must use PostgreSQL (Neon is the easy choice).
- The backend runs as a serverless function: the first request after a quiet period can be slower.
- Without real email settings **nobody can sign up** (the codes are only printed to a log).

## 1. Database (Neon)
1. Create a free project at neon.tech. Copy the connection string (it starts with `postgresql://`).
2. Neon shows a **pooled** string (host contains `-pooler`) and a **direct** one.
   Use the **pooled** one for the app (Vercel env var) and the **direct** one for step 5 (migrate).
   (Or add Neon from the Vercel Marketplace - it creates `DATABASE_URL` for you.)

## 2. Put the code on GitHub
One repository with `frontend/` inside the project root (as it is now). Make sure `.env`, `db.sqlite3`,
`node_modules` and `.venv` are NOT committed (they are in `.gitignore`).

## 3. Backend project on Vercel
1. Vercel -> **Add New -> Project** -> import the repo.
2. **Root Directory:** `./` (the folder that contains `manage.py`). Vercel detects Django by itself.
3. **Environment Variables** (Production):

| Name | Value |
|------|-------|
| `DJANGO_SECRET_KEY` | long random text. Make one: `python -c "import secrets;print(secrets.token_urlsafe(60))"` |
| `DATABASE_URL` | the Neon **pooled** string |
| `CORS_ALLOWED_ORIGINS` | your frontend address, e.g. `https://my-pos.vercel.app` (no `/` at the end). Fill in after step 4 |
| `FRONTEND_URL` | same frontend address (used in the "your POS is ready" email) |
| `CSRF_TRUSTED_ORIGINS` | your backend address, e.g. `https://my-pos-api.vercel.app` |
| `ALLOWED_HOSTS` | only needed if you add a custom domain, e.g. `api.myshop.com` |
| `EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` |
| `EMAIL_HOST` / `EMAIL_PORT` | `smtp.gmail.com` / `587` |
| `EMAIL_HOST_USER` | your Gmail address |
| `EMAIL_HOST_PASSWORD` | a Gmail **App Password** (Google account -> Security -> 2-Step Verification -> App passwords) |
| `DEFAULT_FROM_EMAIL` | same as `EMAIL_HOST_USER` (optional - it defaults to that) |

   Do **not** set `EMAIL_BACKEND` to the console one. If `EMAIL_HOST` is set, real email is used automatically.
   After deploying, check email from your PC with the same variables: `python manage.py test_email you@gmail.com`.
| `ADMIN_NOTIFY_EMAIL` | optional: your email, to be told about new signups |

   `DJANGO_DEBUG` is OFF automatically on Vercel. Never set it to True in production.
4. **Deploy.** Copy the backend address (e.g. `https://my-pos-api.vercel.app`).

## 4. Frontend project on Vercel
1. **Add New -> Project** -> import the **same repo** again.
2. **Root Directory:** `frontend`. Framework: **Vite** (build `npm run build`, output `dist`).
3. **Environment Variable:** `VITE_API_URL` = the backend address from step 3 (no `/` at the end).
   This is baked in at build time - change it, then redeploy.
4. **Deploy.** `frontend/vercel.json` makes refreshing pages like `/dashboard/orders` work.
5. Copy the frontend address, go back to the backend project, set `CORS_ALLOWED_ORIGINS` and `FRONTEND_URL`
   to it, then **Redeploy** the backend (environment changes only apply to new deployments).

## 5. One-time database setup (run on your own PC)
Use the Neon **direct** string. In PowerShell, inside the project folder (venv active):

    $env:DATABASE_URL = "postgresql://USER:PASSWORD@HOST/neondb?sslmode=require"
    python manage.py migrate
    python manage.py createcachetable
    python manage.py seed_categories
    python manage.py createsuperuser

Then close that PowerShell window. Do **not** put the production string in your `.env` file
(your local app would start using the live database).
`createcachetable` is needed: the login rate limit stores its counters there so it works across servers.

## 6. Test it
1. Open the frontend address -> Sign up -> check your email for the 6-digit code.
2. Open `https://YOUR-BACKEND/admin/` -> log in with the superuser -> Users -> approve with days.
3. The user receives the "POS is ready" email and can log in.

## If something goes wrong
| Symptom | Cause / fix |
|---------|-------------|
| Browser console: CORS error | `CORS_ALLOWED_ORIGINS` is wrong or has a trailing `/`, or you did not redeploy the backend |
| Signup says "Can't reach the server" | `VITE_API_URL` wrong/missing, or the frontend was not redeployed after changing it |
| Signup works but no email arrives | email variables wrong. Check the backend logs in Vercel -> Logs |
| 500 error on every request | open Vercel -> Logs for the backend (errors are now printed there) |
| `DisallowedHost` in logs | add that hostname to `ALLOWED_HOSTS` |
| Admin login: "CSRF verification failed" | set `CSRF_TRUSTED_ORIGINS` to the backend address (with `https://`) |
| Refreshing `/dashboard` gives 404 | the frontend project's Root Directory is not `frontend` (so `vercel.json` is missed) |
| App crashes at start: "Set DJANGO_SECRET_KEY" | you forgot `DJANGO_SECRET_KEY` |

## Alternative for the backend: Render or Railway (always-on server)
Simpler to reason about than serverless. Same Neon database, same variables as above, plus:
- Build command: `pip install -r requirements.txt && python manage.py collectstatic --noinput`
- Start command: `gunicorn config.wsgi:application`
- Run `python manage.py migrate` before each deploy (their "pre-deploy command").
The frontend stays on Vercel; only `VITE_API_URL` changes to the new backend address.
