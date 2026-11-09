"""
quota.py

Naukri caps applications at **50 per account per rolling 24 hours**. Manual
applies, bulk applies (5 at a time on Recommended/Saved/Alert pages) and
automated applies all count toward the same bucket, and the window resets on a
rolling basis rather than at midnight. Only *submitted* applications count —
viewing or saving a job does not consume quota.

Because the limit is account-wide rather than per-device, a local CSV can never
be the source of truth on its own: it cannot see applications you submitted from
your phone or from the website. So we count from two places and take the larger:

    1. Naukri's own application history  (authoritative, sees everything)
    2. Local CSV ledgers                 (fallback if the history call fails)

``used = max(naukri_24h, ledger_24h)`` is deliberately conservative. Over-
estimating costs us a few applications today; under-estimating risks a Naukri
cooldown or a flagged account, which costs far more.

Usage::

    from src.client.quota import remaining, describe

    print(describe(client))        # human-readable quota banner
    budget = remaining(client)     # 45 by default (50 minus a 5-application reserve)
"""

import csv
import os
from datetime import datetime, timedelta, timezone


# Naukri's published account-wide cap.
DAILY_CAP = 50

# Keep headroom so manual applies during the day are never refused. The agent
# will not spend the last few applications of the window by itself.
SAFETY_RESERVE = 5

# Naukri resets on a rolling 24h window, not at midnight.
WINDOW_HOURS = 24

# Ledgers the agent reads for dedupe/quota. The first is written by this agent,
# the second is the legacy file written by apply_agent.py (read-only for us).
JEV_LEDGER = "jev_applied_jobs.csv"
LEGACY_LEDGER = "applied_jobs.csv"
DEFAULT_LEDGERS = (JEV_LEDGER, LEGACY_LEDGER)

# Naukri's history payload uses different date shapes across endpoints and
# platforms; try the common ones in order. Unparseable values are handled
# conservatively by the callers below.
DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%d %b %Y, %I:%M %p",
    "%d %b %Y %H:%M:%S",
    "%d %b %Y",
    "%Y-%m-%d",
)


