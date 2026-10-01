"""The audit that runs after every scrape: every game, every rule."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import audit_all  # noqa: E402


def test_every_game_in_the_saved_data_obeys_the_rules():
    raw = json.loads((ROOT / "data" / "state.json").read_text())
    f = audit_all.Findings()
    audit_all.check_version("current", raw, None, f)
    code = {k: v for k, v in f.to_dict().items() if k.startswith("CODE")}
    assert code == {}


def test_a_broken_rule_fails_the_run(monkeypatch):
    real = audit_all.rate
    def broken(g, w=None):
        if g.game_number == "1693":
            raise ValueError("simulated bug")
        return real(g, w)
    monkeypatch.setattr(audit_all, "rate", broken)
    monkeypatch.setattr(sys, "argv", ["audit_all", "--days", "1", "--no-app", "--ref", "HEAD",
                                      "--with-local", "--fail-on-code"])
    with pytest.raises(SystemExit) as e:
        audit_all.main()
    assert e.value.code == 1


def test_data_problems_alone_never_fail_the_run(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["audit_all", "--days", "1", "--no-app", "--ref", "HEAD",
                                      "--with-local", "--fail-on-code"])
    audit_all.main()                       # returns normally
    assert "CODE problems: 0" in capsys.readouterr().out
