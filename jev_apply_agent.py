"""
jev_apply_agent.py

Automated Naukri job application agent powered by **Jev** (TypeSafe AI's System
One model). Jobs are scored against your own resume, and applications are only
spent on roles that clear a strict bar.

Pipeline
--------
1. Verify the Jev API key (cheap, fails before anything else happens).
2. Parse the resume PDF into a candidate profile (cached by file hash).
3. Log in to Naukri.
4. Read the live application quota from Naukri's own history endpoint.
5. Fetch jobs using resume-derived queries plus widened generic queries.
6. Free regex filters, then Jev scores everything that survives.
7. Apply strict gates; rank what passes; spend the remaining quota best-first.

Why the gates are strict
------------------------
Naukri caps applications at 50 per rolling 24h for the whole account, and manual
and bulk applies draw from the same pool. Applying is the scarce resource while
scoring costs a fraction of a paisa, so the agent deliberately scores far more
jobs than it applies to and then spends the budget on the highest-scoring ones.

Usage
-----
    python3 jev_apply_agent.py --quota-only    # just report the quota
    python3 jev_apply_agent.py --dry-run       # score + show, apply nothing
    python3 jev_apply_agent.py                 # live run
    python3 jev_apply_agent.py --limit 5 --threshold 80

Environment (.env)
------------------
    USERNAME, PASSWORD       Naukri credentials
    JEV_API_KEY              TypeSafe key (TYPESAFE_API_KEY also accepted)
    JEV_MODEL                default jev-latest
    JEV_MIN_APPLY_SCORE      default 75
    JEV_RESUME_PATH          default resume.pdf
"""

import argparse
import os
import sys
import time
from datetime import datetime

from colorama import Fore, Style
from dotenv import load_dotenv

from src.client import quota, session_store
from src.client.jev_client import JevClient, JevError
from src.client.jev_matcher import JevJobMatcher
from src.client.job_client import NaukriJobClient
from src.client.naukri_client import NaukriLoginClient
from src.client.resume_profile import DEFAULT_CACHE_FILE, load_resume_profile
from src.utils import console as ui


load_dotenv()


# ----------------------------------------------------------------------
# Search configuration
#
# Tier A is derived from the resume itself (Java / Spring Boot / React stack),
# so searching matches what the PDF actually says. Tier B widens the net with
# generic engineering terms; Jev filters the noise afterwards, which is cheap.
# ----------------------------------------------------------------------

RESUME_QUERIES = [
    "Java Developer",
    "Spring Boot Developer",
    "Full Stack Developer",
    "React Developer",
    "Backend Developer",
    "Java Backend Engineer",
    "Software Engineer Java",
    "Microservices Developer",
]

WIDENED_QUERIES = [
    "Software Engineer",
    "SDE 1",
    "Software Development Engineer",
    "Backend Engineer",
    "Application Developer",
    "Product Engineer",
]

LOCATIONS = ["", "Bangalore", "Chennai", "Hyderabad", "Pune"]
EXPERIENCE_LEVELS = [1, 2, 3]
PAGES = 1
JOB_AGE = 2

MAX_RAW_JOBS = 400          # stop fetching once we have this many unique jobs
FETCH_SLEEP = 1.2           # seconds between search calls (rate limiting)
APPLY_SLEEP = 3.0           # seconds between applies
QUOTA_RECHECK_EVERY = 10    # re-read Naukri history after this many applies

