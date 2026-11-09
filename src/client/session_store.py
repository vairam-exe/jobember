"""
session_store.py

Persist the Naukri ``nauk_at`` bearer token between runs.

Why this exists
---------------
Every endpoint that mints a fresh ``nauk_at`` — ``/central-login-services/v1/login``
and ``/central-login-services/v1/otp`` — answers with::

    {"validRequest": false, "captchaSiteKey": "..."}

Verified against httpcloak, curl_cffi (chrome124) and plain requests, while plain
page loads from the same IP still return 200. In other words the challenge is a
reCAPTCHA gate on token minting itself, not an IP block, a fingerprint problem or
a bad password. There is currently no way to obtain a token headlessly without
solving that captcha.

Also verified: the session does **not** slide. An authenticated history call
returns 200 and sets ``_t_ds`` / ``J`` / ``ak_bmsc``, but never a new ``nauk_at``.
So the JWT's 60-minute ``exp`` is absolute.

What this module does about it
------------------------------
It cannot mint tokens, but it removes the "paste the cookie every run" tax:

  * Decode the JWT to learn exactly when it dies (no signature check needed —
    Naukri verifies; we only need the claim).
  * Cache the token in ``.naukri_session.json`` (mode 0600, gitignored) so
    repeated runs inside the same hour need no input at all.
  * Report the remaining lifetime so a long run can warn before it dies.

The only thing still requiring a human is minting a *new* token once its hour is
up, and ``browser_token()`` below automates even that when a Chrome profile is
already logged in to naukri.com.
"""

import base64
import json
import os
from datetime import datetime, timedelta, timezone


SESSION_FILE = ".naukri_session.json"

# Treat a token as dead slightly early so we never start a run that expires
# mid-way through applying.
EXPIRY_SKEW_SECONDS = 120


def _now():
    return datetime.now(timezone.utc)


def decode_jwt_payload(token):
    """
    Return the JWT payload dict, or {} if it is not a decodable JWT.

    The signature is deliberately not verified: we only need ``exp`` and a few
    display claims, and Naukri is the one that validates authenticity on every
    API call.
    """
    if not token or token.count(".") < 2:
        return {}

    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)

    try:
        raw = base64.urlsafe_b64decode(payload)
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError, base64.binascii.Error):
        return {}


