"""
test_session_store.py

Offline tests for the Naukri session persistence layer.

Run from the repo root:

    python3 src/tests/test_session_store.py

The parser and the token-resolution order are the risky parts: a wrong priority
would either use a dead token or silently ignore a good one, and either mistake
shows up as a confusing 401 later in a run.
"""

import base64
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.client import session_store


def make_jwt(exp_offset_minutes=60, email="test@example.com", issued_offset_minutes=-1):
    """Build an unsigned JWT-shaped string with real exp/iat claims."""
    now = datetime.now(timezone.utc)
    payload = {
        "ud_email": email,
        "exp": int((now + timedelta(minutes=exp_offset_minutes)).timestamp()),
        "iat": int((now + timedelta(minutes=issued_offset_minutes)).timestamp()),
        "ipAddress": "2404:8ec0::1",
    }

    def b64(data):
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{b64({'alg': 'RS512', 'typ': 'JWT'})}.{b64(payload)}.fakesignature"


def test_extract_token():
    token = "eyJhbGciOiJSUzUxMiJ9.eyJleHAiOjF9.sig"
    assert session_store.extract_token(f"nauk_at={token}; x=1") == token
    assert session_store.extract_token(token) == token
    assert session_store.extract_token(f"  nauk_at={token}  ") == token
    assert session_store.extract_token("a=1; nauk_at=" + token) == token
    assert session_store.extract_token("nauk_at=") == ""
    assert session_store.extract_token("") == ""
    assert session_store.extract_token(None) == ""


def test_decode_claims():
    token = make_jwt(email="naresh@example.com")
    claims = session_store.decode_jwt_payload(token)
    assert claims["ud_email"] == "naresh@example.com"
    assert "exp" in claims and "iat" in claims

    # Malformed input must never raise.
    for junk in ("", "a.b", "not-a-jwt", "a.b.c"):
        assert session_store.decode_jwt_payload(junk) == {}


def test_expiry_maths():
    live = make_jwt(exp_offset_minutes=60)
    dead = make_jwt(exp_offset_minutes=-5)

    assert session_store.token_expiry(live) is not None
    assert session_store.minutes_remaining(live) > 55
    assert not session_store.is_expired(live)

    assert session_store.minutes_remaining(dead) < 0
    assert session_store.is_expired(dead)

    # A non-JWT must not be declared dead on spec; the API call decides.
    assert not session_store.is_expired("opaque-token")

    assert "EXPIRED" in session_store.describe(dead)
    assert "valid for" in session_store.describe(live)
    assert "unknown expiry" in session_store.describe("opaque-token")


def test_save_load_clear():
    token = make_jwt()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.json")

        assert session_store.load_session(path) is None
        assert session_store.save_session(token, path=path, source="test") is True

        stored = session_store.load_session(path)
        assert stored["nauk_at"] == token
        assert stored["email"] == "test@example.com"
        assert stored["source"] == "test"
        assert stored["expires_at"]

        # The token is a live credential: the file must not be world-readable.
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, oct(mode)

        assert session_store.clear_session(path) is True
        assert session_store.load_session(path) is None
        assert session_store.clear_session(path) is False


def test_corrupt_session_is_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        assert session_store.load_session(path) is None

        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"no_token": True}, handle)
        assert session_store.load_session(path) is None


def test_resolve_priority():
    live = make_jwt(exp_offset_minutes=60)
    other_live = make_jwt(exp_offset_minutes=45, email="other@example.com")
    dead = make_jwt(exp_offset_minutes=-10)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "session.json")

        # Nothing anywhere.
        assert session_store.resolve_token(path=path) == ("", "", "")

        # Explicit CLI cookie wins over env and cache.
        session_store.save_session(live, path=path)
        token, source, stale = session_store.resolve_token(
            cli_cookie=f"nauk_at={other_live}",
            env_cookie=f"nauk_at={live}",
            path=path,
        )
        assert token == other_live and source == "cli" and stale == ""

        # Env wins over cache.
        token, source, _ = session_store.resolve_token(
            cli_cookie="", env_cookie=f"nauk_at={other_live}", path=path
        )
        assert token == other_live and source == "env"

        # Cache is used when nothing is supplied.
        token, source, _ = session_store.resolve_token(path=path)
        assert token == live and source == "cache"

        # A dead env cookie must NOT shadow a valid cached session. Because a
        # usable session was found, there is nothing to warn about, so `stale`
        # stays empty even though the env value was expired.
        token, source, stale = session_store.resolve_token(
            env_cookie=f"nauk_at={dead}", path=path
        )
        assert token == live and source == "cache" and stale == ""

        # With only dead tokens around, the token is empty and `stale` is set.
        session_store.clear_session(path)
        session_store.save_session(dead, path=path)
        token, source, stale = session_store.resolve_token(path=path)
        assert token == "" and source == "" and stale == dead


def main():
    tests = [
        test_extract_token,
        test_decode_claims,
        test_expiry_maths,
        test_save_load_clear,
        test_corrupt_session_is_ignored,
        test_resolve_priority,
    ]
    for test in tests:
        test()
        print(f"  PASS  {test.__name__}")
    print(f"\n{len(tests)} session-store tests passed")


if __name__ == "__main__":
    main()
