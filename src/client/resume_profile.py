"""
resume_profile.py

Turns a resume PDF into the ``candidate`` block that Jev judges jobs against.

This is the piece that makes the scorer *yours*. Everything Jev is told about
you is derived from your own resume text rather than a hardcoded skill list, so
updating the PDF updates the model's view of you on the next run — no code edit,
no prompt rewrite.

Extraction uses a three-tier fallback so a missing library or an unusual PDF
never silently produces an empty profile:

    1. ``pypdf``   (declared in requirements.txt)
    2. ``PyPDF2``  (legacy, same API shape)
    3. ``fitz``    (PyMuPDF, already present on most dev machines)

The extracted text is cached in ``resume_profile.json`` keyed by the PDF's
SHA-1, so re-runs do not re-parse the file. Delete the cache (or pass
``force=True``) to pick up an edited resume.
"""

import hashlib
import json
import os
import re
from datetime import datetime


DEFAULT_CACHE_FILE = "resume_profile.json"

# Section headers we expect on a modern single-page tech resume. Used to slice
# the raw text into blocks before parsing. Matching is case-insensitive.
SECTION_HEADERS = [
    "Professional Summary",
    "Summary",
    "Skills",
    "Technical Skills",
    "Work Experience",
    "Experience",
    "Education",
    "Certifications & Achievement",
    "Certifications",
    "Project Work",
    "Projects",
]

# Composite skill labels that line the left column of a skills table, e.g.
#   "Languages  Java 17, React, TypeScript, JavaScript"
SKILL_CATEGORIES = [
    "Languages",
    "Backend",
    "Frontend",
    "Database",
    "Databases",
    "Cloud & DevOps",
    "Cloud",
    "DevOps",
    "Testing",
    "AI & ML",
    "AI",
    "Tools",
    "Frameworks",
    "Libraries",
]

# Fallback vocabulary scan. If section parsing finds fewer than MIN_PARSED_SKILLS
# skills (odd layout, two-column PDF, scanned-then-OCRed file) we scan the whole
# resume for these tokens instead. Order matters only for readability.
SKILL_VOCAB = [
    "java", "javascript", "typescript", "python", "sql", "react", "react.js",
    "next.js", "node", "node.js", "spring", "spring boot", "springboot",
    "microservices", "rest", "rest api", "rest apis", "graphql", "oracle",
    "postgresql", "mysql", "mongodb", "sql server", "jpql", "hibernate",
    "docker", "kubernetes", "openshift", "aws", "azure", "gcp",
    "github actions", "jenkins", "ci/cd", "git", "maven", "gradle",
    "selenium", "testng", "junit", "mockito", "api testing", "postman",
    "jasperreports", "apache poi", "ag grid", "zustand", "tanstack query",
    "axios", "snowflake", "snowflake cortex", "rag", "llm", "openai",
    "azure openai", "copilot", "mcp", "prompt engineering", "nlp",
    "openapi", "swagger", "jwt", "kafka", "redis", "nginx", "linux",
    "html", "css", "tailwind", "vite", "webpack", "jest",
]

MIN_PARSED_SKILLS = 8


# ----------------------------------------------------------------------
# Text extraction
# ----------------------------------------------------------------------

def _extract_with_pypdf(path):
    from pypdf import PdfReader

    reader = PdfReader(path)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_with_pypdf2(path):
    import PyPDF2

    reader = PyPDF2.PdfReader(path)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_with_fitz(path):
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def extract_resume_text(path):
    """
    Return the plain text of a resume PDF, or raise RuntimeError.

    Tries pypdf -> PyPDF2 -> PyMuPDF. Raises a clear error rather than
    returning "" so a broken resume stops the run instead of scoring every job
    against nothing.
    """
    if not os.path.exists(path):
        raise RuntimeError(f"Resume not found: {path}")

    errors = []
    for extractor in (_extract_with_pypdf, _extract_with_pypdf2, _extract_with_fitz):
        try:
            text = extractor(path)
        except ImportError as exc:
            errors.append(f"{extractor.__name__}: missing ({exc})")
            continue
        except Exception as exc:  # malformed PDF, encryption, etc.
            errors.append(f"{extractor.__name__}: {exc}")
            continue

        text = (text or "").strip()
        if len(text) >= 100:
            return text
        errors.append(f"{extractor.__name__}: only {len(text)} chars extracted")

    raise RuntimeError(
        "Could not extract text from the resume PDF. "
        "Install pypdf (pip install pypdf) or export the resume with a text "
        "layer (not a scan).\n  " + "\n  ".join(errors)
    )