# Fallback answers for employer questionnaires. Only used when a job asks a
# structured question during the apply flow; override via --profile if needed.
QUESTIONNAIRE_PROFILE = {
    "current_ctc": "600000",
    "current_ctc_lakhs": "6",
    "expected_ctc": "1200000",
    "expected_ctc_lakhs": "12",
    "exp_total": "2",
    "exp_java": "2",
    "exp_python": "2",
    "exp_react": "2",
    "notice_days": 30,
    "dob": "11/08/2002",
    "ex_emp_id": "NA",
    "skills": [
        "java", "spring boot", "react", "typescript", "javascript", "sql",
        "oracle", "microservices", "rest api", "docker", "openshift",
        "github actions", "python", "snowflake",
    ],
}


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Jev-powered Naukri job application agent",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="score and print everything, but submit no applications",
    )
    parser.add_argument(
        "--quota-only", action="store_true",
        help="log in, report the remaining application quota, and exit",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="maximum applications this run (can only lower the quota budget)",
    )
    parser.add_argument(
        "--threshold", type=int, default=None,
        help="override JEV_MIN_APPLY_SCORE",
    )
    parser.add_argument(
        "--resume", default=os.getenv("JEV_RESUME_PATH", "resume.pdf"),
        help="path to the resume PDF",
    )
    parser.add_argument(
        "--max-score", type=int, default=150,
        help="cap on how many jobs are sent to Jev (cost/latency control)",
    )
    parser.add_argument(
        "--workers", type=int, default=5,
        help="parallel Jev requests",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="ignore the Jev score cache and re-score everything",
    )
    parser.add_argument(
        "--cookie", default="",
        help=(
            "reuse a nauk_at session cookie from a browser instead of logging in "
            "(defaults to $NAUKRI_COOKIE; saved for later runs automatically)"
        ),
    )
    parser.add_argument(
        "--refresh-browser", action="store_true",
        help=(
            "pull a fresh nauk_at from a dedicated Chrome profile and save it, "
            "then exit (use with --show-browser on first run to log in by hand)"
        ),
    )
    parser.add_argument(
        "--show-browser", action="store_true",
        help="run the refresh browser visibly instead of headless",
    )
    return parser.parse_args(argv)


# ----------------------------------------------------------------------
# Session reuse
#
# Naukri's /central-login-services/v1/login now answers a normal username +
# password POST with {"validRequest": false, "captchaSiteKey": "..."} — a
# reCAPTCHA challenge — on every transport we can use (it hits httpcloak,
# curl_cffi and plain requests alike, and the same IP still returns 200 for
# normal page loads). Since the API authenticates with the `nauk_at` cookie, the
# practical route is to log in once in a real browser, copy that cookie, and let
# the agent reuse it. The cookie is IP-bound, so run from the same network.
# ----------------------------------------------------------------------

def apply_session_cookie(client, raw_cookie):
    """
    Install a browser-obtained ``nauk_at`` cookie onto an existing client.

    Accepts the bare token, ``nauk_at=<token>``, or a full copied cookie string
    like ``'a=1; nauk_at=<token>; b=2'``. Returns the token, or "" when none
    could be found.
    """
    from src.models.models import NaukriSession

    token = session_store.extract_token(raw_cookie)
    if not token:
        return ""

    client.naukri_session = NaukriSession(token, {})

    # Best effort: some endpoints read the cookie jar directly. The httpcloak and
    # curl_cffi jars expose different APIs, so both shapes are attempted.
    try:
        client.session.cookies.set("nauk_at", token)
    except Exception:
        try:
            client.session.cookies["nauk_at"] = token
        except Exception:
            pass

    return token


def verify_session(client):
    """
    Confirm a reused cookie actually authenticates.

    Returns ``(ok, detail)``. This call is deliberately tiny (one history row) so
    a stale cookie fails immediately with a clear message instead of surfacing as
    confusing errors later in the run.
    """
    try:
        client.get_application_history(page_size=1, days=1, page_number=1)
        return True, "session valid"
    except Exception as exc:
        return False, str(exc)


def build_query_plan():
    """
    Return ``[(keyword, [locations])]`` for the two tiers.

    Tier A (resume-derived) sweeps every location because those titles match the
    resume exactly. Tier B (widened) runs all-India only, since generic titles
    return plenty without multiplying the request count by five.
    """
    plan = [(keyword, LOCATIONS) for keyword in RESUME_QUERIES]
    plan += [(keyword, [""]) for keyword in WIDENED_QUERIES]
    return plan


# ----------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------

