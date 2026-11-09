"""
test_jev_matcher.py

Offline tests for the Jev composite scorer and its strict gates.

Run from the repo root:

    python3 src/tests/test_jev_matcher.py

No network access and no API key are required: a fake Jev client returns canned
typed answers, so the weighting and every gate are pinned deterministically.
The gate behaviour matters more than the exact scores — one wrong "apply" costs
a slice of the 50/day quota, and a support role must never slip through.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.client import jev_matcher
from src.client.jev_matcher import JevJobMatcher, days_old_from_posted, normalise_score
from src.models.models import Job


class FakeJev:
    """Stand-in for JevClient: returns whatever answers the test supplies."""

    def __init__(self, payload):
        self.payload = payload
        self.model = "jev-fake"
        self.calls = 0

    def ask(self, state, questions, model=None):
        self.calls += 1
        return self.payload

    def ask_full(self, state, questions, model=None):
        return {"model": self.model, "answers": self.payload, "usage": {}}

    @property
    def estimated_cost_usd(self):
        return 0.0

    def usage_summary(self):
        return "fake"


CANDIDATE = {
    "headline": "Full Stack Developer",
    "years_experience": 2,
    "core_skills": ["Java 17", "Spring Boot", "React", "TypeScript", "Oracle"],
    "secondary_skills": ["Docker", "OpenShift", "GitHub Actions"],
    "location": "Chennai",
}


def make_answers(
    dev=0.98, support=0.02, role="backend", stack=0.92,
    skill=4.0, experience=4.0, seniority=3.0, should=4.0, confidence=0.90,
):
    """Build a full typed answer set shaped exactly like a real Jev response."""
    return {
        "is_developer": {"type": "noul", "noul": dev},
        "is_support_role": {"type": "noul", "noul": support},
        "role_family": {
            "type": "choice", "choice": role, "confidence": confidence,
            "probabilities": {role: confidence},
        },
        "matches_resume_stack": {"type": "noul", "noul": stack},
        "skill_overlap": {"type": "score", "score": skill, "confidence": confidence},
        "experience_fit": {"type": "score", "score": experience, "confidence": confidence},
        "seniority_fit": {"type": "score", "score": seniority, "confidence": confidence},
        "should_apply": {"type": "score", "score": should, "confidence": confidence},
    }


def matcher(answers):
    return JevJobMatcher(
        jev=FakeJev(answers), candidate=CANDIDATE, cache_file=None, verbose=False
    )


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------

def test_perfect_match_is_eligible():
    verdict = matcher(make_answers()).gate(make_answers(), days_old=5)
    assert verdict["decision"] == "apply", verdict["failures"]
    assert verdict["failures"] == []
    assert verdict["jev_score"] >= 90, verdict["jev_score"]
    assert verdict["role_family"] == "backend"


def test_fullstack_is_allowed():
    verdict = matcher(make_answers()).gate(
        make_answers(role="fullstack"), days_old=3
    )
    assert verdict["decision"] == "apply", verdict["failures"]


# ----------------------------------------------------------------------
# Hard vetoes — these must never reach "apply"
# ----------------------------------------------------------------------

def test_support_role_blocked():
    verdict = matcher(make_answers()).gate(
        make_answers(support=0.87, role="support", should=2.0), days_old=0
    )
    assert verdict["decision"] != "apply"
    assert any("support" in reason for reason in verdict["failures"])


def test_qa_role_blocked():
    verdict = matcher(make_answers()).gate(
        make_answers(role="qa", stack=0.08, skill=2.0, should=2.0), days_old=1
    )
    assert verdict["decision"] != "apply"
    assert any("role family" in reason for reason in verdict["failures"])


def test_frontend_allowed():
    verdict = matcher(make_answers()).gate(
        make_answers(role="frontend"), days_old=2
    )
    assert verdict["decision"] == "apply"
    assert "role_family" not in str(verdict["failures"])


def test_non_developer_blocked():
    verdict = matcher(make_answers()).gate(
        make_answers(dev=0.40), days_old=2
    )
    assert verdict["decision"] != "apply"
    assert any("not a developer" in reason for reason in verdict["failures"])


def test_weak_stack_match_blocked():
    verdict = matcher(make_answers()).gate(
        make_answers(stack=0.20), days_old=2
    )
    assert verdict["decision"] != "apply"


def test_low_confidence_blocked():
    verdict = matcher(make_answers()).gate(
        make_answers(confidence=0.30), days_old=2
    )
    assert verdict["decision"] != "apply"
    assert any("confidence" in reason for reason in verdict["failures"])


def test_low_skill_overlap_blocked():
    verdict = matcher(make_answers()).gate(
        make_answers(skill=1.0, should=1.0), days_old=2
    )
    assert verdict["decision"] != "apply"


def test_threshold_is_respected():
    strict = JevJobMatcher(
        jev=FakeJev(make_answers()),
        candidate=CANDIDATE,
        cache_file=None,
        min_apply_score=100,
        verbose=False,
    )
    verdict = strict.gate(make_answers(), days_old=20)
    assert verdict["decision"] != "apply"
    assert any("score" in reason for reason in verdict["failures"])


# ----------------------------------------------------------------------
# Maths
# ----------------------------------------------------------------------

def test_normalise_score_clamps():
    assert normalise_score({"score": 4.0}, "skill_overlap") == 1.0
    assert normalise_score({"score": 0}, "skill_overlap") == 0.0
    assert normalise_score({"score": 2.0}, "skill_overlap") == 0.5
    assert normalise_score({"score": 99}, "skill_overlap") == 1.0   # clamped
    assert normalise_score({}, "skill_overlap") == 0.0
    assert normalise_score(None, "skill_overlap") == 0.0


def test_composite_weights():
    instance = matcher(make_answers())
    # All dimensions top out, no recency bonus: the composite must be 100.
    top = make_answers(seniority=4.0)
    score, parts, bonus = instance.composite_score(top, days_old=10)
    assert score == 100, (score, parts)
    assert parts == {
        "skill_overlap": 100, "experience_fit": 100,
        "seniority_fit": 100, "should_apply": 100,
    }
    assert bonus == 0

    # The default answer set has seniority at level 3 of 4, so it must land below.
    default_score = instance.composite_score(make_answers(), days_old=10)[0]
    assert 90 <= default_score < 100, default_score

    # skill_overlap carries the most weight: dropping only it must hurt most.
    low_skill = instance.composite_score(make_answers(skill=0.0), days_old=10)[0]
    low_seniority = instance.composite_score(make_answers(seniority=0.0), days_old=10)[0]
    assert low_skill < low_seniority, (low_skill, low_seniority)


def test_recency_bonus_is_capped():
    instance = matcher(make_answers())
    fresh = instance.composite_score(make_answers(), days_old=0)
    stale = instance.composite_score(make_answers(), days_old=10)
    assert fresh[0] == 100          # capped, never above 100
    assert fresh[2] == jev_matcher.RECENCY_BONUS
    assert stale[2] == 0


def test_days_old_parsing():
    assert days_old_from_posted("Today") == 0
    assert days_old_from_posted("Just now") == 0
    assert days_old_from_posted("2 hours ago") == 0
    assert days_old_from_posted("Yesterday") == 1
    assert days_old_from_posted("3 Days Ago") == 3
    assert days_old_from_posted("2 Weeks Ago") == 14
    assert days_old_from_posted("") == 7
    assert days_old_from_posted("N/A") == 7


# ----------------------------------------------------------------------
# score_job wiring
# ----------------------------------------------------------------------

def test_score_job_attaches_metadata():
    job = Job(
        job_id="42", title="Java Developer", company="Acme", location="Chennai",
        experience="1-3 Yrs", salary="8-12 Lacs", posted_date="Today",
        apply_link="", tags=["java", "spring boot"], description="Build APIs.",
    )
    fake = FakeJev(make_answers())
    instance = JevJobMatcher(
        jev=fake, candidate=CANDIDATE, cache_file=None, verbose=False
    )
    result = instance.score_job(job, use_cache=False)

    assert fake.calls == 1
    assert result["job_id"] == "42"
    assert result["title"] == "Java Developer"
    assert result["decision"] == "apply"
    assert result["days_old"] == 0
    # The cached payload must stay JSON-serialisable (no Job object inside).
    assert "job" not in result


def test_questions_shape_is_valid_for_the_api():
    """Every question must carry a type the API accepts, with sane criteria."""
    instance = matcher(make_answers())
    questions = instance.build_questions()

    assert set(questions) == {
        "is_developer", "is_support_role", "role_family", "matches_resume_stack",
        "skill_overlap", "experience_fit", "seniority_fit", "should_apply",
    }

    for name, question in questions.items():
        assert question["type"] in {"noul", "choice", "score"}, name
        if question["type"] == "score":
            levels = question["criteria"]
            assert 2 <= len(levels) <= 10, f"{name} has {len(levels)} levels"
        if question["type"] == "choice":
            assert isinstance(question["criteria"], dict)
            assert len(question["criteria"]) <= 255


# ----------------------------------------------------------------------
# Cache
#
# Regression: score_jobs() attaches the live Job object to each result dict. If
# the cache stored that same dict, saving it raised
# "TypeError: Object of type Job is not JSON serializable" and killed the run.
# ----------------------------------------------------------------------

def test_cache_is_json_safe_and_reused():
    import json
    import tempfile

    job = Job(
        job_id="77", title="Java Developer", company="Acme", location="Chennai",
        experience="1-3 Yrs", salary="8-12 Lacs", posted_date="Today",
        apply_link="", tags=["java"], description="Build APIs.",
    )
    candidate = {"years_experience": 2, "core_skills": ["Java"]}

    with tempfile.TemporaryDirectory() as tmp:
        cache = os.path.join(tmp, "cache.json")

        fake = FakeJev(make_answers())
        first = JevJobMatcher(
            jev=fake, candidate=candidate, cache_file=cache,
            profile_fingerprint="fp1", verbose=False,
        )
        first.score_jobs([job])
        assert os.path.exists(cache), "cache file was never written"
        assert fake.calls == 1

        # The Job object must never end up inside the serialised cache.
        with open(cache, encoding="utf-8") as handle:
            stored = json.load(handle)
        assert "job" not in stored["77"]["result"]

        # A second run with the same resume must hit the cache, not Jev.
        second = JevJobMatcher(
            jev=fake, candidate=candidate, cache_file=cache,
            profile_fingerprint="fp1", verbose=False,
        )
        results = second.score_jobs([job])
        assert fake.calls == 1, "cache was not reused"
        assert results[0]["cached"] is True

        # Re-evaluating cached answers with an impossible threshold
        stricter = JevJobMatcher(
            jev=fake, candidate=candidate, cache_file=cache,
            profile_fingerprint="fp1", min_apply_score=101, verbose=False,
        )
        results_stricter = stricter.score_jobs([job])
        assert fake.calls == 1, "cache was not reused on stricter threshold"
        assert results_stricter[0]["cached"] is True
        assert results_stricter[0]["decision"] == "review"
        assert any("score" in f for f in results_stricter[0]["failures"])

        # A different resume invalidates the entry.
        third = JevJobMatcher(
            jev=fake, candidate=candidate, cache_file=cache,
            profile_fingerprint="fp2", verbose=False,
        )
        third.score_jobs([job])
        assert fake.calls == 2, "resume change did not invalidate the cache"


def main():
    tests = [
        test_perfect_match_is_eligible,
        test_fullstack_is_allowed,
        test_support_role_blocked,
        test_qa_role_blocked,
        test_frontend_allowed,
        test_non_developer_blocked,
        test_weak_stack_match_blocked,
        test_low_confidence_blocked,
        test_low_skill_overlap_blocked,
        test_threshold_is_respected,
        test_normalise_score_clamps,
        test_composite_weights,
        test_recency_bonus_is_capped,
        test_days_old_parsing,
        test_score_job_attaches_metadata,
        test_questions_shape_is_valid_for_the_api,
        test_cache_is_json_safe_and_reused,
    ]
    for test in tests:
        test()
        print(f"  PASS  {test.__name__}")
    print(f"\n{len(tests)} matcher tests passed")


if __name__ == "__main__":
    main()
