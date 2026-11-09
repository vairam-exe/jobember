"""
jev_matcher.py

The resume-driven job scorer. This is the "model" that decides whether a job
deserves one of your scarce applications.

Design follows TypeSafe's own guidance for System One models
(https://docs.typesafe.ai/patterns/composite-scoring): Jev is asked several
*narrow* questions about one job, and **the weights live in code**, not in a
prompt. That means:

  * Each judgement is small enough for the model to be reliable at it.
  * You tune a coefficient instead of rewriting instructions.
  * Every score is inspectable - you can see exactly which factor dragged a job
    up or down, which a single "score this 0-100" prompt can never give you.

One request per job carries all eight questions. Jev evaluates them in parallel
against the same state, so adding questions barely changes latency.

Why this beats the previous ``gpt-4o-mini`` batch scorer in ``jop_classifier.py``:

  * The old rubric hardcoded a Node.js/Python candidate and scored
    "Java + Spring only" as 0-15. The actual resume is Java 17 / Spring Boot /
    React, so the old filter scored the candidate's own stack near zero.
    Here the candidate profile is parsed from the PDF, so it cannot drift.
  * Jev answers are typed. No JSON-fence stripping, no "parse error" fallbacks.
  * ~70-500ms per call and free output tokens, so scoring hundreds of jobs
    costs about a cent and finishes in seconds.
  * Calibrated ``confidence`` lets low-certainty jobs go to manual review
    instead of silently burning application quota.

Note on the application quota: this module only *ranks and filters*. The actual
budget enforcement lives in ``jev_apply_agent.py`` + ``quota.py``.
"""

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.client.jev_client import JevClient, JevError
from src.client.jop_classifier import JobFilterPipeline2


DEFAULT_CACHE_FILE = "jev_score_cache.json"
DEFAULT_MODEL = "jev-latest"


# ----------------------------------------------------------------------
# Strict thresholds
#
# Naukri allows 50 applications per rolling 24h for the whole account, and
# manual/bulk applies share that budget. Since applying is the scarce resource
# and scoring is nearly free, the correct trade is: score widely, apply narrowly.
# Every gate below must pass for a job to become application-eligible.
# ----------------------------------------------------------------------

MIN_APPLY_SCORE = 75        # composite 0-100 needed to spend quota
REVIEW_FLOOR = 60           # below this we do not even show it as a near-miss
CONFIDENCE_FLOOR = float(os.getenv("JEV_CONFIDENCE_FLOOR", "0.48"))  # minimum confidence across the typed answers
DEV_GATE = 0.75             # is_developer noul must be at least this
SUPPORT_GATE = 0.20         # is_support_role noul must be at most this
STACK_GATE = 0.70           # matches_resume_stack noul must be at least this
MIN_SKILL_OVERLAP = 3       # skill_overlap level (0-4)
MIN_SHOULD_APPLY = 3        # should_apply level (0-4)

ALLOWED_ROLE_FAMILIES = {"backend", "fullstack"}
ALLOW_FRONTEND = True       # UI/Frontend roles allowed

# Which answers the confidence gate watches. These are the ones the decision is
# actually made from (the categorical role gate plus the two graded dimensions
# that carry a hard level minimum), so their uncertainty is what should block.
CONFIDENCE_ANSWERS = ("role_family", "skill_overlap", "should_apply")

# Weights sum to 0.95; the remaining 0.05 is reserved for the recency bonus so
# the composite lands in a predictable 0-100 range.
WEIGHTS = {
    "skill_overlap":  0.40,
    "experience_fit": 0.15,
    "seniority_fit":  0.10,
    "should_apply":   0.30,
}

RECENCY_BONUS = 5           # awarded when a job was posted within a day
RECENCY_DAYS = 1


# ----------------------------------------------------------------------
# Question definitions
# ----------------------------------------------------------------------

ROLE_FAMILY_OPTIONS = {
    "backend":   "Server-side APIs, services, databases, microservices",
    "fullstack": "Both server-side and browser/UI development",
    "frontend":  "Browser/UI only, with no server-side work",
    "mobile":    "Android, iOS, Flutter or React Native apps",
    "data":      "Data engineering, analytics, warehousing, BI or ML pipelines",
    "devops":    "Infrastructure, CI/CD, SRE or cloud platform only",
    "support":   "Application/production/customer support, helpdesk, L1/L2, "
                 "maintenance or BPO",
    "qa":        "Testing/QA/SDET or automation-testing only, no feature code",
    "other":     "Anything not covered above",
}