def fetch_all_jobs(jc, verbose=True):
    """
    Search Naukri across the query plan and return unique Job objects.

    Deduplicates by job_id across every query, stops a query early when a page
    returns nothing, throttles between calls to stay clear of rate limits, and
    caps the total so a bad filter cannot balloon into thousands of jobs.
    """
    plan = build_query_plan()
    total_queries = sum(len(locs) for _, locs in plan) * len(EXPERIENCE_LEVELS) * PAGES

    ui.print_section_title(
        f"fetching jobs ({len(plan)} keywords, {total_queries} planned calls)"
    )

    seen = set()
    jobs = []

    for keyword, locations in plan:
        for location in locations:
            # Track how much this location actually contributed. Naukri's search
            # ignores the location filter for these queries in practice: a city
            # search returns the same nationwide page as All India, so once a
            # location yields nothing new the remaining cities for this keyword
            # cannot add anything either. Skipping them cuts the request count by
            # roughly 70% and keeps us well clear of rate limits.
            new_for_location = 0
            fetched_for_location = 0

            for experience in EXPERIENCE_LEVELS:
                for page in range(1, PAGES + 1):
                    if len(jobs) >= MAX_RAW_JOBS:
                        ui.print_warn(f"Reached MAX_RAW_JOBS={MAX_RAW_JOBS}, stopping")
                        return jobs

                    try:
                        batch = jc.search_jobs(
                            keyword=keyword,
                            location=location,
                            experience=experience,
                            job_age=JOB_AGE,
                            page=page,
                        )
                    except Exception as exc:
                        ui.print_error(
                            f"{keyword} @ {location or 'All India'} "
                            f"exp={experience} p{page} -> {exc}"
                        )
                        time.sleep(3)
                        continue

                    fresh = []
                    for job in batch:
                        job_id = str(getattr(job, "job_id", "") or "")
                        if job_id and job_id not in seen:
                            seen.add(job_id)
                            job.query_src = keyword
                            fresh.append(job)

                    jobs.extend(fresh)
                    new_for_location += len(fresh)
                    fetched_for_location += len(batch)

                    if verbose:
                        label = f"{keyword[:28]:<28} | {(location or 'All India')[:11]:<11}"
                        print(
                            f"  {Fore.WHITE}[{label} | exp={experience} | p{page}]"
                            f"{Style.RESET_ALL}  {len(batch):>3} fetched  "
                            f"{Fore.GREEN if fresh else Fore.WHITE}{len(fresh):>3} new"
                            f"{Style.RESET_ALL}"
                        )

                    if not batch:
                        break  # nothing here on page 1: skip further pages

                    time.sleep(FETCH_SLEEP)

            # Only skip the remaining cities when this location did return
            # results but none were new. An empty/errored location is left alone
            # so a transient failure cannot silently drop a whole keyword.
            if fetched_for_location and not new_for_location and len(locations) > 1:
                if verbose:
                    print(
                        f"  {Fore.WHITE}    -> {location or 'All India'} added nothing "
                        f"new; skipping other locations for this keyword"
                        f"{Style.RESET_ALL}"
                    )
                break

    ui.print_info(f"Total unique jobs collected: {Style.BRIGHT}{len(jobs)}{Style.RESET_ALL}")
    return jobs


# ----------------------------------------------------------------------
# Applying
# ----------------------------------------------------------------------

