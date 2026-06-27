# Valley Lotto — multi-store web app

A self-hostable dashboard where **each store has its own login, its own staff, and
its own inventory**, while they all share **one Pennsylvania scratch-off database**
(the data the scraper produces twice a day). Each store sees only the games it
carries, with the same KEEP / SEND-BACK calls and metrics as the static report,
plus a catalog-wide "best games to bring in" board.

## How it fits together

```
  PA Lottery pages
        │  (GitHub Actions, twice a day)
        ▼
  lottery_tracker  ──writes──▶  data/state.json   ← ONE shared PA catalog
        │                              │
        │                              │ read-only
        ▼                              ▼
  data/app.db (SQLite)  ◀──────  lottery_app (FastAPI)
   stores · users · inventory          │
                                        ▼
                              per-store dashboards
```

- **Shared data:** `data/state.json` — every store reads the same catalog.
- **Per store:** `data/app.db` — stores, users (scrypt-hashed passwords), and the
  list of game numbers each store carries.

## Roles

| Role   | Can do |
|--------|--------|
| admin  | everything across every store; create stores; switch store view |
| owner  | manage their own store's staff + inventory |
| staff  | edit their own store's inventory; view the dashboard |

## Run it locally

```bash
pip install -r requirements.txt -r requirements-app.txt

# 1. create the database + first store + an admin login (prompts for password)
PYTHONPATH=src python -m lottery_app.seed \
    --store "Valley Mart - Main St" --username owner --role admin --import-config

# 2. start the server
export LOTTO_SECRET="$(python -c 'import secrets;print(secrets.token_hex(32))')"
PYTHONPATH=src uvicorn lottery_app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 and sign in.

## Run it on the Beelink (Docker)

```bash
echo "LOTTO_SECRET=$(openssl rand -hex 32)" > .env
docker compose up -d --build
docker compose exec web python -m lottery_app.seed \
    --store "Main St" --username owner --password 'strong-pass' --role admin --import-config
```

Add more stores from the **Manage** page once you're signed in as admin.

### Expose it to the other stores (no open ports)

Create a Cloudflare Tunnel in the Cloudflare dashboard, put its token in `.env`
as `TUNNEL_TOKEN=...`, then:

```bash
docker compose --profile tunnel up -d
```

The Beelink comfortably runs both the scraper and this app (two small
containers). A single Beelink can host many such services; this one is tiny
(SQLite + one Python process).

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `LOTTO_SECRET` | dev placeholder | signs session cookies — **set a real one in production** |
| `LOTTO_DB` | `data/app.db` | the app's SQLite database |
| `LOTTO_STATE` | `data/state.json` | the scraper's shared PA catalog |
| `LOTTO_CONFIG` | `config.yaml` | thresholds + bring-in tuning |

## Keeping the PA data fresh

The web app only **reads** `data/state.json`. Keep the existing GitHub Actions
scraper running (it commits `state.json` twice a day), and have the Beelink pull
the repo on a schedule — or run the scraper directly on the Beelink — so the
dashboards always reflect the latest PA numbers.