# ----------------------------------------------------------------------
# Section splitting
# ----------------------------------------------------------------------

def split_sections(text):
    """
    Slice the resume into ``{section_name: body}`` using SECTION_HEADERS.

    Anything before the first recognised header is kept under ``__header__``
    (name, headline, contact line). Unrecognised headers simply accumulate into
    the previous section, which is harmless for our purposes.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    lookup = {h.lower(): h for h in SECTION_HEADERS}

    sections = {"__header__": []}
    current = "__header__"

    for line in lines:
        stripped = line.strip()
        key = stripped.lower().rstrip(":")

        # A header is a whole line that matches a known section title.
        if key in lookup:
            current = lookup[key]
            sections.setdefault(current, [])
            continue

        sections.setdefault(current, []).append(line)

    return {
        name: "\n".join(body).strip()
        for name, body in sections.items()
        if "\n".join(body).strip()
    }


def _clean_skill(token):
    token = token.strip(" .,;:|•–—-")
    token = re.sub(r"\s+", " ", token)
    return token


def parse_skill_sections(text):
    """
    Parse ``{category: [skill, ...]}`` from the Skills block.

    Handles the common layout where each line is ``<Category>  <comma list>``.
    Returns an empty dict when the layout is unrecognisable so the caller can
    fall back to a vocabulary scan.
    """
    sections = split_sections(text)
    block = sections.get("Skills") or sections.get("Technical Skills") or ""
    if not block:
        return {}

    parsed = {}
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue

        category = None
        remainder = line
        for known in SKILL_CATEGORIES:
            if line.lower().startswith(known.lower()):
                category = known
                remainder = line[len(known):]
                break

        if category is None:
            # No label: treat the whole line as a flat skill list.
            category = "General"

        skills = [
            _clean_skill(s)
            for s in remainder.split(",")
            if _clean_skill(s)
        ]
        if skills:
            parsed.setdefault(category, []).extend(skills)

    return parsed


def scan_vocabulary(text):
    """Return the set of SKILL_VOCAB terms literally present in the text."""
    lowered = text.lower()
    found = []
    for term in SKILL_VOCAB:
        if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", lowered):
            found.append(term)
    return found


# ----------------------------------------------------------------------
# Field parsers
# ----------------------------------------------------------------------

CITY_NAMES = [
    "Chennai", "Bangalore", "Bengaluru", "Hyderabad", "Pune", "Mumbai",
    "Delhi", "Noida", "Gurgaon", "Gurugram", "Coimbatore", "Kochi",
    "Kolkata", "Ahmedabad", "Trivandrum", "Thiruvananthapuram", "Jaipur",
]

ROLE_WORDS = (
    "developer", "engineer", "analyst", "architect", "consultant",
    "programmer", "scientist", "intern",
)


def parse_years_experience(text):
    """
    Extract total years of experience from the summary.

    Handles "~2 years", "2.3 years", "3+ yrs", "1 year". Defaults to 0 when the
    resume never states it, which makes the experience gate conservative.
    """
    # Look at the summary first; fall back to the whole document.
    sections = split_sections(text)
    summary = (
        sections.get("Professional Summary")
        or sections.get("Summary")
        or ""
    )
    for blob in (summary, text):
        if not blob:
            continue
        match = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", blob, re.I)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return 0.0


def parse_headline(text):
    """Find the role line at the top of the resume (e.g. 'Full Stack Developer')."""
    sections = split_sections(text)
    header = sections.get("__header__", "")
    lines = [ln.strip() for ln in header.splitlines() if ln.strip()]

    # Skip the candidate's name (first line) and look for a role-ish line.
    for line in lines[1:6]:
        lowered = line.lower()
        if any(word in lowered for word in ROLE_WORDS):
            return _clean_skill(line)

    return lines[1] if len(lines) > 1 else ""


def parse_location(text):
    """Return the first known city mentioned in the work-experience block."""
    sections = split_sections(text)
    for key in ("Work Experience", "Experience", "__header__"):
        blob = sections.get(key, "")
        for city in CITY_NAMES:
            if re.search(r"(?<![a-zA-Z])" + city + r"(?![a-zA-Z])", blob):
                return city
    for city in CITY_NAMES:
        if city in text:
            return city
    return ""


def parse_roles(text):
    """
    Return ``(company, current_role, period)`` from the first job entry.

    The common layout is three consecutive lines::

        Coforge, Chennai
        Aug 2024 – Present
        Software Engineer (Full Stack Developer) - DHL - ...

    Falls back to empty strings if the layout differs.
    """
    sections = split_sections(text)
    block = sections.get("Work Experience") or sections.get("Experience") or ""
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if not lines:
        return "", "", ""

    company = lines[0].split(",")[0].strip()
    period = ""
    role = ""

    for line in lines[1:5]:
        if re.search(r"(19|20)\d{2}", line) and not period:
            period = _clean_skill(line)
            continue
        if not role and any(w in line.lower() for w in ROLE_WORDS):
            role = _clean_skill(line)

    return company, role, period


def parse_proof_points(text, limit=3):
    """
    Pull the quantified achievement bullets from the work-experience block.

    A strong claim ("migrated 160+ endpoints") gives Jev honest evidence for the
    seniority question, so we keep the bullets that actually contain numbers.
    """
    sections = split_sections(text)
    block = sections.get("Work Experience") or sections.get("Experience") or ""

    points = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] not in "–—-•":
            continue
        cleaned = _clean_skill(stripped)
        if not re.search(r"\d", cleaned):
            continue
        points.append(cleaned[:180])
        if len(points) >= limit:
            break

    return points


def parse_certifications(text):
    """Split the certifications line into individual entries."""
    sections = split_sections(text)
    block = (
        sections.get("Certifications & Achievement")
        or sections.get("Certifications")
        or ""
    )
    if not block:
        parts = []
        for line in text.splitlines():
            if "certif" in line.lower() and "—" in line:
                parts = line.split("—")
                break
        return [_clean_skill(p) for p in parts if _clean_skill(p)]

    line = " ".join(block.split())
    parts = re.split(r"\s*[—–]\s*", line)
    return [_clean_skill(p) for p in parts if _clean_skill(p)]


def parse_education(text):
    """Return the education block as a single readable line."""
    sections = split_sections(text)
    block = sections.get("Education") or ""
    if not block:
        return ""
    return _clean_skill(" / ".join(ln.strip() for ln in block.splitlines() if ln.strip()))


def _split_outside_parens(text, separator=","):
    """
    Split on ``separator`` but ignore separators nested inside brackets.

    Needed for entries like ``Snowflake Cortex (Vector Embeddings, LLMs)`` which
    would otherwise be torn into two bogus skills.
    """
    parts, buf, depth = [], [], 0
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char == separator and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(char)
    parts.append("".join(buf))
    return parts


def parse_project_skills(text):
    """Return skills listed on a project's ``Tech Stack:`` line."""
    sections = split_sections(text)
    block = sections.get("Project Work") or sections.get("Projects") or ""
    if not block:
        return []

    found = []
    for line in block.splitlines():
        if "tech stack" not in line.lower():
            continue
        _, _, tail = line.partition(":")
        for token in _split_outside_parens(tail):
            cleaned = _clean_skill(token)
            if cleaned:
                found.append(cleaned)
    return found


