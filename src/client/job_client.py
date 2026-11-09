import logging
from datetime import datetime

from src.models.models import Job
from src.client.naukri_client import NaukriLoginClient
from src.exceptions.exceptions import NaukriAuthError, NaukriParseError
from src.utils.request_helper import with_exponential_retry
from src.utils.nkparam_generator import generate_nkparam
from src.config.constants import RECOMMENDED_JOBS_URL, JOB_SEARCH_URL, APPLY_JOB_URL,HISTORY_URL
import json


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
_handler = logging.StreamHandler()
_handler.setFormatter(
    logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
)
logger.addHandler(_handler)


APPLY_SRC_MAP = {
    "recommended": ("drecomm_apply", "--drecomm_apply-1-F-0-1--{sid}-"),
    "search":      ("srp",           "--srp-1-F-0-1--{sid}-"),
}


# ----------------------------------------------------------------------------------
# NaukriJobClient
#
# A thin client over Naukri's internal APIs using an authenticated session from
# NaukriLoginClient. Handles job search, recommendations, and apply workflows.
#
# Responsibilities:
#   - Build correct headers (authenticated and non-authenticated)
#   - Generate SEO-style keys for the search endpoint
#   - Attach the required nkparam header
#   - Parse raw API responses into the Job model
#   - Normalize inconsistent fields (placeholders, tags, etc.)
#   - Handle common failure cases (403, 406, malformed JSON)
# ----------------------------------------------------------------------------------


# ----------------------------------------------------------------------------------
# nkparam header
#
# nkparam is a signed request header required by the Naukri search API. It is
# generated inside their obfuscated frontend JS and validated server-side.
# A missing or invalid token results in a 403 response.
#
# It is not tied to the login session directly, but to how the frontend signs
# outgoing requests.
# ----------------------------------------------------------------------------------


# ----------------------------------------------------------------------------------
# Supported nkparam modes
#
# 1. Generator mode (default)
#    Uses generate_nkparam("srp") to produce a fresh token per request.
#    Preferred when the generator logic is working correctly.
#
# 2. Pool mode (optional)
#    Uses a list of pre-captured tokens (self.pool), rotated via pool_idx.
#    Useful as a fallback if the generator breaks.
#
# Toggle via:
#    NaukriJobClient(login_client, use_pool=True)
# ----------------------------------------------------------------------------------


# ----------------------------------------------------------------------------------
# Token pool notes
#
# Naukri's search endpoint (/jobapi/v3/search) requires a signed nkparam header.
# This token is generated inside Naukri's obfuscated JS bundle and changes each
# browser session. Without a valid token the API returns 403 Forbidden.
#
# Harvesting tokens:
#   1. Run: python get_Nkparam.py
#      Opens Chrome, captures nkparam from network logs, appends to nkPool.txt.
#   2. Collect roughly 100 tokens for light usage, ~1000 for heavy usage.
#
# Using the pool:
#   self.pool = open("nkPool.txt").read().splitlines()
#
# Token expiry:
#   Tokens typically last a few hours. On 403, rotate to the next token.
#   If all tokens fail, regenerate the pool.
#
# Note: do not commit nkPool.txt. Add it to .gitignore.
# ----------------------------------------------------------------------------------


# ----------------------------------------------------------------------------------
# Design notes
#
# - All helpers live inside the class, no module-level globals.
# - _get_nkparam() abstracts which token source is used.
# - The search path only requires a valid nkparam.
# - The rest of the client is a standard request / parse layer.
# ----------------------------------------------------------------------------------


