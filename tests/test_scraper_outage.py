"""PA's website being down is their outage, not this program being broken."""

import json
import pathlib

import pytest

from lottery_tracker import cli, fetch
from lottery_tracker.web.app import create_app


def test_a_timeout_exits_try_again_later_not_failure(monkeypatch, capsys):
    def timed_out(*a, **k):
        raise fetch.FetchError("Read timed out. (read timeout=60)")
    monkeypatch.setattr(fetch, "fetch_active", timed_out)

    code = cli.run(["--config", "config.yaml"])
    assert code == 75                       # EX_TEMPFAIL: try again later
    err = capsys.readouterr().err
    assert "did not answer" in err and "try again" in err


def test_a_real_break_still_fails_loudly(monkeypatch):
    """Only an unreachable site is forgiven — a broken parse must still shout."""
    monkeypatch.setattr(fetch, "fetch_active", lambda *a, **k: "<html>nope</html>")
    monkeypatch.setattr(fetch, "fetch_remaining", lambda *a, **k: "<html>nope</html>")
    monkeypatch.setattr(fetch, "fetch_sales_ended", lambda *a, **k: "<html>nope</html>")
    with pytest.raises(Exception):
        cli.run(["--config", "config.yaml"])


def test_it_waits_longer_before_giving_up():
    """A twice-a-day job can afford to be patient with a slow server."""
    import inspect
    sig = inspect.signature(fetch.fetch)
    assert sig.parameters["retries"].default >= 5
    assert sig.parameters["timeout"].default >= 60


def test_the_workflow_treats_that_code_as_a_warning():
    wf = pathlib.Path(".github/workflows/track.yml").read_text()
    assert "75)" in wf and "::warning::" in wf
    # and doesn't commit or raise an issue off a run that fetched nothing
    assert wf.count("steps.run.outputs.exit_code != '75'") == 2


# --- the app says so when the numbers stop being refreshed -------------------

@pytest.fixture()
def client(tmp_path):
    a = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'s.db'}", "SECRET_KEY": "k",
                    "DEFAULT_STORE": "t", "SLOTS": "2", "REGISTER_CODE": None})
    a.config.update(TESTING=True)
    c = a.test_client()
    c.post("/register", data={"username": "prit", "password": "pw"})
    return c


def _age(client, captured_at):
    """Ask the app how old it thinks a given capture time is."""
    from lottery_tracker.web import app as appmod
    return captured_at


def test_stale_data_is_called_out_on_the_dashboard(client, monkeypatch):
    from lottery_app import pa_data
    real = pa_data.load_catalog

    def old_catalog(path):
        cat = real(path)
        cat.captured_at = "2020-01-01T00:00:00Z"
        return cat
    monkeypatch.setattr(pa_data, "load_catalog", old_catalog)

    html = client.get("/dashboard").data.decode()
    assert "haven&#39;t refreshed" in html or "haven't refreshed" in html
    assert "days" in html


def test_fresh_data_says_nothing(client, monkeypatch):
    from datetime import datetime, timezone
    from lottery_app import pa_data
    real = pa_data.load_catalog

    def fresh(path):
        cat = real(path)
        cat.captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return cat
    monkeypatch.setattr(pa_data, "load_catalog", fresh)

    html = client.get("/dashboard").data.decode()
    assert "haven't refreshed" not in html and "haven&#39;t refreshed" not in html
    assert "PA data: today" in html