def utcnow():
    """Naive UTC 'now', matching the rest of the codebase."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_history_dt(value):
    """
    Parse a Naukri timestamp into a naive UTC datetime, or return None.

    Accepts ISO-8601 (with or without ``Z``/offset/fractional seconds), common
    Indian display formats, and epoch seconds/milliseconds.
    """
    if value is None:
        return None

    # Epoch timestamps (seconds or milliseconds).
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e11:  # milliseconds
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    # Numeric strings are epoch too.
    if text.isdigit() and len(text) >= 10:
        return parse_history_dt(float(text))

    candidate = text.replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        pass

    for fmt in DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    return None


def _within_window(stamp, since, now):
    """True when ``stamp`` falls inside [since, now + 1h slack]."""
    if stamp is None:
        return False
    return since <= stamp <= now + timedelta(hours=1)


def _item_timestamps(item):
    """Collect every plausible timestamp from one history record."""
    stamps = []

    for status in item.get("status") or []:
        if isinstance(status, dict):
            stamps.append(parse_history_dt(status.get("dateTime")))

    for key in ("appliedDate", "applyDate", "createdDate", "dateTime", "updatedDate"):
        if item.get(key):
            stamps.append(parse_history_dt(item.get(key)))

    return stamps


# ----------------------------------------------------------------------
# Counting: Naukri (authoritative)
# ----------------------------------------------------------------------

def applied_in_window_naukri(client, hours=WINDOW_HOURS, page_size=100):
    """
    Count applications Naukri reports inside the rolling window.

    ``client`` is a logged-in ``NaukriLoginClient``. Returns ``(count, company)``
    where ``company`` is the number of applications currently open — useful for
    the run banner.

    Conservatism rules, in order:
      * An item with at least one timestamp inside the window counts.
      * An item with timestamps that are all unparseable also counts (we cannot
        prove it is outside the window).
      * If *no* timestamp anywhere could be parsed, every returned row counts.

    The history endpoint accepts a ``days`` lookback. We ask for 2 days so a
    full rolling 24h window is always covered even across a timezone boundary,
    then filter precisely by timestamp.
    """
    raw = client.get_application_history(
        page_size=page_size, days=2, page_number=1, mobile=False
    )
    items = raw.get("applyDetails") or []

    if not items:
        return 0, 0

    now = utcnow()
    since = now - timedelta(hours=hours)

    parsed_any = False
    in_window = 0
    total = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        total += 1

        stamps = _item_timestamps(item)
        real = [s for s in stamps if s is not None]
        if real:
            parsed_any = True

        if any(_within_window(s, since, now) for s in real):
            in_window += 1
        elif not real:
            # Unparseable — assume it is recent rather than risk over-applying.
            in_window += 1

    open_now = sum(
        1 for item in items
        if isinstance(item, dict) and str(item.get("isOpen", "")).lower() == "true"
    )

    if not parsed_any:
        # Nothing was parseable: treat every row as inside the window.
        return total, open_now

    return in_window, open_now


# ----------------------------------------------------------------------
# Counting: local ledgers (fallback)
# ----------------------------------------------------------------------

def _read_ledger_rows(path):
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []


def applied_in_window_ledger(paths, hours=WINDOW_HOURS):
    """
    Count applications recorded in the local CSV ledgers inside the window.

    Rows without a parsable ``applied_at`` are counted, for the same
    conservative reason as above.
    """
    if isinstance(paths, str):
        paths = (paths,)

    now = utcnow()
    since = now - timedelta(hours=hours)
    total = 0
    seen_ids = set()

    for path in paths or ():
        for row in _read_ledger_rows(path):
            job_id = (row.get("job_id") or "").strip()
            if not job_id:
                continue

            # A job can appear in both ledgers; count it once.
            dedupe_key = f"{path}:{job_id}"
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)

            stamp = parse_history_dt(
                row.get("applied_at") or row.get("appliedAt") or row.get("date")
            )
            if stamp is None:
                total += 1
            elif _within_window(stamp, since, now):
                total += 1

    return total


# ----------------------------------------------------------------------
# Combined view
# ----------------------------------------------------------------------

def used_in_window(client=None, ledger_paths=DEFAULT_LEDGERS, hours=WINDOW_HOURS):
    """
    Return a dict describing how much of the quota is already spent.

        {
          "naukri": 12,     # authoritative count (0 if the call failed)
          "ledger": 12,     # local CSV count
          "used":   12,     # max() of the two — the number we act on
          "source": "naukri" | "ledger" | "none",
          "errors": [...]   # non-fatal problems, for the run banner
        }
    """
    errors = []
    naukri_count = 0
    ledger_count = 0

    if client is not None:
        try:
            naukri_count, _open_now = applied_in_window_naukri(client, hours=hours)
        except Exception as exc:
            errors.append(f"history unavailable ({exc})")
            naukri_count = 0

    try:
        ledger_count = applied_in_window_ledger(ledger_paths, hours=hours)
    except Exception as exc:
        errors.append(f"ledger unreadable ({exc})")

    used = max(naukri_count, ledger_count)

    if naukri_count >= ledger_count and naukri_count > 0:
        source = "naukri"
    elif ledger_count > 0:
        source = "ledger"
    else:
        source = "none"

    return {
        "naukri": naukri_count,
        "ledger": ledger_count,
        "used": used,
        "source": source,
        "errors": errors,
    }


def remaining(client=None, cap=DAILY_CAP, reserve=SAFETY_RESERVE,
              hours=WINDOW_HOURS, ledger_paths=DEFAULT_LEDGERS, state=None):
    """
    Applications the agent may still submit right now.

    Never negative. ``reserve`` keeps headroom so you can still apply manually
    later in the same window without hitting Naukri's ceiling.
    """
    info = state or used_in_window(
        client=client, ledger_paths=ledger_paths, hours=hours
    )
    return max(0, cap - info["used"] - reserve)


def describe(client=None, cap=DAILY_CAP, reserve=SAFETY_RESERVE, state=None,
             ledger_paths=DEFAULT_LEDGERS):
    """One-line, human-readable quota banner for the terminal."""
    info = state or used_in_window(client=client, ledger_paths=ledger_paths)
    budget = max(0, cap - info["used"] - reserve)

    text = (
        f"used {info['used']}/{cap} in last {WINDOW_HOURS}h "
        f"(naukri={info['naukri']}, ledger={info['ledger']}, "
        f"source={info['source']}) | reserve={reserve} | "
        f"usable now={budget}"
    )
    if info["errors"]:
        text += f" | warnings: {'; '.join(info['errors'])}"
    return text


# ----------------------------------------------------------------------
# Dedupe: never apply to the same job twice
# ----------------------------------------------------------------------

def applied_job_ids_naukri(client, days=90, page_size=100):
    """
    Job IDs Naukri already has an application for.

    Catches applications submitted from the website or phone, which the local
    CSV cannot see. Returns an empty set on any failure — dedupe is a safety net,
    not a reason to abort the run.
    """
    try:
        raw = client.get_application_history(
            page_size=page_size, days=days, page_number=1, mobile=False
        )
    except Exception:
        return set()

    ids = set()
    for item in raw.get("applyDetails") or []:
        if isinstance(item, dict):
            job_id = item.get("jobId")
            if job_id:
                ids.add(str(job_id))
    return ids


def applied_job_ids_ledger(paths=DEFAULT_LEDGERS):
    """Job IDs recorded in the local CSV ledgers."""
    if isinstance(paths, str):
        paths = (paths,)

    ids = set()
    for path in paths or ():
        for row in _read_ledger_rows(path):
            job_id = (row.get("job_id") or "").strip()
            if job_id:
                ids.add(job_id)
    return ids


def applied_job_ids(client=None, ledger_paths=DEFAULT_LEDGERS, days=90):
    """Union of Naukri history and both local ledgers."""
    ids = applied_job_ids_ledger(ledger_paths)
    if client is not None:
        ids |= applied_job_ids_naukri(client, days=days)
    return ids


# ----------------------------------------------------------------------
# Writing the ledger
# ----------------------------------------------------------------------

JEV_LEDGER_FIELDS = [
    "job_id",
    "title",
    "company",
    "role_family",
    "jev_score",
    "confidence",
    "applied_at",
    "quota_used",
]


def record_application(job_id, title, company, role_family="", jev_score="",
                       confidence="", ledger=JEV_LEDGER, quota_used=""):
    """
    Append one successful application to this agent's own ledger.

    A separate file is used rather than ``applied_jobs.csv`` because
    ``apply_agent.py`` writes that one with a fixed four-column ``DictWriter``;
    adding columns there would raise ``ValueError: dict contains fields not in
    fieldnames`` for its own rows. Reading both is safe, writing only ours is not.
    """
    file_exists = os.path.exists(ledger)

    try:
        with open(ledger, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=JEV_LEDGER_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "job_id": job_id,
                "title": title,
                "company": company,
                "role_family": role_family,
                "jev_score": jev_score,
                "confidence": confidence,
                "applied_at": utcnow().isoformat(),
                "quota_used": quota_used,
            })
        return True
    except OSError:
        return False


if __name__ == "__main__":
    # Sanity check for the timestamp parser: python3 -m src.client.quota
    samples = [
        "2026-11-08T10:30:00.000Z",
        "2026-11-08T10:30:00+05:30",
        "2026-11-08T10:30:00",
        "2026-11-08 10:30:00",
        "08/11/2026 10:30:00",
        "08 Nov 2026, 10:30 AM",
        "08 Nov 2026",
        "2026-11-08",
        1762595400000,
        "1762595400",
        "",
        None,
        "not a date",
    ]

    for sample in samples:
        print(f"{str(sample)!r:32} -> {parse_history_dt(sample)}")