SCORE_LEVELS = {
    "skill_overlap": [
        "None of the candidate's core skills apply; the stack is unrelated",
        "Only one peripheral skill overlaps; the job needs a different stack",
        "Some overlap, but the job's main required skills are missing",
        "Most required skills overlap with the candidate's core stack",
        "The required stack is almost exactly the candidate's core stack",
    ],
    "experience_fit": [
        "Job demands far more experience than the candidate has (5+ years more)",
        "Job demands notably more experience than the candidate has (3-4 years more)",
        "Job demands somewhat more experience than the candidate has (1-2 years more)",
        "Job demands about the same experience as the candidate has",
        "Job demands equal or less experience; the candidate is well matched or senior",
    ],
    "seniority_fit": [
        "Role is far above the candidate's level (principal, staff, architect, manager)",
        "Role is above the candidate's level (senior or lead)",
        "Role is slightly above the candidate's level",
        "Role matches the candidate's level (junior-mid engineer / SDE 1)",
        "Role is at or slightly below the candidate's level (entry / junior)",
    ],
    "should_apply": [
        "Clearly not worth an application",
        "Poor fit overall",
        "Borderline fit",
        "Good fit; worth spending an application",
        "Excellent fit; apply immediately",
    ],
}

# Applied to job *titles* only, before any tokens are spent. Deliberately narrow
# so real engineering roles are never dropped by mistake.
SUPPORT_TITLE_PATTERN = re.compile(
    r"\b("
    r"support|helpdesk|help\s*desk|service\s*desk|desktop\s*support|"
    r"technical\s*support|customer\s*support|production\s*support|"
    r"application\s*support|maintenance\s*engineer|bpo|"
    r"l1|l2|l3|sdet|qa|quality\s*assurance|"
    r"manual\s*test(?:er|ing)|tester|data\s*entry|non\s*voice|voice\s*process"
    r")\b",
    re.IGNORECASE,
)


def _levels_for(name):
    """Number of rubric levels, used to normalise a 0..n score into 0..1."""
    return len(SCORE_LEVELS[name])


def normalise_score(answer, name):
    """
    Convert a Jev Score answer into 0.0-1.0.

    Jev returns a position on the rubric (e.g. 3.74 on a 0-4 scale); dividing by
    the top level makes it comparable with the Noul probabilities.
    """
    if not isinstance(answer, dict):
        return 0.0
    raw = answer.get("score")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    top = _levels_for(name) - 1
    if top <= 0:
        return 0.0
    return max(0.0, min(1.0, value / top))


def answer_level(answer):
    """Rounded rubric level (0..n) for a Score answer, or None."""
    if not isinstance(answer, dict):
        return None
    try:
        return int(round(float(answer.get("score"))))
    except (TypeError, ValueError):
        return None


def days_old_from_posted(posted):
    """
    Convert Naukri's relative "posted" label into an integer day count.

    Mirrors ``JobFilterPipeline2.normalize_jobs`` so the recency bonus agrees
    with the pre-filter's notion of freshness.
    """
    text = str(posted or "").lower()
    if not text:
        return 7
    if "today" in text or "hour" in text or "just now" in text:
        return 0
    if "yesterday" in text:
        return 1

    match = re.search(r"(\d+)\s*day", text)
    if match:
        return int(match.group(1))

    match = re.search(r"(\d+)\s*week", text)
    if match:
        return int(match.group(1)) * 7

    return 7