# ----------------------------------------------------------------------
# Candidate state
# ----------------------------------------------------------------------

# Categories that represent day-to-day professional work versus supporting tools.
CORE_CATEGORIES = ("Languages", "Backend", "Frontend", "Database", "Databases",
                   "Frameworks", "Libraries", "General")
SECONDARY_CATEGORIES = ("Cloud & DevOps", "Cloud", "DevOps", "Tools")
TESTING_CATEGORIES = ("Testing",)
AI_CATEGORIES = ("AI & ML", "AI")

DEFAULT_OPEN_TO = ["Chennai", "Bangalore", "Hyderabad", "Pune"]


def _dedupe(items):
    seen, out = set(), []
    for item in items:
        key = item.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def build_candidate_state(text, open_to=None):
    """
    Build the ``candidate`` dict that travels with every Jev request.

    Keys are named fields (not a blob) because TypeSafe's own guidance is to use
    a structured ``state`` object so each part has a descriptive name. The full
    resume text is included verbatim as ground truth, so Jev can always fall
    back to the source rather than trusting our parsing.
    """
    skills_by_cat = parse_skill_sections(text)

    def take(categories):
        out = []
        for cat in categories:
            out.extend(skills_by_cat.get(cat, []))
        return _dedupe([_clean_skill(s) for s in out if _clean_skill(s)])

    core = take(CORE_CATEGORIES)
    secondary = take(SECONDARY_CATEGORIES)
    testing = take(TESTING_CATEGORIES)
    ai_tooling = take(AI_CATEGORIES)

    # Layout fallback: if section parsing found almost nothing, scan the whole
    # document for known tech terms so the scorer is never handed an empty stack.
    if len(core) + len(secondary) + len(testing) + len(ai_tooling) < MIN_PARSED_SKILLS:
        vocab = scan_vocabulary(text)
        core = _dedupe(core + [v.title() for v in vocab])

    company, role, period = parse_roles(text)

    current_role = role or parse_headline(text)
    if company and role:
        current_role = f"{role} at {company}"

    state = {
        "headline": parse_headline(text),
        "years_experience": parse_years_experience(text),
        "current_role": current_role,
        "current_company": company,
        "current_period": period,
        "location": parse_location(text),
        "open_to": open_to or DEFAULT_OPEN_TO,
        "core_skills": core,
        "secondary_skills": secondary,
        "testing_skills": testing,
        "ai_tooling": ai_tooling,
        "project_skills": _dedupe(parse_project_skills(text)),
        "education": parse_education(text),
        "certifications": parse_certifications(text),
        "proof_points": parse_proof_points(text),
        "resume_text": text,
    }

    # Drop empty values so the state we send stays compact and unambiguous.
    return {k: v for k, v in state.items() if v not in ("", [], None)}


