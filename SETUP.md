# Scan app — setup & deploy

The scan app is a small Flask web app. It runs on **SQLite locally** (zero setup)
and on **Postgres/Supabase in production**, hosted on **Railway**. Switching
between them is just the `DATABASE_URL` environment variable — the code and the
database tables are identical, and tables auto-create on first boot.

---

## Run it locally (to test with your scan gun on your own network)

```bash
pip install -r requirements.txt
SECRET_KEY=dev PYTHONPATH=src python -m lottery_tracker.web
# open http://localhost:5000  (or http://<your-computer-ip>:5000 from the gun)
```

First account you create becomes the **admin**. Then: **Start count → scan every
box → Finish & Save → Report**.

---

## Deploy to Railway + Supabase (production)

### 1. Supabase (the database)
1. Create a free account at supabase.com and a new project. Pick a strong DB password.
2. Project Settings → **Database** → **Connection string** → **URI**. Copy it.
   It looks like `postgresql://postgres:PASSWORD@db.xxxx.supabase.co:5432/postgres`.
3. That's it — you do **not** need to create any tables; the app creates them on
   first boot.

### 2. Railway (the app)
1. Create a free account at railway.app → **New Project → Deploy from GitHub repo**
   → pick `pritpnp/valley_lotto`.
2. Railway auto-detects the `Procfile`. Set these **Variables**:
   - `DATABASE_URL` = the Supabase URI from step 1
   - `SECRET_KEY` = a long random string (e.g. run `python -c "import secrets;print(secrets.token_hex(32))"`)
   - `REGISTER_CODE` = a code only you know (so random people can't sign up)
   - `DEFAULT_STORE` = `valley` (or your store name)
   - `SLOTS` = `48`  (boxes numbered 1..48)
3. Deploy. Railway gives you a public URL. Open it, register the first (admin)
   account with your `REGISTER_CODE`, and you're live.

### 3. Point the gun at it
Open the Railway URL on the tablet/phone at the counter, log in, and scan. Because
the gun types the barcode like a keyboard, no app install is needed.

---

## What I need from you to finish the production deploy
- [ ] A **Supabase** account + project, and its **`DATABASE_URL`** (or add me as a
      collaborator / paste the URI). *This is the only true blocker.*
- [ ] A **Railway** account connected to the `pritpnp/valley_lotto` repo.
- [ ] Choose the **`REGISTER_CODE`** and **`SECRET_KEY`** values (or let me generate them).
- [x] Box layout is **1–48** (`SLOTS=48`).

Once you have the Supabase `DATABASE_URL` and Railway connected, I can wire the
variables and walk the first deploy with you.

---

## Notes
- **Ticket prices / revenue** in the report come from the scraper's `data/state.json`
  (it already knows each game's price). Run the scraper once so revenue shows up.
- **Pack sizes** are learned from real scans automatically; `config.yaml`'s
  `pack_sizes` are just optional seeds (see that file).
- Run with a **single web worker** (the Procfile already does) — fine for one store.
