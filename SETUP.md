# Scan app — setup & deploy

The scan app is a small Flask web app. It runs on **SQLite locally** (zero setup)
and on **Postgres/Supabase in production**, hosted on **Render** (free plan). Switching
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

## Deploy to Render + Supabase (production)

### 1. Supabase (the database)
1. Create a free account at supabase.com and a new project. Pick a strong DB password.
2. Project Settings → **Database** → **Connection string** → **URI**. Copy it.
   It looks like `postgresql://postgres:PASSWORD@db.xxxx.supabase.co:5432/postgres`.
3. That's it — you do **not** need to create any tables; the app creates them on
   first boot.

### 2. Render (the app) — free
Railway's trial ended and it has no free plan, so the app runs on Render's free
plan instead. No credit card, 750 hours a month (more than a month around the
clock), and the data stays in Supabase, so changing hosts loses nothing.

1. Sign up at render.com with GitHub. Choose **New → Blueprint**, then pick the
   `pritpnp/Valley_Lotto` repo. Render reads `render.yaml` and fills in the rest.
2. It asks for five values. Copy them **exactly** from the old Railway service
   (Railway → the service → **Variables**. The Raw Editor shows them all at once):
   - `DATABASE_URL`: the Supabase connection string
   - `SECRET_KEY`: keep the same one so nobody is logged out
   - `REGISTER_CODE`
   - `DEFAULT_STORE`: **must match**, or counts go under a new, empty store
   - `SLOTS`
3. Choose **Apply**. The first build takes a few minutes. Render shows the
   address, something like `https://valley-lotto.onrender.com`.
4. In GitHub, go to repo **Settings → Secrets and variables → Actions → Variables**
   and set `APP_URL` to that address. Two jobs use it. One checks the site
   daily and emails you if it's down. The other keeps the site awake from 6am
   to midnight, so nobody waits for it to start up.
5. On the phone app, **press and hold anywhere on the page** to change the server
   address.

The free plan sleeps after 15 minutes with no visitors and takes about a minute
to wake. During store hours the wake-up job stops that. Late at night the first
load is slow.

### 3. Point the gun at it
Open the Render URL on the tablet/phone at the counter, log in, and scan. Because
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


---

## Security: Row Level Security on Supabase

**Short answer: leave RLS ON.** The app enables it automatically on every boot,
so there is nothing to do — but it is worth understanding why.

Supabase serves every table in the `public` schema through an auto-generated
REST API. With RLS off, anyone holding the project's anon key could read or write
store data directly and skip this app's permission checks entirely.

With RLS **on and no policies defined**, those API roles (`anon`,
`authenticated`) are denied outright, which is what we want: this data should
only be reachable through the app.

It does not lock the app out, because a table's owner bypasses RLS and the app
owns the tables it created. For the same reason the app never issues
`FORCE ROW LEVEL SECURITY` — that would apply the (non-existent) policies to the
owner too and cut the app off from its own data.

If Supabase's Table Editor shows "RLS disabled" on a table, redeploy — the next
boot turns it on. Do **not** add permissive policies to silence the warning;
"RLS on, zero policies" is the intended state here.
