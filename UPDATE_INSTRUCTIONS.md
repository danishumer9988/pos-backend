# POS update v6 - find out why the email is not sent

Copy these 3 files over your project, then **stop and start** the backend (Ctrl+C, then `python manage.py runserver`).
Editing `.env` does NOT trigger Django's auto-reload - you must restart it by hand after every change to `.env`.

## Step 1 - run the checker
    python manage.py test_email youraddress@gmail.com

Read the first lines. They tell you exactly what is wrong, for example:
- `.env : NOT FOUND` -> the file must be named exactly `.env` and sit next to `manage.py`.
- `Found .env.txt instead` -> Windows hid the ".txt". Rename it:  `Rename-Item .env.txt .env`
- `EMAIL_HOST is not in that file` -> add the mail lines below.
- `Backend : console` -> still no real sending (see above).

## Step 2 - the .env lines (same folder as manage.py)
    EMAIL_HOST=smtp.gmail.com
    EMAIL_PORT=587
    EMAIL_HOST_USER=youraddress@gmail.com
    EMAIL_HOST_PASSWORD=your16letterapppassword

Gmail needs an App Password (Google account > Security > 2-Step Verification > App passwords).

## What changed
- `.env` loader now handles Windows Notepad files (hidden BOM mark, CRLF, `# comments` after a value, `export`).
- `test_email` now reports whether `.env` was found, where, and how many EMAIL_* settings it holds.
- 3 new tests for the loader.

## Files
- config/settings.py
- pos/management/commands/test_email.py
- pos/tests.py
