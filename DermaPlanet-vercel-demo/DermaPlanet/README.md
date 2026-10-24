# DermaPlanet

Dermatology clinic website with online appointment booking, patient accounts and an admin panel.
Built with Flask and SQLite. **No database server to install**: SQLite ships with Python, and the
database file is created automatically the first time you run the app.

## Run it locally

```bash
# 1. (optional but recommended) create a virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # macOS / Linux

# 2. install dependencies
pip install -r requirements.txt

# 3. start the app
python app.py
```

Open http://127.0.0.1:5000

On first start the app creates `instance/dermaplanet.db`, adds two sample doctors and creates an admin account:

| | |
|---|---|
| Admin login page | http://127.0.0.1:5000/admin-login |
| Email | `admin@dermaplanet.com` |
| Password | `Admin@12345` (change it from **Profile** after your first login) |

To use your own admin email/password or other settings, copy `.env.example` to `.env` and edit it
**before the first run** (the admin is only created when no admin exists yet).

## Features

**Patients**
- Register / log in, edit profile, change password
- Book appointments by choosing a doctor, a date and a free time slot (slots already taken disappear)
- Dashboard with upcoming visits and counts; full booking history with status; cancel a booking
- Contact form

**Admin** (login: `/admin-login`, also linked as "Admin" in the site header)
- Appointments: status chips with counts, search and filters, approve / reject / complete / cancel / delete
- Appointment details page: reschedule to another doctor, date or free slot, and leave a note the patient can see
- Export the (filtered) appointment list to CSV for Excel or Google Sheets
- Doctors: add / edit / hide / delete with photo upload (photos are resized automatically)
- Patients: list, profile page with full booking history, delete a patient and their data
- Messages: contact-form inbox (mark read, delete)
- Staff: create more admin accounts, remove them (you cannot remove yourself or the last admin)
- Profile: change your own name and password

**Look and feel: "Lavender calm" in green**
- Poppins font, calm green palette and a soft wave-line background texture (pure CSS/SVG, no image files)
- **Light / dark toggle** (sun/moon button in the header). It remembers the choice on that device, follows the
  phone or computer setting on a first visit, and applies before the page paints so dark mode never flashes white
- To change colours, edit the variables in the two blocks at the top of `static/css/style.css`
  (`:root` for light, `:root[data-theme="dark"]` for dark). To tune the texture, edit `--texture` (the stroke
  opacity controls how strong it is)

**Quality**
- Responsive on phones, tablets and desktops (tables turn into cards on small screens)
- Double-booking is impossible (enforced by a database index, not just the UI)
- CSRF protection on every form, hashed passwords, login rate limiting, safe redirects,
  security headers incl. a strict Content-Security-Policy, secure file-upload checks
- Clinic hours: Mon-Sat 10:00-18:00, lunch 13:00-14:00, 30-minute slots, bookable up to 60 days ahead.
  Change these constants at the top of the "Scheduling" part of `app.py`.

## Tests

```bash
python -m unittest -v test_app
```

## Project layout

```
app.py              routes, database, validation, security
templates/          base.html + pages, admin/ and partials/
static/css/style.css  one responsive stylesheet with light and dark themes
static/js/theme.js    sets the saved theme before the page paints
static/js/script.js   nav menu, theme toggle, slot picker, confirmations
static/images/      optimised photos (about 330 KB total, was 4.7 MB)
instance/           database + secret key are created here (git-ignored)
uploads/            uploaded doctor photos, created on demand (git-ignored)
```

## Deploy as a demo on Vercel (no database setup)

No environment variables or external database are needed.

1. Push this folder to GitHub and import it in Vercel (Flask is detected from `app.py`; no build command).
2. Deploy. Log in at `/admin-login` with `admin@dermaplanet.com` / `Admin@12345`.

On Vercel the SQLite file and uploaded photos live in `/tmp`, so this is a **demo**: data
(accounts, bookings, new doctors) is temporary and resets when Vercel starts a new instance.
For real use, host on a machine with a persistent disk (see below). Optional: set `SECRET_KEY`
and `ADMIN_PASSWORD` in Vercel's environment variables.

## Other hosts

SQLite is a file, so a host that keeps its disk between restarts (a VPS, PythonAnywhere,
Render/Railway with a persistent disk) works. Point `DATABASE_PATH` at that disk, set `SECRET_KEY`,
set `SESSION_COOKIE_SECURE=1` and run `gunicorn app:app`.

## Notes

- Locally the data lives in `instance/dermaplanet.db`.
- The first load fetches fonts from Google Fonts; offline, the site falls back to system fonts.
- Booking requests are stored and shown in the app. Email/SMS notifications are not included.
