"""Tests for the guided count-session capture flow."""

import pytest

from lottery_tracker.session import CountSession, standard_slots
from lottery_tracker.scans import load_scans


def test_standard_slots_numeric_default():
    s = standard_slots()
    assert len(s) == 48
    assert s[0] == "1" and s[-1] == "48"


def test_standard_slots_lettered_optional():
    s = standard_slots((("A", 24), ("B", 24)))
    assert s[0] == "A1" and s[23] == "A24" and s[24] == "B1" and s[-1] == "B24"


def test_auto_advance_through_the_walk():
    sess = CountSession(slots=["A1", "A2", "A3"])
    assert sess.current_slot == "A1"
    step = sess.scan("1750-0091798-010", at="2026-08-08T08:00:00Z")
    assert step.ok and step.slot == "A1" and step.next_slot == "A2"
    sess.scan("1744-0100200-005", at="2026-08-08T08:00:05Z")
    assert sess.current_slot == "A3"
    assert sess.progress() == (2, 3)


def test_bad_barcode_is_rejected_and_stays_put():
    sess = CountSession(slots=["A1", "A2"])
    step = sess.scan("not-a-barcode")
    assert step.ok is False
    assert sess.current_slot == "A1"          # did NOT advance
    assert step.next_slot == "A1"


def test_back_redoes_the_previous_box():
    sess = CountSession(slots=["A1", "A2", "A3"])
    sess.scan("1750-0091798-010", at="t1")
    assert sess.current_slot == "A2"
    sess.back()
    assert sess.current_slot == "A1"
    sess.scan("1750-0091798-011", at="t2")    # corrected value
    assert sess.entries["A1"].ticket == 11


def test_rescan_fixes_a_box_without_losing_place():
    sess = CountSession(slots=["A1", "A2", "A3", "A4"])
    sess.scan("1750-0091798-010", at="t1")    # A1
    sess.scan("1744-0100200-005", at="t2")    # A2
    sess.scan("1780-0088010-002", at="t3")    # A3  -> now at A4
    assert sess.current_slot == "A4"
    step = sess.rescan("A2", "1744-0100200-009", at="t4")   # fix A2 mid-flow
    assert step.ok and sess.entries["A2"].ticket == 9
    assert sess.current_slot == "A4"          # place preserved


def test_duplicate_ticket_warns():
    sess = CountSession(slots=["A1", "A2"])
    sess.scan("1750-0091798-010", at="t1")
    step = sess.scan("1750-0091798-010", at="t2")   # same exact ticket in A2
    assert step.ok is True
    assert step.warning and "wrong box" in step.warning


def test_skip_and_goto_fill_later():
    sess = CountSession(slots=["A1", "A2", "A3"])
    sess.scan("1750-0091798-010", at="t1")    # A1
    sess.skip()                                # skip A2
    sess.scan("1780-0088010-002", at="t3")    # A3
    assert sess.pending() == ["A2"]
    sess.goto("A2")
    assert sess.current_slot == "A2"
    sess.scan("1744-0100200-005", at="t4")
    assert sess.is_complete()


def test_game_per_box_updates_not_locked():
    # Count 1: A1 holds game 1750. Count 2: A1 now holds 1744 (product swapped).
    s1 = CountSession(slots=["A1"], session="morning")
    s1.scan("1750-0091798-010", at="t1")
    assert s1.entries["A1"].game_number == "1750"
    s2 = CountSession(slots=["A1"], session="night")
    s2.scan("1744-0100200-005", at="t2")
    assert s2.entries["A1"].game_number == "1744"     # updated freely


def test_commit_persists_in_box_order(tmp_path):
    path = tmp_path / "scans.json"
    sess = CountSession(slots=["A1", "A2"], session="morning")
    sess.scan("1750-0091798-010", at="t1")
    sess.scan("1744-0100200-005", at="t2")
    sess.commit(path)

    log = load_scans(path)
    assert [s.slot for s in log.scans] == ["A1", "A2"]
    assert [s.session for s in log.scans] == ["morning", "morning"]
    with pytest.raises(RuntimeError):
        sess.commit(path)                      # no double-commit
