"""Bounded SerpApi Google Search adapter with lazy AI Overview resolution.

Mirrors the request behavior of the sibling ``serp_api_overviews.py`` runner:
one ``engine=google`` first-page request, plus one ``engine=google_ai_overview``
follow-up only when the response carries an empty overview behind a
``page_token``. No pagination, destination fetches, or automatic retries.

Only the standard library is used, and the endpoint is fixed to SerpApi's
``search.json``. Every value that leaves this module passes through
:func:`sanitize` so credentials and short-lived tokens cannot leak into
captures or logs.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
USER_AGENT = "Mozilla/5.0 (compatible; google-aio-comparison/1.0)"
DEFAULT_TIMEOUT = 30.0

# Key names whose values are always replaced, regardless of where they appear.
_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "page_token", "access_token", "refresh_token",
    "token", "secret", "password", "client_secret",
})
_REDACTED = "[REDACTED]"

# URL/form credential parameters, including a parameter at the start of a string.
_CREDENTIAL_PARAM_RE = re.compile(
    r"(?i)((?:^|[?&])(?:api_key|apikey|page_token|access_token|refresh_token"
    r"|token|secret|password|client_secret)=)([^&\s\"']+)"
)
_URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^/\s@]+@")


class SerpApiRequestError(RuntimeError):
    """A SerpApi request failed, returned a non-object, or reported an error."""


def _redact_string(text: str, secrets: tuple[str, ...]) -> str:
    variants = {variant for secret in secrets if secret for variant in
                (secret, urllib.parse.quote(secret, safe=''), urllib.parse.quote_plus(secret))}
    for secret in sorted(variants, key=len, reverse=True):
        text = text.replace(secret, _REDACTED)
    text = _CREDENTIAL_PARAM_RE.sub(lambda match: match.group(1) + _REDACTED, text)
    return _URL_USERINFO_RE.sub(lambda match: match.group(1) + _REDACTED + "@", text)


def _sanitize(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
                cleaned[key] = _REDACTED
            else:
                cleaned[_redact_string(key, secrets)] = _sanitize(item, secrets)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, secrets) for item in value]
    if isinstance(value, str):
        return _redact_string(value, secrets)
    return value


def sanitize(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Return a JSON-safe copy with credential keys/values replaced.

    Sensitive dict keys are replaced wholesale, credential-bearing URL or form
    parameters are redacted, HTTP userinfo is stripped, and any supplied
    ``secrets`` substrings are replaced anywhere they appear.
    """
    secret_values = tuple(s for s in secrets if isinstance(s, str) and s) + tuple(_embedded_secrets(value))
    return _sanitize(value, secret_values)


