"""
console.py

Shared terminal output for the Jev agent.

The colour conventions match ``apply_agent.py`` so both agents look the same in
the terminal: cyan section headers, green success, yellow caution, red failure.
Keeping these helpers in one module means the scoring table, the job cards and
the run summary cannot drift apart stylistically.
"""

from colorama import Fore, Style, init

init(autoreset=True)

WIDTH = 74
LINE = f"{Fore.WHITE}{'─' * WIDTH}{Style.RESET_ALL}"
THIN = f"{Fore.WHITE}{'·' * WIDTH}{Style.RESET_ALL}"


def score_colour(score):
    """Green from 75, yellow from 60, red below — mirrors the strict threshold."""
    if score is None:
        return Fore.WHITE
    if score >= 75:
        return Fore.GREEN
    if score >= 60:
        return Fore.YELLOW
    return Fore.RED


def decision_colour(decision):
    return {
        "apply": Fore.GREEN,
        "review": Fore.YELLOW,
        "veto": Fore.RED,
        "error": Fore.MAGENTA,
    }.get(decision, Fore.WHITE)


def score_bar(score, width=10):
    """Small ASCII meter for a 0-100 composite score."""
    score = score or 0
    filled = max(0, min(width, int(round((score / 100.0) * width))))
    bar = "█" * filled + "░" * (width - filled)
    return f"{score_colour(score)}{bar}{Style.RESET_ALL}"


def print_banner(title, subtitle=None):
    print(f"\n{Fore.CYAN}{'═' * WIDTH}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{Style.BRIGHT}  {title}{Style.RESET_ALL}")
    if subtitle:
        print(f"{Fore.WHITE}  {subtitle}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'═' * WIDTH}{Style.RESET_ALL}")


def print_section_title(text):
    print(f"\n{LINE}")
    print(f"  {Fore.CYAN}{Style.BRIGHT}{text.upper()}{Style.RESET_ALL}")
    print(LINE)


def print_kv(label, value, colour=Fore.WHITE, label_width=24):
    print(f"  {Fore.WHITE}{label:<{label_width}}{Style.RESET_ALL} {colour}{value}{Style.RESET_ALL}")


def print_ok(message):
    print(f"  {Fore.GREEN}✔ {message}{Style.RESET_ALL}")


def print_warn(message):
    print(f"  {Fore.YELLOW}▲ {message}{Style.RESET_ALL}")


def print_error(message):
    print(f"  {Fore.RED}✘ {message}{Style.RESET_ALL}")


def print_info(message):
    print(f"  {Fore.CYAN}• {message}{Style.RESET_ALL}")


def print_quota(text):
    """Quota banner. Yellow when the budget is thin, red when it is gone."""
    used_all = "usable now=0" in text
    colour = Fore.RED if used_all else Fore.CYAN
    print(f"  {colour}QUOTA  {text}{Style.RESET_ALL}")


def print_results_table(title, results, limit=None):
    """
    Compact ranked table of scored jobs.

    Columns: rank, score, confidence, role family, title, company. Sorted input
    is assumed (the matcher already ranks best-first).
    """
    rows = results[:limit] if limit else results
    print_section_title(f"{title} ({len(results)})")

    if not rows:
        print(f"  {Fore.WHITE}none{Style.RESET_ALL}")
        return

    header = (
        f"  {Fore.WHITE}{'#':>3}  {'Score':>5}  {'Conf':>5}  "
        f"{'Role':<10}  {'Title':<34}{Style.RESET_ALL}"
    )
    print(header)
    print(f"  {Fore.WHITE}{'─' * 64}{Style.RESET_ALL}")

    for index, result in enumerate(rows, start=1):
        score = result.get("jev_score")
        conf = result.get("confidence")
        role = (result.get("role_family") or "-")[:10]
        title_text = (result.get("title") or "")[:34]
        print(
            f"  {Fore.CYAN}{index:>3}{Style.RESET_ALL}  "
            f"{score_colour(score)}{score:>5}{Style.RESET_ALL}  "
            f"{Fore.WHITE}{conf:>5.2f}{Style.RESET_ALL}  "
            f"{Fore.BLUE}{role:<10}{Style.RESET_ALL}  "
            f"{title_text:<34}"
        )

    if limit and len(results) > limit:
        print(f"  {Fore.WHITE}... {len(results) - limit} more{Style.RESET_ALL}")


def print_job_card(index, total, result):
    """Full detail block for one job, shown immediately before applying."""
    job = result.get("job")
    score = result.get("jev_score")
    decision = result.get("decision", "?")
    colour = decision_colour(decision)
    parts = result.get("parts") or {}

    print(f"\n{LINE}")
    print(
        f"  {Fore.CYAN}{Style.BRIGHT}JOB {index}/{total}{Style.RESET_ALL}"
        f"   {colour}{decision.upper()}{Style.RESET_ALL}"
    )
    print(THIN)

    title = getattr(job, "title", result.get("title", ""))
    company = getattr(job, "company", result.get("company", ""))
    job_id = getattr(job, "job_id", result.get("job_id", ""))

    print_kv("Title", f"{Style.BRIGHT}{title}{Style.RESET_ALL}")
    print_kv("Company", company, Fore.YELLOW)
    print_kv("Job ID", job_id, Fore.BLUE)
    print_kv("URL", f"https://www.naukri.com/job-listings-{job_id}", Fore.BLUE)

    print_kv(
        "Score",
        f"{score_colour(score)}{Style.BRIGHT}{score}/100{Style.RESET_ALL}  "
        f"{score_bar(score)}  {Fore.WHITE}conf {result.get('confidence', 0):.2f}{Style.RESET_ALL}",
    )
    print_kv("Role family", result.get("role_family") or "-", Fore.BLUE)

    if parts:
        detail = "  ".join(
            f"{name}={value}" for name, value in parts.items()
        )
        print_kv("Dimensions", detail, Fore.WHITE)

    if result.get("recency_bonus"):
        print_kv("Recency bonus", f"+{result['recency_bonus']}", Fore.GREEN)

    tags = getattr(job, "tags", None) or []
    if tags:
        tag_text = "  ".join(f"{Fore.CYAN}[{tag}]{Style.RESET_ALL}" for tag in tags[:8])
        print(f"  {Fore.WHITE}{'Tags':<24}{Style.RESET_ALL} {tag_text}")

    failures = result.get("failures") or []
    if failures:
        print_kv("Blocked by", failures[0], Fore.YELLOW)
        for extra in failures[1:3]:
            print(f"  {'':<24} {Fore.YELLOW}{extra}{Style.RESET_ALL}")


def print_summary(rows):
    """Final run summary: list of (label, value, colour) tuples."""
    print_section_title("run summary")
    for label, value, colour in rows:
        print(
            f"  {Fore.WHITE}{label:<32}{Style.RESET_ALL}  "
            f"{colour}{Style.BRIGHT}{value}{Style.RESET_ALL}"
        )
    print(LINE + "\n")