def apply_to_job(jc, result, profile=None):
    """
    Submit one application, handling questionnaires and external redirects.

    Returns ``(status, detail)`` where status is one of:
        ``applied``  - submitted successfully
        ``external`` - company-site apply; cannot be submitted via the API
        ``failed``   - an error occurred (does NOT consume quota)
    """
    job = result["job"]

    if jc.is_external_apply(job.job_id):
        return "external", "company-site apply"

    mandatory = job.tags[:2] if job.tags else []
    optional = job.tags[2:] if len(job.tags) > 2 else []

    response = jc.apply_job(
        job,
        mandatory_skills=mandatory,
        optional_skills=optional,
        source="search",
    )

    job_result = (response.get("jobs") or [{}])[0]

    if job_result.get("questionnaire"):
        sid = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "0000000"
        response = jc.handle_static_questionnaire_and_apply(
            job,
            questionnaire=job_result["questionnaire"],
            sid=sid,
            mandatory_skills=mandatory,
            optional_skills=optional,
            source="search",
            profile=profile,
        )
        if response.get("success") is False:
            return "failed", response.get("error", "questionnaire rejected")

    return "applied", None


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def _fingerprint(candidate):
    """Stable hash of the candidate profile, used to invalidate the score cache."""
    import hashlib
    import json

    payload = json.dumps(candidate, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def main(argv=None):
    args = parse_args(argv)

    username = os.getenv("USERNAME")
    password = os.getenv("PASSWORD")
    api_key = os.getenv("JEV_API_KEY") or os.getenv("TYPESAFE_API_KEY")
    model = os.getenv("JEV_MODEL", "jev-latest")
    threshold = args.threshold or int(os.getenv("JEV_MIN_APPLY_SCORE", "75"))

    ui.print_banner(
        "Noperi · Jev apply agent",
        f"model={model}  threshold={threshold}  mode="
        f"{'DRY RUN' if args.dry_run else 'LIVE'}",
    )

    # A saved session alone is enough to run. Credentials are only needed for the
    # password-login fallback, which Naukri currently captcha-gates anyway.
    has_session = bool(session_store.load_session())
    if not username and not has_session:
        ui.print_error("USERNAME must be set in .env")
        return 2
    if not password and not (args.cookie or os.getenv("NAUKRI_COOKIE") or has_session):
        ui.print_error("PASSWORD (or a session cookie) must be set in .env")
        return 2
    if not api_key:
        ui.print_error(
            "JEV_API_KEY is not set. Add it to .env "
            "(get one at https://console.typesafe.ai/keys)"
        )
        return 2

    # --- 1. verify Jev before touching Naukri -------------------------
    ui.print_section_title("verifying jev api key")
    try:
        jev = JevClient(api_key, model=model)
        models = jev.verify()
    except JevError as exc:
        ui.print_error(str(exc))
        return 2
    ui.print_ok(f"Jev reachable — models: {', '.join(models)}")

    # --- 2. parse the resume -----------------------------------------
    ui.print_section_title("parsing resume")
    try:
        candidate = load_resume_profile(
            args.resume, cache_file=DEFAULT_CACHE_FILE
        )
    except Exception as exc:
        ui.print_error(f"Could not parse {args.resume}: {exc}")
        return 2

    ui.print_kv("Headline", candidate.get("headline", "-"), Fore.CYAN)
    ui.print_kv("Experience", f"{candidate.get('years_experience', 0):g} years")
    ui.print_kv("Location", candidate.get("location", "-"))
    ui.print_kv(
        "Core skills",
        ", ".join(candidate.get("core_skills", [])[:8]) or "-",
    )
    ui.print_kv(
        "Secondary",
        ", ".join(candidate.get("secondary_skills", [])[:6]) or "-",
    )

    # --- 3. authenticate ---------------------------------------------
    client = NaukriLoginClient(username, password)

    # --refresh-browser: mint a token from a browser profile that is already
    # signed in. Every endpoint that issues a nauk_at is captcha-gated, so a
    # browser session is the only hands-free source.
    if args.refresh_browser:
        ui.print_section_title("refreshing session from browser")
        token, detail = session_store.browser_token(
            headless=not args.show_browser, verbose=True
        )
        ui.print_info(detail)
        if not token:
            ui.print_error("No nauk_at found in the browser profile")
            ui.print_info(
                "First run: add --show-browser, then log in to naukri.com in the "
                f"window that opens (profile: {session_store.DEFAULT_PROFILE_DIR})."
            )
            return 2
        session_store.save_session(token, source="browser")
        ui.print_ok(f"Saved session — {session_store.describe(token)}")
        ui.print_info("Re-run without --refresh-browser to continue.")
        return 0

    token, source, stale = session_store.resolve_token(
        cli_cookie=args.cookie,
        env_cookie=os.getenv("NAUKRI_COOKIE", ""),
    )

    if token:
        label = "saved session" if source == "cache" else f"{source} cookie"
        ui.print_section_title(f"using {label}")
        apply_session_cookie(client, token)
        ok, detail = verify_session(client)
        if not ok:
            ui.print_error(f"Session rejected: {detail}")
            if source == "cache":
                session_store.clear_session()
                ui.print_warn("Cleared the unusable saved session")
            ui.print_warn(
                "The token has expired, was rejected, or was issued to a "
                "different IP (cookies are IP-bound)."
            )
            ui.print_info(
                "Run: python3 jev_apply_agent.py --refresh-browser --show-browser"
            )
            return 3
        session_store.save_session(token, source=source)
        ui.print_ok(f"Session valid — {session_store.describe(token)}")
    elif stale:
        ui.print_section_title("session expired")
        ui.print_warn(f"Previous session {session_store.describe(stale)}")
        ui.print_info(
            "Naukri captcha-gates every token endpoint, so a new token has to come "
            "from a browser."
        )
        ui.print_info(
            "Run: python3 jev_apply_agent.py --refresh-browser --show-browser"
        )
        return 3
    else:
        ui.print_section_title("logging in to naukri")
        try:
            client.login()
        except Exception as exc:
            ui.print_error(f"Login failed: {exc}")
            # Naukri answers a blocked login with an anti-bot payload such as
            # {"validRequest": false, "captchaSiteKey": "..."} rather than a normal
            # auth error, so the fix is environmental rather than a bad password.
            ui.print_warn(
                "If the response above mentions validRequest/captchaSiteKey, Naukri "
                "is challenging the login with a reCAPTCHA rather than rejecting "
                "the password. Every token endpoint is gated the same way."
            )
            ui.print_info(
                "Run: python3 jev_apply_agent.py --refresh-browser --show-browser"
            )
            return 2

        if client.naukri_session:
            session_store.save_session(
                client.naukri_session.bearer_token, source="login"
            )
        ui.print_ok(f"Logged in as {username}")

    # --- 4. quota ----------------------------------------------------
    ui.print_section_title("application quota")
    quota_state = quota.used_in_window(client=client)
    ui.print_quota(quota.describe(client=client, state=quota_state))

    budget = quota.remaining(client=client, state=quota_state)
    if args.limit is not None:
        budget = min(budget, max(0, args.limit))
    ui.print_kv("Usable this run", str(budget), Fore.CYAN)

    if args.quota_only:
        ui.print_info("--quota-only: exiting without fetching or scoring")
        return 0

    if budget <= 0 and not args.dry_run:
        ui.print_warn(
            "No usable quota in the current 24h window. "
            "Re-run later, or use --dry-run to preview matches."
        )
        return 0

    # --- 5. fetch jobs -----------------------------------------------
    jc = NaukriJobClient(client)
    jobs = fetch_all_jobs(jc)
    if not jobs:
        ui.print_warn("No jobs returned. Naukri may be rate-limiting; try later.")
        return 0

    # --- 6. skip anything already applied to --------------------------
    already = quota.applied_job_ids(client=client)
    ui.print_info(f"Already applied (Naukri history + local ledgers): {len(already)}")

    # --- 7. score ----------------------------------------------------
    ui.print_section_title("scoring with jev")
    matcher = JevJobMatcher(
        jev=jev,
        candidate=candidate,
        min_apply_score=threshold,
        max_score_jobs=args.max_score,
        max_workers=args.workers,
        profile_fingerprint=_fingerprint(candidate),
        cache_file=None if args.no_cache else "jev_score_cache.json",
        # Empty by default: the inherited Infosys/Wipro/TCS/Accenture blacklist was
        # discarding roles whose titles matched the resume exactly. Opt back in
        # per company with JEV_VETO_COMPANIES="accenture,infosys".
        veto_companies=[
            name.strip()
            for name in os.getenv("JEV_VETO_COMPANIES", "").split(",")
            if name.strip()
        ],
    )
    outcome = matcher.run(jobs, already_applied=already)

    ui.print_info(f"Jev usage: {jev.usage_summary()}")

    # --- 8. show the ranked verdicts ----------------------------------
    eligible = outcome["eligible"]
    review = outcome["review"]

    ui.print_results_table("application-eligible", eligible)
    ui.print_results_table("review manually (blocked by a gate)", review, limit=15)

    if outcome["errors"]:
        ui.print_section_title(f"scoring errors ({len(outcome['errors'])})")
        for error in outcome["errors"][:5]:
            ui.print_error(f"{error.get('title')} -> {', '.join(error.get('failures') or [])}")

    if not eligible:
        ui.print_warn(
            "Nothing cleared the gates. Lower --threshold or widen the queries "
            "if this keeps happening."
        )
        ui.print_summary([
            ("Jobs fetched", str(len(jobs)), Fore.WHITE),
            ("Jobs scored", str(outcome["stats"]["scored"]), Fore.CYAN),
            ("Eligible", "0", Fore.YELLOW),
            ("Applied", "0", Fore.GREEN),
        ])
        return 0

    # --- 9. dry run stops here ----------------------------------------
    if args.dry_run:
        would_apply = eligible[:budget]
        ui.print_section_title("dry run - nothing submitted")
        ui.print_kv("Quota available", str(budget), Fore.CYAN)
        ui.print_kv(
            "Would apply to", f"{len(would_apply)} of {len(eligible)} eligible",
            Fore.GREEN,
        )
        for index, result in enumerate(would_apply, start=1):
            ui.print_job_card(index, len(would_apply), result)
        ui.print_summary([
            ("Jobs fetched", str(len(jobs)), Fore.WHITE),
            ("Jobs scored", str(outcome["stats"]["scored"]), Fore.CYAN),
            ("Passed strict gates", str(len(eligible)), Fore.GREEN),
            ("Held for review", str(len(review)), Fore.YELLOW),
            ("Would apply (budget)", str(len(would_apply)), Fore.CYAN),
            ("Applied", "0 (dry run)", Fore.WHITE),
            ("Jev usage", jev.usage_summary(), Fore.MAGENTA),
        ])
        return 0

    # --- 10. apply loop ------------------------------------------------
    selected = eligible[:budget]
    ui.print_section_title(
        f"applying to {len(selected)} jobs (budget {budget} of {len(eligible)} eligible)"
    )

    applied = 0
    skipped_external = 0
    failed = 0
    remaining_now = budget

    for index, result in enumerate(selected, start=1):
        if remaining_now <= 0:
            ui.print_warn("Quota budget exhausted - stopping before Naukri's cap")
            break

        ui.print_job_card(index, len(selected), result)

        try:
            status, detail = apply_to_job(jc, result, profile=QUESTIONNAIRE_PROFILE)
        except Exception as exc:
            status, detail = "failed", str(exc)

        if status == "applied":
            job = result["job"]
            ui.print_ok("Applied successfully")
            quota.record_application(
                job_id=job.job_id,
                title=job.title,
                company=job.company,
                role_family=result.get("role_family", ""),
                jev_score=result.get("jev_score", ""),
                confidence=result.get("confidence", ""),
                quota_used=budget - remaining_now + 1,
            )
            applied += 1
            remaining_now -= 1
        elif status == "external":
            ui.print_warn(f"Skipped - external apply ({detail})")
            skipped_external += 1
        else:
            ui.print_error(f"Failed - {detail}")
            failed += 1

        # A run can cross a 24h boundary or you may apply manually on your
        # phone mid-run, so re-read the authoritative count periodically.
        if applied and applied % QUOTA_RECHECK_EVERY == 0:
            remaining_now = quota.remaining(client=client)
            ui.print_quota(quota.describe(client=client))

        time.sleep(APPLY_SLEEP)

    # --- 11. summary ---------------------------------------------------
    ui.print_summary([
        ("Jobs fetched", str(len(jobs)), Fore.WHITE),
        ("Jobs scored", str(outcome["stats"]["scored"]), Fore.CYAN),
        ("Passed strict gates", str(len(eligible)), Fore.GREEN),
        ("Held for review", str(len(review)), Fore.YELLOW),
        ("Applied", str(applied), Fore.GREEN),
        ("Skipped (external)", str(skipped_external), Fore.YELLOW),
        ("Failed", str(failed), Fore.RED),
        ("Quota left this run", str(max(0, remaining_now)), Fore.CYAN),
        ("Jev usage", jev.usage_summary(), Fore.MAGENTA),
    ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
