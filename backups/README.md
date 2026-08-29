# Backups

A copy of the store database, taken nightly and kept here for 30 nights.

Every file is **encrypted**, and that is not optional: this repository is public.
A plain dump would put staff names, store addresses, retailer numbers, every sale
and every password hash in front of anyone who found the repo.

    valley-lotto-2026-08-29.sql.gpg

## What you need

Two repository secrets — Settings → Secrets and variables → Actions:

| Secret | What it is |
|--------|------------|
| `DATABASE_URL` | the Supabase connection string (Session pooler), same one Railway uses |
| `BACKUP_PASSPHRASE` | a long random phrase you choose |

**The passphrase is the only way to read a backup.** Nobody can recover it for
you — not GitHub, not Supabase, not me. Keep it wherever you keep the database
password, and not only in GitHub: a passphrase stored solely in the repo whose
backups it protects is no protection at all.

## Reading a backup

Download the file you want, then:

    gpg --decrypt valley-lotto-2026-08-29.sql.gpg > restore.sql

It will ask for the passphrase. The result is ordinary SQL you can open and read.

## Restoring

Into a fresh Supabase project (or the same one after a mishap):

    # 1. get the connection string of the project you're restoring INTO
    export TARGET="postgresql://postgres.xxxx:PASSWORD@aws-0-us-east-1.pooler.supabase.com:5432/postgres"

    # 2. decrypt
    gpg --decrypt valley-lotto-2026-08-29.sql.gpg > restore.sql

    # 3. restore
    psql "$TARGET" -f restore.sql

The dump is taken with `--clean --if-exists`, so it drops and recreates each
table as it goes. Restoring into a database that already has data **replaces**
it. Restore into a fresh project first if you want to look before committing.

If you don't have `psql` locally, Supabase's dashboard has a SQL editor — but a
full restore is usually too large to paste. Installing the Postgres client
(`brew install libpq` on a Mac, `apt install postgresql-client` on Linux) is the
easier path.

## What it does not cover

This is the database only — your counts, boxes, packs, staff, stores and the
audit trail. The code lives in git already, and the PA catalog is fetched fresh
twice a day, so neither needs a backup.

## When something looks wrong

The workflow refuses to save a dump that doesn't contain the tables it expects,
so a half-finished or empty backup never quietly replaces a good one. If it
fails, the previous nights are still here — nothing is deleted until a new
backup has been written successfully.