# ----------------------------------------------------------------------
# Cache + entry point
# ----------------------------------------------------------------------

def _sha1_of_file(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_resume_profile(path="resume.pdf", cache_file=DEFAULT_CACHE_FILE, force=False):
    """
    Return the parsed candidate profile, using a SHA-1 keyed cache.

    The cache exists purely for speed: parsing a PDF is fast, but the agent may
    be launched several times a day and there is no reason to touch the file
    each time. Editing the resume changes its hash, which invalidates the cache
    automatically — no manual clearing needed.
    """
    if not os.path.exists(path):
        raise RuntimeError(f"Resume not found: {path}")

    current_hash = _sha1_of_file(path)

    if cache_file and not force and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as handle:
                cached = json.load(handle)
            if cached.get("sha1") == current_hash and cached.get("profile"):
                return cached["profile"]
        except (ValueError, OSError):
            pass  # corrupt cache: fall through and rebuild

    text = extract_resume_text(path)
    profile = build_candidate_state(text)

    if cache_file:
        payload = {
            "source": path,
            "sha1": current_hash,
            "chars": len(text),
            "extracted_at": datetime.utcnow().isoformat(),
            "profile": profile,
        }
        try:
            with open(cache_file, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
        except OSError:
            pass  # a read-only FS should not stop the run

    return profile


if __name__ == "__main__":
    # Quick manual check: python3 -m src.client.resume_profile [path]
    import sys

    resume_path = sys.argv[1] if len(sys.argv) > 1 else "resume.pdf"
    text = extract_resume_text(resume_path)
    print(f"extracted {len(text)} chars from {resume_path}\n")

    profile = build_candidate_state(text)
    for key, value in profile.items():
        if key == "resume_text":
            print(f"{key}: <{len(value)} chars>")
        else:
            print(f"{key}: {value}")