def token_expiry(token):
    """Return the token's expiry as an aware UTC datetime, or None."""
    exp = decode_jwt_payload(token).get("exp")
    try:
        return datetime.fromtimestamp(int(exp), timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def minutes_remaining(token):
    """Minutes until expiry, or None when the token carries no ``exp``."""
    expiry = token_expiry(token)
    if expiry is None:
        return None
    return (expiry - _now()).total_seconds() / 60.0


def is_expired(token, skew_seconds=EXPIRY_SKEW_SECONDS):
    """True when the token is past (or within ``skew_seconds`` of) expiry."""
    expiry = token_expiry(token)
    if expiry is None:
        # Not a JWT: fall back to "we cannot prove it is dead, so try it". The
        # agent validates with a real API call anyway.
        return False
    return expiry <= _now() + timedelta(seconds=skew_seconds)


def describe(token):
    """Human-readable one-liner about a token's lifetime."""
    expiry = token_expiry(token)
    if expiry is None:
        return "unknown expiry (not a JWT)"

    remaining = minutes_remaining(token)
    stamp = expiry.strftime("%H:%M:%S UTC")
    if remaining is None:
        return f"expires {stamp}"
    if remaining <= 0:
        return f"EXPIRED at {stamp}"
    return f"valid for {remaining:.0f} more min (expires {stamp})"


def extract_token(raw):
    """
    Pull the ``nauk_at`` value out of whatever the caller pasted.

    Accepts a bare token, ``nauk_at=<token>``, or a full copied cookie string
    such as ``'a=1; nauk_at=<token>; b=2'``. Returns "" when nothing usable is
    present.
    """
    text = (raw or "").strip()
    if not text:
        return ""

    if "nauk_at=" in text:
        token = text.split("nauk_at=", 1)[1].split(";", 1)[0].strip()
    else:
        token = text.split(";", 1)[0].strip()

    return token


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------

def load_session(path=SESSION_FILE):
    """
    Return the stored session dict, or None when absent/unreadable.

    The dict is returned as-is even when the token has expired; ``resolve_token``
    and the agent decide what to do with it so the caller can still show the
    expiry time to the user.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) and data.get("nauk_at") else None


def save_session(token, path=SESSION_FILE, source="unknown"):
    """
    Persist a token with its decoded metadata. Returns True on success.

    Written with mode 0600 because the token is a live credential. The file is
    gitignored; this is only about keeping other local users out.
    """
    if not token:
        return False

    payload = decode_jwt_payload(token)
    expiry = token_expiry(token)
    issued = payload.get("iat")

    record = {
        "nauk_at": token,
        "email": payload.get("ud_email") or payload.get("ud_username"),
        "issued_at": (
            datetime.fromtimestamp(int(issued), timezone.utc).isoformat()
            if issued else None
        ),
        "expires_at": expiry.isoformat() if expiry else None,
        "saved_at": _now().isoformat(),
        "source": source,
    }

    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)

        # Create with 0600 from the outset rather than chmod-ing after the fact,
        # so the token is never briefly world-readable.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
        return True
    except OSError:
        return False


def clear_session(path=SESSION_FILE):
    """Delete the stored session. Returns True if a file was removed."""
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def resolve_token(cli_cookie="", env_cookie="", path=SESSION_FILE):
    """
    Pick the best available token, in order of freshness of intent.

    Priority:
      1. ``--cookie`` on the command line (explicit beats implicit)
      2. ``NAUKRI_COOKIE`` in the environment
      3. the saved session file

    Expired tokens are skipped rather than returned, so a stale value left in
    ``.env`` can never shadow a still-valid cached session.

    ``rejected`` carries an expired token only when *nothing* usable was found,
    so the caller can explain why a session went cold. When a good token exists
    there is nothing to report and it stays empty.

    Returns ``(token, source, rejected_expired_token)``; ``source`` is one of
    ``cli``, ``env``, ``cache`` or ``""``.
    """
    rejected = ""

    for raw, source in ((cli_cookie, "cli"), (env_cookie, "env")):
        token = extract_token(raw)
        if not token:
            continue
        if not is_expired(token):
            return token, source, ""
        rejected = rejected or token

    stored = load_session(path)
    if stored:
        token = stored.get("nauk_at") or ""
        if token:
            if not is_expired(token):
                return token, "cache", ""
            rejected = rejected or token

    return "", "", rejected


# ----------------------------------------------------------------------
# Browser-assisted refresh
#
# The one route to a fresh token that does not involve a captcha or DevTools is
# a browser that is already signed in: Naukri re-issues `nauk_at` to a real
# browser session, and Selenium can read it straight from the cookie jar. This
# uses a dedicated profile so it never touches your everyday Chrome profile (and
# therefore never fights it for the profile lock after the first login).
# ----------------------------------------------------------------------

DEFAULT_PROFILE_DIR = ".chrome-naukri-profile"


def browser_token(profile_dir=None, headless=True, timeout=25,
                  url="https://www.naukri.com/", verbose=False):
    """
    Read ``nauk_at`` from a Chrome profile via Selenium.

    Returns ``(token, message)``. On the very first use the profile is empty, so
    run with ``headless=False`` (``--refresh-browser --show-browser``) and log in
    once by hand; every later call reuses that profile.

    This is best-effort by design: it returns ("", reason) instead of raising so
    the caller can fall back to the saved session or to a pasted cookie.
    """
    profile_dir = os.path.abspath(profile_dir or DEFAULT_PROFILE_DIR)

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        return "", "selenium is not installed (pip install selenium)"

    options = Options()
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,900")

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(timeout)
        try:
            driver.get(url)
        except Exception:
            # Page-load timeouts are common on Naukri; the cookies may still be
            # set, so carry on and check the jar.
            pass

        cookies = {cookie.get("name"): cookie.get("value")
                   for cookie in driver.get_cookies() or []}
        token = extract_token(cookies.get("nauk_at", ""))

        if verbose:
            names = ", ".join(sorted(name for name in cookies if name)) or "none"
            return token, f"cookies seen: {names}"

        return token, ("token found" if token else "nauk_at cookie absent")
    except Exception as exc:
        return "", f"browser launch failed ({exc})"
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    # Quick check: python3 -m src.client.session_store [token-or-cookie-string]
    import sys

    sample = sys.argv[1] if len(sys.argv) > 1 else ""
    if sample:
        found = extract_token(sample)
        print("token   :", found[:24] + "..." + found[-8:] if found else "(none)")
        print("expiry  :", describe(found))
        print("claims  :", {k: v for k, v in decode_jwt_payload(found).items()
                            if k in ("ud_email", "exp", "iat", "ipAddress")})
    stored = load_session()
    print("saved   :", describe(stored["nauk_at"]) if stored else "(no saved session)")
