"""
test_quota.py

Offline tests for the application-quota logic.

Run from the repo root:

    python3 src/tests/test_quota.py

These are plain asserts (no pytest dependency) because the project does not
ship a test runner. Naukri's 50/24h cap is the one thing that must never be
miscalculated, so the riskiest parts — timestamp parsing and the rolling-window
count — are pinned here.
"""

import csv
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.client import quota


def test_parse_formats():
    cases = {
        "2026-11-08T10:30:00.000Z": (2026, 11, 8, 10, 30),
        "2026-11-08T10:30:00": (2026, 11, 8, 10, 30),
        "2026-11-08 10:30:00": (2026, 11, 8, 10, 30),
        "08/11/2026 10:30:00": (2026, 11, 8, 10, 30),
        "08 Nov 2026, 10:30 AM": (2026, 11, 8, 10, 30),
        "2026-11-08": (2026, 11, 8, 0, 0),
    }
    for raw, expected in cases.items():
        parsed = quota.parse_history_dt(raw)
        assert parsed is not None, f"failed to parse {raw!r}"
        assert (
            parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute
        ) == expected, f"{raw!r} -> {parsed}"

    # Offset timestamps must be normalised to UTC.
    offset = quota.parse_history_dt("2026-11-08T10:30:00+05:30")
    assert (offset.hour, offset.minute) == (5, 0), offset

    # Junk is None, never an exception.
    for junk in ("", None, "not a date", "—"):
        assert quota.parse_history_dt(junk) is None


def test_epoch_values():
    seconds = quota.parse_history_dt("1762595400")
    millis = quota.parse_history_dt(1762595400000)
    assert seconds is not None and millis is not None
    assert abs((seconds - millis).total_seconds()) < 1


def _write_ledger(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["job_id", "title", "company", "applied_at"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_ledger_window():
    now = quota.utcnow()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ledger.csv")
        _write_ledger(path, [
            {"job_id": "a", "title": "t", "company": "c",
             "applied_at": (now - timedelta(hours=1)).isoformat()},
            {"job_id": "b", "title": "t", "company": "c",
             "applied_at": (now - timedelta(hours=23)).isoformat()},
            # Outside the rolling window on purpose.
            {"job_id": "c", "title": "t", "company": "c",
             "applied_at": (now - timedelta(hours=30)).isoformat()},
        ])

        assert quota.applied_in_window_ledger([path]) == 2
        assert quota.applied_in_window_ledger([path], hours=48) == 3
        assert quota.applied_job_ids_ledger([path]) == {"a", "b", "c"}


def test_unparsable_rows_count_conservatively():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "ledger.csv")
        _write_ledger(path, [
            {"job_id": "x", "title": "t", "company": "c", "applied_at": ""},
        ])
        # A row we cannot date must be assumed recent, never ignored.
        assert quota.applied_in_window_ledger([path]) == 1


def test_remaining_maths():
    state = {"naukri": 10, "ledger": 4, "used": 10, "source": "naukri", "errors": []}
    assert quota.remaining(state=state, cap=50, reserve=5) == 35

    # Never negative.
    heavy = {"naukri": 60, "ledger": 0, "used": 60, "source": "naukri", "errors": []}
    assert quota.remaining(state=heavy, cap=50, reserve=5) == 0

    # Max() of the two sources is what we act on.
    mixed = quota.used_in_window(client=None, ledger_paths=())
    assert mixed["used"] == 0 and mixed["source"] == "none"


def test_record_application_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, "jev_applied_jobs.csv")
        assert quota.record_application(
            job_id="123", title="Java Developer", company="Acme",
            role_family="backend", jev_score=91, confidence=0.88, ledger=ledger,
        ) is True

        rows = quota._read_ledger_rows(ledger)
        assert len(rows) == 1
        assert rows[0]["job_id"] == "123"
        assert rows[0]["jev_score"] == "91"
        assert quota.applied_job_ids_ledger([ledger]) == {"123"}
        # Freshly written rows are inside the window.
        assert quota.applied_in_window_ledger([ledger]) == 1


def test_missing_files_are_safe():
    assert quota.applied_in_window_ledger(["/nonexistent/nope.csv"]) == 0
    assert quota.applied_job_ids_ledger(["/nonexistent/nope.csv"]) == set()


def main():
    tests = [
        test_parse_formats,
        test_epoch_values,
        test_ledger_window,
        test_unparsable_rows_count_conservatively,
        test_remaining_maths,
        test_record_application_roundtrip,
        test_missing_files_are_safe,
    ]
    for test in tests:
        test()
        print(f"  PASS  {test.__name__}")
    print(f"\n{len(tests)} quota tests passed")


if __name__ == "__main__":
    main()