class JevJobMatcher:
    """
    Scores Naukri jobs against a parsed resume using Jev.

    Parameters
    ----------
    api_key / jev:
        Pass a raw key (a ``JevClient`` is built for you) or an existing client.
    candidate:
        The dict from ``resume_profile.build_candidate_state``. This is the
        ground truth Jev judges every job against.
    min_apply_score:
        Composite threshold (default 75). Lower it to apply to more jobs.
    """

    def __init__(
        self,
        api_key=None,
        candidate=None,
        jev=None,
        model=DEFAULT_MODEL,
        cache_file=DEFAULT_CACHE_FILE,
        min_apply_score=MIN_APPLY_SCORE,
        review_floor=REVIEW_FLOOR,
        confidence_floor=CONFIDENCE_FLOOR,
        max_workers=5,
        max_score_jobs=150,
        profile_fingerprint="",
        veto_companies=None,
        verbose=True,
    ):
        if jev is None:
            jev = JevClient(api_key, model=model)
        if not candidate:
            raise ValueError("A parsed candidate profile is required")

        self.jev = jev
        self.candidate = candidate
        self.cache_file = cache_file
        self.min_apply_score = min_apply_score
        self.review_floor = review_floor
        self.confidence_floor = confidence_floor
        self.max_workers = max_workers
        self.max_score_jobs = max_score_jobs
        self.profile_fingerprint = profile_fingerprint
        self.veto_companies = veto_companies
        self.verbose = verbose

        self.cache = self._load_cache()
        self._cache_lock = threading.Lock()

        # We borrow only the free regex pre-filters from the old pipeline.
        # No OpenAI request is ever made, so the blank key is intentional.
        self._prefilter = JobFilterPipeline2(
            openai_api_key="", cache_file="jev_prefilter_cache.json"
        )

        # Jev scoring makes the company blacklist redundant *and* harmful here.
        # A live run showed it discarding ~40 roles whose titles were exactly the
        # candidate's stack ("Java Spring Boot Microservices @ Infosys",
        # "Java Full Stack Developer @ Infosys", and 18 near-identical
        # "Application Developer @ Accenture" postings). Whether a service company
        # is a good employer is a judgement, not a regex — so by default we let
        # Jev score the role on its merits and only veto companies the user names
        # explicitly.
        #
        # This shadows the class attribute on this instance only, so
        # apply_agent.py keeps its original blacklist behaviour untouched.
        self._prefilter.VETO_COMPANIES = {
            str(name).strip().lower()
            for name in (veto_companies or [])
            if str(name).strip()
        }

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _load_cache(self):
        if not self.cache_file or not os.path.exists(self.cache_file):
            return {}
        try:
            with open(self.cache_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    def _save_cache(self):
        if not self.cache_file:
            return
        try:
            with open(self.cache_file, "w", encoding="utf-8") as handle:
                json.dump(self.cache, handle, indent=2)
        except OSError:
            pass  # read-only FS or concurrent writer: caching is best-effort
        except TypeError:
            # Never let a non-serialisable cache entry break an otherwise
            # successful run; the cache only saves tokens, it is not required.
            pass

    def _cached_result(self, job_id):
        """
        Return a cached scoring result when the cache entry is still valid.

        Entries are invalidated when the resume profile changes, so re-uploading
        an updated resume automatically re-scores everything.
        """
        entry = self.cache.get(str(job_id))
        if not isinstance(entry, dict):
            return None
        if entry.get("profile") != self.profile_fingerprint:
            return None
        if entry.get("model") != getattr(self.jev, "model", None):
            return None
        return entry.get("result")

    # ------------------------------------------------------------------
    # State + questions
    # ------------------------------------------------------------------

    def build_state(self, job):
        """
        One structured state per job: the candidate on the left, the posting on
        the right, so Jev compares two named records rather than prose.

        The full resume text is included so a judgement never depends solely on
        our parsing. Descriptions are capped because Naukri's filler text is long
        and adds cost without adding signal.
        """
        return {
            "candidate": self.candidate,
            "job": {
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "experience_required": job.experience,
                "salary": job.salary,
                "posted": job.posted_date,
                "days_old": days_old_from_posted(job.posted_date),
                "skills": job.tags or [],
                "description": (job.description or "")[:4000],
            },
        }

    def build_questions(self):
        """
        The eight questions asked about every job.

        Three coarse gates (Noul/Choice) catch roles we must never apply to, and
        four graded dimensions (Score) are combined with the weights in code. The
        final ``should_apply`` Score is the holistic tie-breaker: it sees the same
        state but answers "given all of it, is this worth an application?".
        """
        candidate = self.candidate
        years = candidate.get("years_experience", 0)
        stack = ", ".join(candidate.get("core_skills", [])[:12]) or "the listed skills"

        return {
            "is_developer": {
                "type": "noul",
                "instructions": (
                    "Is this a software developer or software engineering role where the "
                    "person writes application code, rather than a support, helpdesk, "
                    "QA-only, sales, recruiting or pure infrastructure-operations role?"
                ),
            },
            "is_support_role": {
                "type": "noul",
                "instructions": (
                    "Is this role primarily application/production support, helpdesk, "
                    "L1/L2 maintenance, BPO or customer service rather than building "
                    "new software?"
                ),
            },
            "role_family": {
                "type": "choice",
                "instructions": "What is the primary role family for this job?",
                "criteria": ROLE_FAMILY_OPTIONS,
            },
            "matches_resume_stack": {
                "type": "noul",
                "instructions": (
                    f"Do the job's required skills overlap substantially with the "
                    f"candidate's core skills ({stack})?"
                ),
            },
            "skill_overlap": {
                "type": "score",
                "instructions": (
                    "How much do the job's required skills overlap with the candidate's "
                    "core and secondary skills?"
                ),
                "criteria": SCORE_LEVELS["skill_overlap"],
            },
            "experience_fit": {
                "type": "score",
                "instructions": (
                    f"How well does the candidate's {years:g} years of experience match "
                    f"the experience this job demands?"
                ),
                "criteria": SCORE_LEVELS["experience_fit"],
            },
            "seniority_fit": {
                "type": "score",
                "instructions": (
                    "How well does the seniority implied by this job title and "
                    "description match an early-career engineer with about "
                    f"{years:g} years of experience?"
                ),
                "criteria": SCORE_LEVELS["seniority_fit"],
            },
            "should_apply": {
                "type": "score",
                "instructions": (
                    "Overall, how good a fit is this job for this candidate, "
                    "considering their actual experience, skills and seniority?"
                ),
                "criteria": SCORE_LEVELS["should_apply"],
            },
        }

    # ------------------------------------------------------------------
    # Composite scoring + gates
    # ------------------------------------------------------------------

    def composite_score(self, answers, days_old=7):
        """
        Fold the four graded answers into one 0-100 number.

        Returns ``(score, parts, bonus)`` where ``parts`` holds each dimension's
        0-100 value, so the terminal can show *why* a job landed where it did.
        """
        weighted = 0.0
        parts = {}

        for name, weight in WEIGHTS.items():
            normalised = normalise_score(answers.get(name), name)
            parts[name] = int(round(normalised * 100))
            weighted += weight * normalised

        weight_sum = sum(WEIGHTS.values()) or 1.0
        base = (weighted / weight_sum) * 100.0
        bonus = RECENCY_BONUS if days_old <= RECENCY_DAYS else 0

        return int(round(min(100.0, base + bonus))), parts, bonus

    @staticmethod
    def _confidence(answers):
        """
        Confidence in the answers the decision actually rests on.

        Deliberately **not** the minimum across all eight answers. The
        ``experience_fit`` and ``seniority_fit`` rubrics are continuous and Jev
        is naturally less certain about them, so including them let a single
        soft dimension drag an otherwise perfect match (score 98) into review.

        We only route on three answers — the categorical role gate and the two
        graded dimensions in WEIGHTS that have hard level minimums — so those are
        the ones whose uncertainty should be able to block an application.
        """
        values = []
        for name in CONFIDENCE_ANSWERS:
            answer = answers.get(name)
            if isinstance(answer, dict) and answer.get("confidence") is not None:
                try:
                    values.append(float(answer["confidence"]))
                except (TypeError, ValueError):
                    pass

        # If a response ever omits confidence entirely, do not fail closed on
        # this gate alone: the other seven gates still apply unchanged.
        return min(values) if values else 1.0

    @staticmethod
    def _noul(answers, name):
        answer = answers.get(name)
        if not isinstance(answer, dict):
            return 0.0
        try:
            return float(answer.get("noul") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def gate(self, answers, days_old):
        """
        Apply every strict gate plus the composite threshold.

        Returns a dict with the decision (``apply`` / ``review`` / ``veto``), the
        composite score, per-dimension parts, confidence, role family, and the
        list of failed gates. A job is application-eligible only when that list
        is empty — no partial credit, because quota is the scarce resource.
        """
        failures = []

        is_developer = self._noul(answers, "is_developer")
        is_support = self._noul(answers, "is_support_role")
        stack_match = self._noul(answers, "matches_resume_stack")

        role_answer = answers.get("role_family") or {}
        role_family = role_answer.get("choice") if isinstance(role_answer, dict) else None

        skill_level = answer_level(answers.get("skill_overlap"))
        apply_level = answer_level(answers.get("should_apply"))
        confidence = self._confidence(answers)
        score, parts, bonus = self.composite_score(answers, days_old=days_old)

        allowed_families = set(ALLOWED_ROLE_FAMILIES)
        if ALLOW_FRONTEND:
            allowed_families.add("frontend")

        if is_support > SUPPORT_GATE:
            failures.append(f"support role (p={is_support:.2f} > {SUPPORT_GATE})")
        if is_developer < DEV_GATE:
            failures.append(f"not a developer role (p={is_developer:.2f} < {DEV_GATE})")
        if stack_match < STACK_GATE:
            failures.append(f"weak resume-stack match (p={stack_match:.2f} < {STACK_GATE})")
        if role_family not in allowed_families:
            failures.append(f"role family {role_family or 'unknown'} not in {sorted(allowed_families)}")
        if skill_level is None or skill_level < MIN_SKILL_OVERLAP:
            failures.append(f"skill overlap level {skill_level} < {MIN_SKILL_OVERLAP}")
        if apply_level is None or apply_level < MIN_SHOULD_APPLY:
            failures.append(f"should_apply level {apply_level} < {MIN_SHOULD_APPLY}")
        if confidence < self.confidence_floor:
            failures.append(f"low confidence {confidence:.2f} < {self.confidence_floor}")
        if score < self.min_apply_score:
            failures.append(f"score {score} < {self.min_apply_score}")

        if not failures:
            decision = "apply"
        elif score >= self.review_floor:
            decision = "review"
        else:
            decision = "veto"

        return {
            "jev_score": score,
            "parts": parts,
            "recency_bonus": bonus,
            "confidence": round(confidence, 3),
            "role_family": role_family,
            "is_developer": round(is_developer, 3),
            "is_support_role": round(is_support, 3),
            "stack_match": round(stack_match, 3),
            "skill_level": skill_level,
            "should_apply_level": apply_level,
            "decision": decision,
            "failures": failures,
            "answers": answers,
        }

    # ------------------------------------------------------------------
    # Per-job scoring
    # ------------------------------------------------------------------

    def score_job(self, job, use_cache=True):
        """
        Score one job and return a verdict dict (no ``job`` object attached, so
        the result stays JSON-serialisable for the cache).
        """
        job_id = str(getattr(job, "job_id", "") or "")
        days_old = days_old_from_posted(job.posted_date)

        if use_cache and job_id:
            cached = self._cached_result(job_id)
            if cached:
                answers = cached.get("answers")
                if answers:
                    result = self.gate(answers, days_old)
                    result.update({
                        "job_id": job_id,
                        "title": getattr(job, "title", cached.get("title", "")),
                        "company": getattr(job, "company", cached.get("company", "")),
                        "days_old": days_old,
                        "cached": True,
                    })
                    with self._cache_lock:
                        if job_id in self.cache:
                            self.cache[job_id]["result"] = dict(result)
                    return result

                result = dict(cached)
                result["cached"] = True
                return result

        try:
            answers = self.jev.ask(self.build_state(job), self.build_questions())
        except JevError as exc:
            return {
                "job_id": job_id,
                "title": job.title,
                "company": job.company,
                "days_old": days_old,
                "jev_score": 0,
                "confidence": 0.0,
                "role_family": None,
                "decision": "error",
                "failures": [f"jev error: {exc}"],
                "parts": {},
                "answers": {},
                "cached": False,
            }

        result = self.gate(answers, days_old)
        result.update({
            "job_id": job_id,
            "title": job.title,
            "company": job.company,
            "days_old": days_old,
            "cached": False,
        })

        if job_id:
            with self._cache_lock:
                self.cache[job_id] = {
                    "profile": self.profile_fingerprint,
                    "model": getattr(self.jev, "model", None),
                    # Store a copy: score_jobs() attaches the live Job object to
                    # the dict it returns, and mutating the cached instance would
                    # make it non-serialisable (and leak a Job into the cache).
                    "result": dict(result),
                }

        return result

    def score_jobs(self, jobs, use_cache=True):
        """
        Score many jobs concurrently.

        Jev answers in 70-500ms, so a small thread pool is enough to turn a few
        hundred jobs into a handful of seconds. Each thread makes one request;
        there is no shared mutable state beyond the cache, which is locked.
        """
        results = []
        total = len(jobs)
        if total == 0:
            return results

        with ThreadPoolExecutor(max_workers=max(1, self.max_workers)) as pool:
            futures = {
                pool.submit(self.score_job, job, use_cache): job for job in jobs
            }
            for done, future in enumerate(as_completed(futures), start=1):
                job = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # never let one job kill the run
                    result = {
                        "job_id": str(getattr(job, "job_id", "") or ""),
                        "title": getattr(job, "title", ""),
                        "company": getattr(job, "company", ""),
                        "jev_score": 0,
                        "confidence": 0.0,
                        "role_family": None,
                        "decision": "error",
                        "failures": [f"unexpected error: {exc}"],
                        "parts": {},
                        "answers": {},
                    }

                result["job"] = job
                results.append(result)

                if self.verbose:
                    flag = result.get("decision", "?")
                    print(
                        f"  [{done:>3}/{total}] {flag:<6} "
                        f"{result.get('jev_score', 0):>3} "
                        f"{result.get('role_family') or '-':<10} "
                        f"{(result.get('title') or '')[:44]}"
                    )

        self._save_cache()
        return results

    # ------------------------------------------------------------------
    # Pre-filter (free) and full run
    # ------------------------------------------------------------------

    def prefilter(self, jobs):
        """
        Run the free regex filters before spending a single token.

        These come from ``JobFilterPipeline2`` so behaviour matches the existing
        agent (company blacklist, walk-in detection, experience window, title
        sanity). A support-role regex is layered on top because that is the one
        hard rule Jev should not have to spend tokens discovering.
        """
        normalized = self._prefilter.normalize_jobs(jobs)
        normalized = self._prefilter.dedup(normalized)
        normalized = self._prefilter.hard_veto(normalized)
        normalized = self._prefilter.experience_filter(normalized)
        normalized = self._prefilter.desc_red_flag_check(normalized)
        normalized = self._prefilter.title_filter(normalized)
        normalized = self._prefilter.company_veto(normalized)

        kept = []
        for entry in normalized:
            title = entry.get("title") or ""
            if SUPPORT_TITLE_PATTERN.search(title):
                if self.verbose:
                    print(f"  [SUPPORT VETO] {title}")
                continue
            kept.append(entry)

        kept = self._prefilter.tag_presort(kept)

        # Re-attach the original Job objects, since the apply flow needs them.
        by_id = {}
        for job in jobs:
            job_id = str(getattr(job, "job_id", "") or "")
            if job_id and job_id not in by_id:
                by_id[job_id] = job

        return [
            by_id[str(entry.get("job_id"))]
            for entry in kept
            if str(entry.get("job_id")) in by_id
        ]

    def run(self, jobs, already_applied=None):
        """
        Full pipeline: free filters -> Jev scoring -> ranked verdicts.

        Returns a dict rather than a bare list so the caller can show near-misses
        and failures separately:

            {
              "eligible": [...],   # passed every gate, best first
              "review":   [...],   # close but blocked, for manual inspection
              "rejected": [...],   # everything else that was scored
              "errors":   [...],   # Jev failures
              "stats":    {...},
            }
        """
        already = {str(x) for x in (already_applied or set())}

        if self.verbose:
            print(f"\n  RAW JOBS: {len(jobs)}")

        candidates = self.prefilter(jobs)

        if self.verbose:
            print(f"  AFTER FREE FILTERS: {len(candidates)}")

        if already:
            before = len(candidates)
            candidates = [
                job for job in candidates
                if str(getattr(job, "job_id", "") or "") not in already
            ]
            if self.verbose and before != len(candidates):
                print(f"  AFTER ALREADY-APPLIED: {len(candidates)} "
                      f"(dropped {before - len(candidates)})")

        trimmed = candidates[: self.max_score_jobs]
        if self.verbose and len(candidates) > len(trimmed):
            print(f"  SCORING TOP {len(trimmed)} OF {len(candidates)} (by stack overlap)")

        results = self.score_jobs(trimmed)

        def sort_key(item):
            # Best first: score, then the confidence behind it, then freshness.
            return (-item.get("jev_score", 0), -item.get("confidence", 0.0),
                    item.get("days_old", 99))

        eligible = sorted(
            [r for r in results if r.get("decision") == "apply"], key=sort_key
        )
        review = sorted(
            [r for r in results if r.get("decision") == "review"], key=sort_key
        )
        errors = [r for r in results if r.get("decision") == "error"]
        rejected_ids = {
            id(r) for r in eligible
        } | {
            id(r) for r in review
        } | {
            id(r) for r in errors
        }
        rejected = [r for r in results if id(r) not in rejected_ids]

        stats = {
            "raw": len(jobs),
            "after_filters": len(candidates),
            "scored": len(results),
            "eligible": len(eligible),
            "review": len(review),
            "rejected": len(rejected),
            "errors": len(errors),
            "cached": sum(1 for r in results if r.get("cached")),
        }

        if self.verbose:
            print(
                f"  SCORED: {stats['scored']} "
                f"({stats['cached']} cached)  ->  "
                f"eligible={stats['eligible']}  review={stats['review']}  "
                f"rejected={stats['rejected']}  errors={stats['errors']}"
            )

        return {
            "eligible": eligible,
            "review": review,
            "rejected": rejected,
            "errors": errors,
            "stats": stats,
        }