def _embedded_secrets(item):
    if isinstance(item, dict):
        for key, child in item.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS and isinstance(child, str) and child:
                yield child
            yield from _embedded_secrets(child)
    elif isinstance(item, (list, tuple)):
        for child in item:
            yield from _embedded_secrets(child)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_json(params: dict[str, Any], *, timeout: float) -> Any:
    """Execute one fixed-endpoint SerpApi request and decode its JSON body."""
    url = f"{SERPAPI_ENDPOINT}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            payload = None
        finally:
            exc.close()
        if isinstance(payload, dict) and payload.get('error'):
            return payload
        raise SerpApiRequestError(f"SerpApi HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SerpApiRequestError(f"SerpApi request failed: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise SerpApiRequestError(f"SerpApi request failed: {type(exc).__name__}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SerpApiRequestError("SerpApi returned invalid JSON") from exc


def _validate(query: str, api_key: str, hl: str, gl: str, timeout: float) -> None:
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("api_key must be a non-empty string")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not isinstance(hl, str) or not hl.strip():
        raise ValueError("hl must be a non-empty string")
    if not isinstance(gl, str) or not gl.strip():
        raise ValueError("gl must be a non-empty string")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be a positive finite number")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")


def _invalid_shape(response: Any) -> bool:
    if not isinstance(response, dict) or not response:
        return True
    if 'search_parameters' in response and not isinstance(response['search_parameters'], dict):
        return True
    if 'organic_results' in response and not isinstance(response['organic_results'], list):
        return True
    overview = response.get('ai_overview')
    if overview is not None and not isinstance(overview, dict):
        return True
    if isinstance(overview, dict):
        for field in ('text_blocks', 'references'):
            if field in overview and not isinstance(overview[field], list):
                return True
    return False


def _lazy_overview(overview: Any) -> dict[str, Any] | None:
    """Return the overview dict when it is a lazy-loading container."""
    if (
        isinstance(overview, dict)
        and overview.get("page_token")
        and not overview.get("text_blocks")
    ):
        return overview
    return None


def fetch_search(
    query: str,
    *,
    api_key: str,
    hl: str = "en",
    gl: str = "us",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run one first-page Google Search request and resolve a lazy AI Overview.

    Returns a dict with ``status`` (``ok``/``error``/``unresolved``),
    ``response`` (the effective native response, or ``None`` on failure),
    ``evidence`` (initial overview and follow-up response for lazy requests),
    and ``error`` (a sanitized message for error/unresolved statuses).
    """
    _validate(query, api_key, hl, gl, timeout)
    secrets = (api_key,)

    initial_params: dict[str, Any] = {
        "engine": "google",
        "q": query,
        "hl": hl,
        "gl": gl,
        "api_key": api_key,
        # Prevent stale results, matching the reference runner.
        "no_cache": True,
    }
    try:
        initial = _request_json(initial_params, timeout=timeout)
    except SerpApiRequestError as exc:
        return {
            "status": "error",
            "response": None,
            "evidence": {},
            "error": _redact_string(str(exc), secrets),
        }
    if _invalid_shape(initial):
        return {
            "status": "error",
            "response": None,
            "evidence": sanitize({'initial_response': initial}, secrets),
            "error": "SerpApi returned an invalid response shape",
        }
    secrets += tuple(_embedded_secrets(initial))
    if initial.get("error"):
        return {
            "status": "error",
            "response": None,
            "evidence": sanitize({'initial_response': initial}, secrets),
            "error": _redact_string(str(initial["error"]), secrets),
        }

    overview = initial.get("ai_overview")
    evidence: dict[str, Any] = {}
    if overview is not None:
        evidence["initial_overview"] = overview

    lazy = _lazy_overview(overview)
    if lazy is None:
        return {
            "status": "ok",
            "response": _sanitize(initial, secrets),
            "evidence": _sanitize(evidence, secrets),
            "error": None,
        }

    follow_up_params: dict[str, Any] = {
        "engine": "google_ai_overview",
        "page_token": lazy["page_token"],
        "api_key": api_key,
    }
    secrets += (str(lazy["page_token"]),)
    try:
        follow_up = _request_json(follow_up_params, timeout=timeout)
    except SerpApiRequestError as exc:
        evidence["follow_up"] = None
        evidence["follow_up_error"] = str(exc)
        return {
            "status": "unresolved",
            "response": _sanitize(initial, secrets),
            "evidence": _sanitize(evidence, secrets),
            "error": _redact_string(f"AI Overview follow-up failed: {exc}", secrets),
        }

    secrets += tuple(_embedded_secrets(follow_up))
    resolved = follow_up.get("ai_overview") if isinstance(follow_up, dict) else None
    evidence["follow_up"] = follow_up
    if (_invalid_shape(follow_up) or follow_up.get('error')
            or not isinstance(resolved, dict) or not resolved.get("text_blocks")):
        evidence["follow_up_error"] = "AI Overview follow-up returned no text blocks"
        return {
            "status": "unresolved",
            "response": _sanitize(initial, secrets),
            "evidence": _sanitize(evidence, secrets),
            "error": "AI Overview follow-up returned no text blocks",
        }

    effective = {**initial, "ai_overview": resolved}
    return {
        "status": "ok",
        "response": _sanitize(effective, secrets),
        "evidence": _sanitize(evidence, secrets),
        "error": None,
    }