class NaukriJobClient:

    def __init__(self, login_client: NaukriLoginClient, use_pool: bool = False):
        if not login_client.session:
            raise NaukriAuthError("Login required")

        self._session = login_client.session
        self._client = login_client

        self.pool_idx = 0
        self.use_pool = use_pool

        # Seed pool with one pre-captured token as a baseline fallback.
        self.pool = [
            "sa9chfJkrXEpn3Zt7rAPaAOb6gAWNSFzzmPQEc6tLSMzytUGPxrGDqiKJyjvBAHGIYPhbDRBDHMad071ZRZlZA=="
        ]

    # ----------------------------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------------------------

    def _parse_job(self, raw: dict) -> Job:
        # Extract location from the placeholders list if present.
        location = next(
            (p["label"] for p in raw.get("placeholders", []) if p.get("type") == "location"),
            "N/A",
        )
        return Job(
            job_id=str(raw.get("jobId") or raw.get("id") or ""),
            title=raw.get("title") or raw.get("jobTitle") or "N/A",
            company=raw.get("companyName") or raw.get("company") or "N/A",
            location=location,
            experience=raw.get("experienceText") or raw.get("experience") or "N/A",
            salary=raw.get("salaryDetail") or raw.get("salary") or "Not disclosed",
            posted_date=raw.get("footerPlaceholderLabel") or raw.get("postedDate") or "N/A",
            apply_link=raw.get("jdURL") or f"https://www.naukri.com/job-listings-{raw.get('jobId', '')}",
            description=raw.get("jobDescription") or "",
            tags=(
                [t.strip() for t in raw.get("tagsAndSkills", "").split(",")]
                if raw.get("tagsAndSkills")
                else []
            ),
        )

    def _cluster_dates(self) -> dict:
        # Returns a dict of current UTC timestamps used by the recommended jobs payload.
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return {
            "apply":        now,
            "preference":   now,
            "profile":      now,
            "similar_jobs": now,
        }

    def _headers(self):
        return self._client._build_headers(auth=True)

    def _build_seo_key(self, keyword: str, location: str, page: int) -> str:
        # Produces the seoKey param expected by the search endpoint.
        # Example: "python-developer-jobs-in-bangalore-2"
        kw_slug = (
            keyword.strip().lower()
            .replace(".", "-dot-")
            .replace(" ", "-")
            .replace("+", "-")
            .strip("-")
        )

        if location.strip():
            loc_slug = location.strip().lower().replace(" ", "-")
            return f"{kw_slug}-jobs-in-{loc_slug}-{page}"

        return f"{kw_slug}-jobs-{page}"

    def format_jobs(self, raw_jobs: list) -> list[dict]:
        # Converts raw job dicts from the API into a flat, readable structure.
        formatted = []

        for job in raw_jobs:
            exp = sal = loc = ""

            for item in job.get("placeholders", []):
                t = item.get("type")
                if t == "experience":
                    exp = item.get("label")
                elif t == "salary":
                    sal = item.get("label")
                elif t == "location":
                    loc = item.get("label")

            formatted.append({
                "title":    job.get("title"),
                "company":  job.get("companyName"),
                "experience": exp,
                "location": loc,
                "salary":   sal,
                "skills":   job.get("tagsAndSkills", "").split(","),
                "job_url":  "https://www.naukri.com" + job.get("jdURL", ""),
                "posted":   job.get("footerPlaceholderLabel"),
            })

        return formatted

    def _get_nkparam(self) -> str:
        # Returns a token from the pool (pool mode) or generates a fresh one.
        if self.use_pool:
            token = self.pool[self.pool_idx % len(self.pool)]
            self.pool_idx += 1
            return token
        return generate_nkparam("srp")

    def _search_headers(self) -> dict:
        # Builds headers for the search endpoint. Uses non-auth base headers
        # and adds the appid, gid, and nkparam fields required by the search API.
        headers = self._client._build_headers(auth=False)
        headers.update({
            "authority":       "www.naukri.com",
            "accept":          "application/json",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "en-US,en;q=0.9",
            "appid":           "109",
            "gid":             "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
            "nkparam":         self._get_nkparam(),
        })
        return headers

    # ----------------------------------------------------------------------------------
    # Job details
    # ----------------------------------------------------------------------------------

    def get_job_details(self, job_id: str, sid: str = "") -> dict:
        if not job_id:
            raise ValueError("job_id is required")

        if not sid:
            sid = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "0000000"

        url = f"https://www.naukri.com/jobapi/v1/job/{job_id}"

        params = {
            "microsite": "y",
            "src":       "jobsearchDesk",
            "sid":       sid,
            "xp":        "1",
            "px":        "1",
        }

        headers = self._client._build_headers(auth=True)
        headers["nkparam"] = self._get_nkparam()
        headers.update({
            "appid":          "121",
            "systemid":       "Naukri",
            "clientid":       "d3skt0p",
            "accept":         "application/json",
            "referer":        "https://www.naukri.com/",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        })

        logger.debug("Fetching job details for job_id=%s sid=%s", job_id, sid)

        res = self._session.get(url, headers=headers, params=params)

        if res.status_code in (401, 403):
            try:
                msg = res.json().get("message", "Auth failed")
            except Exception:
                msg = res.text
            raise NaukriAuthError(msg)

        if not res.ok:
            raise NaukriParseError(f"Job details fetch failed: {res.status_code} — {res.text}")

        try:
            return res.json()
        except Exception:
            raise NaukriParseError(f"Invalid JSON response: {res.text}")

    def is_external_apply(self, job_id: str, sid: str = "") -> bool:
        data = self.get_job_details(job_id, sid)

        job = data.get("job") if isinstance(data, dict) else None
        return isinstance(job, dict) and job.get("responseManager") == "companyUrl"

    # ----------------------------------------------------------------------------------
    # Apply job
    # ----------------------------------------------------------------------------------

    def apply_job(
        self,
        job: Job,
        mandatory_skills: list[str] = None,
        optional_skills:  list[str] = None,
        sid:    str = "",
        source: str = "recommended",
    ) -> dict:
        url = APPLY_JOB_URL

        if not job.job_id:
            raise ValueError("Invalid job_id")

        if not sid:
            sid = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "0000000"

        apply_src, logstr_template = APPLY_SRC_MAP.get(source, APPLY_SRC_MAP["recommended"])
        logstr = logstr_template.format(sid=sid)

        payload = {
            "strJobsarr":       [job.job_id],
            "logstr":           logstr,
            "flowtype":         "show",
            "crossdomain":      True,
            "jquery":           1,
            "rdxMsgId":         "",
            "chatBotSDK":       True,
            "mandatory_skills": mandatory_skills or [],
            "optional_skills":  optional_skills or [],
            "applyTypeId":      "107",
            "closebtn":         "y",
            "applySrc":         apply_src,
            "sid":              sid,
            "mid":              "",
        }

        headers = self._client._build_headers(auth=True)
        headers.update({
            "appid":     "121",
            "systemid":  "jobseeker",
            "clientid":  "d3skt0p",
            "accept":    "application/json",
        })

        logger.debug("Applying to job_id=%s sid=%s", job.job_id, sid)

        res = self._session.post(url, headers=headers, json=payload)

        if res.status_code in (401, 403):
            try:
                msg = res.json().get("message", "Auth failed")
            except Exception:
                msg = res.text
            raise NaukriAuthError(msg)

        if not res.ok:
            raise NaukriParseError(f"Apply failed: {res.status_code} — {res.text}")

        try:
            return res.json()
        except Exception:
            raise NaukriParseError(f"Invalid JSON response: {res.text}")

    # ----------------------------------------------------------------------------------
    # Apply job with questionnaire answers
    # ----------------------------------------------------------------------------------

    def handle_static_questionnaire_and_apply(
        self,
        job,
        questionnaire,
        sid,
        mandatory_skills=None,
        optional_skills=None,
        source="recommended",
        profile=None,
    ) -> dict:

        DEFAULT_PROFILE = {
            "current_ctc":       "500000",
            "current_ctc_lakhs": "5",
            "expected_ctc":      "1000000",
            "expected_ctc_lakhs": "10",
            "exp_total":         "2",
            "exp_node":          "2",
            "exp_python":        "2",
            "exp_java":          "2",
            "notice_days":       30,
            "dob":               "11/08/2002",
            "ex_emp_id":         "NA",
            "skills": [
                "java", "spring boot", "react", "node", "sql", "full stack",
                "python", "docker", "microservices", "javascript", "aws", "git",
            ],
        }

        active_profile = DEFAULT_PROFILE.copy()
        if profile:
            active_profile.update(profile)

        def build_smart_answers(questionnaire: list, profile: dict) -> tuple[dict, list]:
            answers = {}
            unanswerable = []

            def pick_yes(options: dict) -> str:
                for k, v in options.items():
                    if "yes" in str(v).lower():
                        return k
                return list(options.keys())[0] if options else "1"

            def pick_notice(options: dict, notice_days: int) -> str:
                for k, v in options.items():
                    val = str(v).lower()
                    if "15" in val and notice_days <= 15:
                        return k
                    if ("30" in val or "1 month" in val) and notice_days <= 30:
                        return k
                    if ("60" in val or "2 month" in val) and notice_days <= 60:
                        return k
                    if "immediate" in val and notice_days == 0:
                        return k
                for k, v in options.items():
                    if "30" in str(v).lower() or "1 month" in str(v).lower():
                        return k
                return list(options.keys())[0] if options else "1"

            def pick_ctc(options: dict, target_lakhs: int) -> str:
                target_str = str(target_lakhs)
                for k, v in options.items():
                    if target_str in str(v).lower():
                        return k
                return list(options.keys())[0] if options else "1"

            def pick_exp(options: dict, target_years: int) -> str:
                target_str = str(target_years)
                for k, v in options.items():
                    if target_str in str(v).lower():
                        return k
                return list(options.keys())[0] if options else "1"

            for q in questionnaire:
                qid     = q["questionId"]
                qtext   = (q.get("questionName") or "").strip().lower()
                qtype   = (q.get("questionType") or "").strip().lower()
                options = q.get("answerOption") or {}
                matched = False

                if qtype in ("text box", "textbox", "text", "textarea"):
                    if any(dob_key in qtext for dob_key in ["dob", "date of birth", "birth date", "d.o.b"]):
                        matched = True
                        ans = profile.get("dob", "11/08/2002")

                    elif any(ex_key in qtext for ex_key in ["ex infy", "ex-employee", "ex employee", "employee id", "previous employee", "worked in"]):
                        matched = True
                        ans = profile.get("ex_emp_id", "NA")

                    elif any(ctc_key in qtext for ctc_key in ["current ctc", "current salary", "present ctc", "fixed ctc"]):
                        matched = True
                        if any(unit in qtext for unit in ["lakh", "lac", "lpa"]):
                            ans = profile.get("current_ctc_lakhs", "5")
                        else:
                            ans = profile.get("current_ctc", "500000")

                    elif any(ctc_key in qtext for ctc_key in ["expected ctc", "expected salary", "expectation"]):
                        matched = True
                        if any(unit in qtext for unit in ["lakh", "lac", "lpa"]):
                            ans = profile.get("expected_ctc_lakhs", "10")
                        else:
                            ans = profile.get("expected_ctc", "1000000")

                    elif any(exp_key in qtext for exp_key in ["experience", "exp", "years"]):
                        matched = True
                        if "node" in qtext:
                            ans = str(profile.get("exp_node", "2"))
                        elif "python" in qtext:
                            ans = str(profile.get("exp_python", "2"))
                        elif "java" in qtext:
                            ans = str(profile.get("exp_java", "2"))
                        else:
                            ans = str(profile.get("exp_total", "2"))

                    elif any(n_key in qtext for n_key in ["notice", "joining", "how soon"]):
                        matched = True
                        ans = str(profile.get("notice_days", 30))

                    elif any(reloc_key in qtext for reloc_key in ["relocate", "willing to", "ready to", "open to relocate", "residing"]):
                        matched = True
                        ans = "Yes"

                    elif any(yn_key in qtext for yn_key in ["are you", "do you", "have you", "can you"]):
                        matched = True
                        ans = "Yes"

                    elif any(loc_key in qtext for loc_key in ["location", "city", "current location"]):
                        matched = True
                        ans = str(profile.get("location", "Chennai"))

                    else:
                        matched = False
                        ans = "2"  # fallback
                        unanswerable.append({
                            "id": qid,
                            "question": q.get("questionName") or "",
                            "type": qtype,
                            "options": options,
                            "fallback": ans,
                        })

                else:
                    if options:
                        if any(n_key in qtext for n_key in ["notice", "joining", "how soon", "when can you join"]):
                            matched = True
                            key = pick_notice(options, int(profile.get("notice_days", 30)))
                        elif any(ctc_key in qtext for ctc_key in ["current ctc", "current salary", "present ctc"]):
                            matched = True
                            key = pick_ctc(options, 5)
                        elif any(ctc_key in qtext for ctc_key in ["expected ctc", "expected salary"]):
                            matched = True
                            key = pick_ctc(options, 10)
                        elif any(exp_key in qtext for exp_key in ["experience", "years of exp", "total exp"]):
                            matched = True
                            key = pick_exp(options, 2)
                        elif any(skill in qtext for skill in profile.get("skills", [])):
                            matched = True
                            key = pick_yes(options)
                        elif any(x in qtext for x in ["do you", "have you", "are you", "can you", "comfortable", "willing", "relocate", "hybrid", "office", "shift"]):
                            matched = True
                            key = pick_yes(options)
                        else:
                            matched = False
                            key = pick_yes(options) if any("yes" in str(v).lower() for v in options.values()) else list(options.keys())[0]
                            unanswerable.append({
                                "id": qid,
                                "question": q.get("questionName") or "",
                                "type": qtype,
                                "options": options,
                                "fallback": f"{key} ({options.get(key, '')})",
                            })

                        ans = [key]
                    else:
                        matched = False
                        ans = "1"
                        unanswerable.append({
                            "id": qid,
                            "question": q.get("questionName") or "",
                            "type": qtype,
                            "options": options,
                            "fallback": ans,
                        })

                answers[qid] = ans

            return answers, unanswerable

        answers, unanswerable_questions = build_smart_answers(questionnaire, active_profile)
        logger.debug("Generated answers: %s", answers)

        apply_src, logstr_template = APPLY_SRC_MAP.get(source, APPLY_SRC_MAP["recommended"])
        logstr = logstr_template.format(sid=sid)

        payload = {
            "strJobsarr":       [job.job_id],
            "logstr":           logstr,
            "flowtype":         "show",
            "crossdomain":      True,
            "jquery":           1,
            "rdxMsgId":         "",
            "chatBotSDK":       True,
            "mandatory_skills": mandatory_skills or [],
            "optional_skills":  optional_skills or [],
            "applyTypeId":      "107",
            "closebtn":         "y",
            "applySrc":         apply_src,
            "sid":              sid,
            "mid":              "",
            "applyData": {
                job.job_id: {
                    "answers": answers,
                }
            },
        }

        headers = self._client._build_headers(auth=True)
        headers.update({
            "appid":     "121",
            "systemid":  "jobseeker",
            "clientid":  "d3skt0p",
            "accept":    "application/json",
        })
        res = self._session.post(APPLY_JOB_URL, headers=headers, json=payload)

        if not res.ok:
            logger.debug("Apply failed: %s", res.text)
            return {"success": False, "error": res.text, "unanswerable": unanswerable_questions}

        try:
            data = res.json()
            if isinstance(data, dict):
                data["unanswerable"] = unanswerable_questions
            return data
        except Exception:
            return {"success": False, "error": "Invalid JSON response", "unanswerable": unanswerable_questions}

    # ----------------------------------------------------------------------------------
    # Recommended jobs
    # ----------------------------------------------------------------------------------

    def get_recommended_jobs(self) -> list[Job]:
        url = RECOMMENDED_JOBS_URL
        res = self._session.post(
            url,
            headers=self._headers(),
            json={
                "clusterId":       None,
                "src":             "recommClusterApi",
                "clusterSplitDate": self._cluster_dates(),
            },
        )

        if not res.ok:
            raise NaukriParseError(f"Recommended jobs fetch failed: {res.status_code}")

        data = res.json()
        raw_jobs = data.get("jobDetails") or []
        print(raw_jobs[:5])
        return [self._parse_job(j) for j in raw_jobs]

    # ----------------------------------------------------------------------------------
    # Search jobs
    # ----------------------------------------------------------------------------------

    def search_jobs(
        self,
        keyword:          str,
        location:         str = "",
        page:             int = 2,
        job_age:          int = 3,
        experience:       int = 2,
        results_per_page: int = 20,
        lat_long:         str = "",
    ) -> list[Job]:

        url     = JOB_SEARCH_URL
        seo_key = self._build_seo_key(keyword, location, page)

        params = {
            "noOfResults":    results_per_page,
            "urlType":        "search_by_keyword",
            "searchType":     "adv",
            "keyword":        keyword,
            "k":              keyword,
            "pageNo":         page,
            "experience":     experience,
            "jobAge":         job_age,
            "nignbevent_src": "jobsearchDeskGNB",
            "seoKey":         seo_key,
            "src":            "jobsearchDesk",
            "latLong":        lat_long,
        }

        res = self._session.get(url, headers=self._search_headers(), params=params)

        if res.status_code == 403:
            raise NaukriAuthError("403 Forbidden — nkparam token likely expired")

        if res.status_code == 406:
            logger.debug("406 Validation error: %s", res.text)
            return []

        if not res.ok:
            raise NaukriParseError(f"Search failed: {res.status_code} — {res.text}")

        data     = res.json()
        raw_jobs = data.get("jobDetails") or data.get("jobs") or []

        # format_jobs is called here for side-effect logging/debugging purposes.
        self.format_jobs(raw_jobs)

        if not raw_jobs:
            logger.debug("No jobs returned for keyword=%r page=%d", keyword, page)
            return []

        return [self._parse_job(j) for j in raw_jobs]