"""The nightly backup, and the one thing it must never do.

This repository is public. A readable dump committed here would publish staff
names, store addresses, retailer numbers, every sale and every password hash.
These tests exist so that can't be lost in a later edit.
"""

import pathlib

WORKFLOW = pathlib.Path(".github/workflows/backup.yml").read_text()


def test_backups_are_encrypted():
    assert "--symmetric" in WORKFLOW and "AES256" in WORKFLOW
    assert "BACKUP_PASSPHRASE" in WORKFLOW


def test_it_refuses_to_run_without_a_passphrase():
    """Rather than quietly writing something readable."""
    assert 'missing="$missing BACKUP_PASSPHRASE"' in WORKFLOW
    assert "::error::Missing secret" in WORKFLOW


def test_the_plain_dump_is_destroyed_and_never_committed():
    assert "shred -u dump.sql" in WORKFLOW
    assert "An unencrypted .sql file is staged" in WORKFLOW
    assert "git add backups" in WORKFLOW      # only the backups directory


def test_a_half_finished_dump_cannot_replace_a_good_one():
    """A dump that didn't reach the data is worse than none, because it looks
    like one."""
    assert "CREATE TABLE public.$table" in WORKFLOW
    assert "refusing to save it" in WORKFLOW
    # and old backups are only pruned after a new one is written
    prune = WORKFLOW.index("Keep the last 30 nights")
    dump = WORKFLOW.index("Dump the database")
    assert dump < prune


def test_it_dumps_with_a_matching_postgres_version():
    """An older client refuses to dump a newer server — a confusing way to lose
    your backups."""
    assert "postgres:17-alpine" in WORKFLOW


def test_nothing_readable_is_checked_in():
    here = pathlib.Path("backups")
    loose = [p.name for p in here.glob("*") if p.suffix in (".sql", ".dump", ".csv")]
    assert loose == [], f"unencrypted backup files in the repo: {loose}"


def test_the_instructions_say_how_to_restore_and_warn_about_the_passphrase():
    doc = (pathlib.Path("backups/README.md")).read_text()
    assert "gpg --decrypt" in doc and "psql" in doc
    assert "only way to read a backup" in doc
    assert "replaces" in doc          # restoring over live data is destructive
