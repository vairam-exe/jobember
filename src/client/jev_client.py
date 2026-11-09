"""
jev_client.py

Thin HTTP client for TypeSafe AI's System One endpoint (``/v1/systemone``),
which serves **Jev** — the first System One model.

Jev is not a chat model. You send one ``state`` (the material to judge) plus a
set of typed ``questions``, and it returns one typed answer per question, each
with calibrated probabilities and (for Score/Choice) a confidence. There is no
free text to parse, so nothing here can hallucinate a malformed payload.

Why raw ``requests`` instead of the official ``typesafe-sdk``?
    typesafe-sdk 0.7.x depends on ``httpx2>=2.0.0``, ``pydantic>=2.12``,
    ``anyio``, ``tenacity`` and ``truststore``. This repo already pins
    ``curl_cffi==0.15.0`` and ``httpcloak==1.6.1`` for Naukri's TLS
    fingerprinting, and installing a second HTTP stack next to those risks
    resolver conflicts. The wire contract is small and stable (see
    https://api.typesafe.ai/openapi.json), so we speak it directly with
    ``requests`` — the same approach ``jop_classifier.py`` already uses.

Endpoints (both verified live):
    GET  https://api.typesafe.ai/v1/models      -> available model names
    POST https://api.typesafe.ai/v1/systemone   -> answers for the questions

Auth:
    Authorization: Bearer <API_KEY>

Cost (as published by TypeSafe):
    $0.042 / million input tokens, output tokens are free. A resume-shaped
    state plus a job-shaped state is roughly 1.4k input tokens per call, so
    scoring a few hundred jobs costs well under a cent.
"""

import time

import requests


MODELS_URL = "https://api.typesafe.ai/v1/models"
SYSTEMONE_URL = "https://api.typesafe.ai/v1/systemone"

# Statuses worth retrying: rate limits, transient upstream failures, and the
# usual gateway/connection hiccups. Anything else (400/401/422) is a bug in the
# request and retrying will never help.
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class JevError(Exception):
    """Raised when Jev cannot be reached or returns an unusable response."""

    def __init__(self, message, status_code=None, body=None):
        detail = f"[JEV ERROR] {message}"
        if status_code:
            detail += f" | HTTP {status_code}"
        if body:
            detail += f" | {str(body)[:300]}"
        super().__init__(detail)
        self.status_code = status_code
        self.body = body


class JevClient:
    """
    Minimal, retrying client for the TypeSafe System One API.

    Usage:
        jev = JevClient(api_key)
        jev.list_models()                           # fail fast on a bad key
        answers = jev.ask(state, questions)         # {"fit": {...}, ...}
    """

    def __init__(
        self,
        api_key,
        model="jev-latest",
        timeout=45,
        max_retries=3,
        base_url=SYSTEMONE_URL,
        models_url=MODELS_URL,
        backoff=1.0,
    ):
        if not api_key:
            raise JevError("No Jev API key supplied (set JEV_API_KEY in .env)")

        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.url = base_url
        self.models_url = models_url
        self.backoff = backoff

        # Lightweight usage accounting so the agent can report spend per run.
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.last_model = None

        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Headers
    # ------------------------------------------------------------------

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Discovery / key validation
    # ------------------------------------------------------------------

    def list_models(self):
        """
        Return the model names available to this API key.

        Also serves as the cheapest possible key check, so the agent can fail
        fast before logging into Naukri or spending any application quota.
        """
        try:
            res = self._session.get(
                self.models_url,
                headers=self._headers(),
                timeout=min(self.timeout, 20),
            )
        except requests.RequestException as exc:
            raise JevError(f"Could not reach TypeSafe ({exc})")

        if res.status_code in (401, 403):
            raise JevError("API key rejected by TypeSafe", res.status_code, res.text)
        if not res.ok:
            raise JevError("Model listing failed", res.status_code, res.text)

        try:
            payload = res.json()
        except ValueError:
            raise JevError("Model listing returned non-JSON", res.status_code, res.text)

        return [m.get("name") for m in payload.get("models", []) if m.get("name")]

    def verify(self):
        """
        Confirm the key works and that ``self.model`` is actually offered.

        Returns the list of model names. Raises JevError when the configured
        model is unknown, so a typo surfaces immediately instead of after a
        few hundred failed scoring calls.
        """
        models = self.list_models()
        if models and self.model not in models:
            raise JevError(
                f"Model {self.model!r} is not available; offered: {', '.join(models)}"
            )
        return models

    # ------------------------------------------------------------------
    # Core call
    # ------------------------------------------------------------------

    def ask_full(self, state, questions, model=None):
        """
        Send one state plus questions and return the full response body:

            {
              "model":   "jev-1.14.0",
              "answers": {<question id>: {...}},
              "usage":   {"input_tokens": 428, "output_tokens": 73}
            }

        ``state`` may be a string, dict or list (all three are supported by the
        API). ``questions`` is a dict of question-id -> question object.
        """
        if not questions:
            raise JevError("At least one question is required")
        if state is None or (isinstance(state, (str, list, dict)) and not state):
            raise JevError("State is empty")

        body = {
            "model": model or self.model,
            "state": state,
            "questions": questions,
        }

        last_error = None

        for attempt in range(self.max_retries + 1):
            res = None
            try:
                res = self._session.post(
                    self.url,
                    headers=self._headers(),
                    json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = JevError(f"Request failed ({exc})")

            if res is not None:
                if res.ok:
                    return self._consume(res)

                # Auth and validation problems are not worth retrying.
                if res.status_code in (401, 403):
                    raise JevError(
                        "API key rejected by TypeSafe", res.status_code, res.text
                    )
                if res.status_code in (400, 422):
                    raise JevError(
                        "Request rejected by TypeSafe", res.status_code, res.text
                    )

                last_error = JevError(
                    "System One call failed", res.status_code, res.text
                )
                if res.status_code not in RETRYABLE_STATUS:
                    raise last_error

            # Exponential backoff between attempts: 1s, 2s, 4s ...
            if attempt < self.max_retries:
                time.sleep(self.backoff * (2 ** attempt))

        raise last_error or JevError("System One call failed after retries")

    def ask(self, state, questions, model=None):
        """Convenience wrapper returning only the ``answers`` map."""
        return self.ask_full(state, questions, model=model).get("answers", {})

    def _consume(self, res):
        """Parse a successful response, update usage counters, return the body."""
        try:
            payload = res.json()
        except ValueError:
            raise JevError("System One returned non-JSON", res.status_code, res.text)

        answers = payload.get("answers")
        if not isinstance(answers, dict) or not answers:
            raise JevError("System One returned no answers", res.status_code, res.text)

        usage = payload.get("usage") or {}
        self.calls += 1
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        self.last_model = payload.get("model")

        return payload

    # ------------------------------------------------------------------
    # Usage reporting
    # ------------------------------------------------------------------

    @property
    def estimated_cost_usd(self):
        """Input tokens are billed; output tokens are currently free."""
        return self.input_tokens * 0.042 / 1_000_000

    def usage_summary(self):
        return (
            f"{self.calls} calls | {self.input_tokens:,} in / "
            f"{self.output_tokens:,} out tokens | "
            f"~${self.estimated_cost_usd:.4f}"
        )
