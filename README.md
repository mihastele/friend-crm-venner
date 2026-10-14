# Venner — friend reminders PWA

Venner ("friends") reminds you to call your close friends, alerts you to
birthdays, and nudges you to stay away from people on your cutoff list.
Beautiful yellow dashboard, installable PWA with offline shell and local
browser alerts. No dependencies — Python 3 standard library only.

## Run it

```sh
cd /home/miha/dev/venner
python3 server.py
```

Then open **http://localhost:8765** in a browser.

Options: `python3 server.py --port 8080 --db venner.db --host 127.0.0.1`
The flags also read `HOST`, `PORT`, and `VENNER_DB` from the environment
(explicit flags win).

## Run it with Docker

```sh
cd /home/miha/dev/venner
docker compose up --build
```

Then open **http://localhost:8765** in a browser. Data lives in the
`venner-data` volume, so friends and accounts survive restarts.

To run on another port: `PORT=9000 docker compose up --build`, then open
**http://localhost:9000**. (`PORT` sets both the host mapping and the port
the app listens on inside the container; copy `.env.example` to `.env` to
make it stick.)

First run creates a fresh database. Sign in as:

- username: `admin`
- password: `admin-1234` (temporary — you pick your own on first login)

## How it works

- **Invite-only.** Registration is disabled. The admin adds people and hands
  each one a temporary password. A lost password means the admin assigns a
  new temporary one; signing in with it forces a password change first.
- **Friend list.** Each user keeps their own friends with birthday, phone,
  email, notes, and a list assignment.
- **Lists & policies.** Built-ins: Close friends (contact weekly),
  Acquaintances (contact monthly), Celebrate (birthdays only), Cutoff list
  and Blocklist (stay-away reminders). Every list has one editable policy:
  remind-to-contact or remind-to-avoid, how often, the reminder text
  (`{name}`, `{birthday}`, `{phone}` placeholders), and a birthday-alert
  toggle. "Contacted" marks reset the contact clock.
- **Imports.** Birthday calendar (`.ics`), contacts (`.vcf`/vCard or `.csv`),
  phone list (`.csv`/`.txt`, one `name, phone` per line).
- **Merge review.** When an imported name + surname matches someone you
  already have, both entries wait in Merge review. Confirm, and birthday +
  phone join into one friend; decline, and they stay separate.
- **Reminders.** Generated from list policies plus birthdays in the next
  14 days. Turn on "browser alerts" for OS-level notifications; the
  service worker keeps the shell working offline.
- Try it fast: the Imports tab links sample files that deliberately
  overlap (`Kari Nordmann` is in both the calendar and contacts).

## Layout

```
server.py          HTTP API + static hosting (stdlib only)
store.py           SQLite schema, users, friends, lists, merges, reminders engine
parsers.py         ICS / vCard / CSV / phone-list parsers
public/            PWA: index.html, styles.css, app.js, manifest.json,
                   service-worker.js, icons/, samples/
venner.db          created on first run (SQLite)
```

API overview: `POST /api/login`, `POST /api/change-password`,
`/api/admin/users…`, `/api/friends…`, `/api/categories…`,
`POST /api/import/{birthdays,contacts,phones}`,
`/api/merge-candidates…`, `/api/notifications…`. Auth is a signed
bearer token; passwords are PBKDF2-hashed.

Regression check: `python3 -m unittest tests.test_frontend_boots`
(boots the app and asserts zero JS errors; needs Chrome/Chromium).
