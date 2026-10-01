"""Validate, normalize, and atomically upload native scraper snapshots to Supabase.

A snapshot is ``{"run_id", "label", "source_file", "captured_at", "results"}`` where
``results`` is a list of ``{"query_id", "result"}`` entries holding native
``parse_search_html`` payloads. ``normalize_snapshot`` maps that to one row per
storage table. ``upload_snapshot`` runs a single parameterized PostgreSQL
statement through the Management API, so a run is all-or-nothing and a repeated
upload fails on the primary key instead of overwriting earlier runs.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

_MANAGEMENT_HOST = "https://api.supabase.com"
_PROJECT_REF = re.compile(r"[a-z0-9]{20}\Z")
_RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_BLOCK_TYPES = ("heading", "paragraph", "list")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+|(?<![\w.])\/[\w.~%/-]+)\?[^\s]*")
_SECRET_PATTERNS = (
    re.compile(r"sb_secret_[A-Za-z0-9_-]+"),
    re.compile(r"sbp_[A-Za-z0-9_-]+"),
    re.compile(r"GOOGLE_ABUSE[A-Za-z0-9_%:=.-]*"),
)
_COUNT_KEYS = ("queries", "ai_overviews", "ai_references", "organic_results")

UPLOAD_SQL = """
WITH run_input AS (
    SELECT * FROM jsonb_to_recordset($1::jsonb -> 'scrape_runs')
        AS t(run_id uuid, label text, source_file text, captured_at timestamptz, metadata jsonb)
),
inserted_run AS (
    INSERT INTO public.scrape_runs (run_id, label, source_file, captured_at, metadata)
    SELECT run_id, label, source_file, captured_at, metadata FROM run_input
    RETURNING run_id
),
query_input AS (
    SELECT * FROM jsonb_to_recordset($1::jsonb -> 'queries')
        AS t(query_id text, ordinal integer, query text, hl text, gl text,
             status text, error_code text, error_message text)
),
inserted_queries AS (
    INSERT INTO public.queries
        (run_id, query_id, ordinal, query, hl, gl, status, error_code, error_message)
    SELECT r.run_id, q.query_id, q.ordinal, q.query, q.hl, q.gl,
           q.status, q.error_code, q.error_message
    FROM query_input q CROSS JOIN inserted_run r
    RETURNING run_id, query_id
),
overview_input AS (
    SELECT * FROM jsonb_to_recordset($1::jsonb -> 'ai_overviews')
        AS t(query_id text, paragraph text, text_blocks jsonb)
),
inserted_overviews AS (
    INSERT INTO public.ai_overviews (run_id, query_id, paragraph, text_blocks)
    SELECT q.run_id, o.query_id, o.paragraph, o.text_blocks
    FROM overview_input o
    JOIN inserted_queries q ON q.query_id = o.query_id
    RETURNING run_id, query_id
),
reference_input AS (
    SELECT * FROM jsonb_to_recordset($1::jsonb -> 'ai_references')
        AS t(query_id text, ordinal integer, title text, source text, link text)
),
inserted_references AS (
    INSERT INTO public.ai_references (run_id, query_id, ordinal, title, source, link)
    SELECT o.run_id, r.query_id, r.ordinal, r.title, r.source, r.link
    FROM reference_input r
    JOIN inserted_overviews o ON o.query_id = r.query_id
    RETURNING 1 AS inserted
),
organic_input AS (
    SELECT * FROM jsonb_to_recordset($1::jsonb -> 'organic_results')
        AS t(query_id text, ordinal integer, title text, snippet text, link text)
),
inserted_organic AS (
    INSERT INTO public.organic_results (run_id, query_id, ordinal, title, snippet, link)
    SELECT q.run_id, g.query_id, g.ordinal, g.title, g.snippet, g.link
    FROM organic_input g
    JOIN inserted_queries q ON q.query_id = g.query_id
    RETURNING 1 AS inserted
)
SELECT
    (SELECT run_id FROM inserted_run) AS run_id,
    (SELECT count(*) FROM inserted_queries) AS queries,
    (SELECT count(*) FROM inserted_overviews) AS ai_overviews,
    (SELECT count(*) FROM inserted_references) AS ai_references,
    (SELECT count(*) FROM inserted_organic) AS organic_results
""".strip()


class SnapshotError(ValueError):
    """The snapshot cannot be validated or normalized."""


class ConfigurationError(ValueError):
    """Deployment configuration is unusable; nothing was sent."""


class UploadError(RuntimeError):
    """The Management API rejected the request or returned an unusable result."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the Authorization header cannot travel elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request, timeout):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def sanitize_error_message(message, secrets=()):
    """Strip URL query strings and secret-like tokens from an error message."""
    text = "" if message is None else str(message)
    for secret in secrets or ():
        if secret:
            text = text.replace(str(secret), "<redacted>")
    text = _URL_QUERY.sub(r"\1?<redacted>", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


def _dump(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise SnapshotError("snapshot contains values that are not finite JSON") from None


def _required_text(value, where, allow_empty=False):
    if not isinstance(value, str):
        raise SnapshotError(f"{where} must be a string")
    if not allow_empty and not value.strip():
        raise SnapshotError(f"{where} must not be empty")
    return value


def _http_url(value, where):
    url = _required_text(value, where)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SnapshotError(f"{where} must be an absolute http(s) URL")
    return url


def _integer(value, where):
    if not isinstance(value, int) or isinstance(value, bool):
        raise SnapshotError(f"{where} must be an integer")
    return value


def _validate_indexes(indexes, reference_count, where):
    if indexes is None:
        return
    if not isinstance(indexes, list):
        raise SnapshotError(f"{where} reference_indexes must be a list")
    for index in indexes:
        index = _integer(index, f"{where} reference index")
        if index < 0 or index >= reference_count:
            raise SnapshotError(f"{where} citation index {index} is out of range")


def _validate_blocks(blocks, reference_count):
    if not isinstance(blocks, list) or not blocks:
        raise SnapshotError("ai_overview text_blocks must be a non-empty list")
    out = []
    for block in blocks:
        if not isinstance(block, dict):
            raise SnapshotError("each AIO text block must be an object")
        kind = block.get("type")
        if kind not in _BLOCK_TYPES:
            raise SnapshotError(f"unsupported AIO block type {kind!r}")
        block = copy.deepcopy(block)
        if kind == "list":
            items = block.get("list")
            if not isinstance(items, list) or not items:
                raise SnapshotError("AIO list block must have at least one item")
            for item in items:
                if not isinstance(item, dict):
                    raise SnapshotError("AIO list items must be objects")
                _required_text(item.get("snippet"), "AIO list item snippet")
                _validate_indexes(item.get("reference_indexes"), reference_count, "AIO list item")
        else:
            _required_text(block.get("snippet"), "AIO block snippet")
            _validate_indexes(block.get("reference_indexes"), reference_count, "AIO block")
        out.append(block)
    return out


def _validate_references(query_id, references):
    if not isinstance(references, list):
        raise SnapshotError("ai_overview references must be a list")
    rows = []
    for position, reference in enumerate(references):
        if not isinstance(reference, dict):
            raise SnapshotError("each AIO reference must be an object")
        index = reference.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index != position:
            raise SnapshotError("AIO reference indexes must be consecutive from zero")
        rows.append({
            "query_id": query_id,
            "ordinal": position + 1,
            "title": _required_text(reference.get("title", ""), "reference title", allow_empty=True),
            "source": _required_text(reference.get("source", ""), "reference source", allow_empty=True),
            "link": _http_url(reference.get("link"), "reference link"),
        })
    return rows


def _validate_organic(query_id, organic_results):
    if not isinstance(organic_results, list):
        raise SnapshotError("organic_results must be a list")
    rows = []
    for position, item in enumerate(organic_results, start=1):
        if not isinstance(item, dict):
            raise SnapshotError("each organic result must be an object")
        if _integer(item.get("position"), "organic position") != position:
            raise SnapshotError("organic positions must be consecutive from one")
        rows.append({
            "query_id": query_id,
            "ordinal": position,
            "title": _required_text(item.get("title", ""), "organic title", allow_empty=True),
            "snippet": _required_text(item.get("snippet", ""), "organic snippet", allow_empty=True),
            "link": _http_url(item.get("link"), "organic link"),
        })
    return rows


def _normalize_result(ordinal, query_id, result):
    if not isinstance(result, dict):
        raise SnapshotError(f"result {ordinal} payload must be an object")
    params = result.get("search_parameters")
    if not isinstance(params, dict):
        raise SnapshotError(f"result {ordinal} has no search_parameters")
    query = _required_text(params.get("q"), f"result {ordinal} query")
    hl = _required_text(params.get("hl", "en"), "hl", allow_empty=True) or "en"
    gl = _required_text(params.get("gl", "us"), "gl", allow_empty=True) or "us"

    error = result.get("error")
    if error is not None:
        if not isinstance(error, dict):
            raise SnapshotError(f"result {ordinal} error must be an object")
        return ({
            "query_id": query_id, "ordinal": ordinal, "query": query, "hl": hl, "gl": gl,
            "status": "error",
            "error_code": _required_text(error.get("code"), "error code"),
            "error_message": sanitize_error_message(error.get("message", "")),
        }, None, [], [])

    overview_row = None
    reference_rows = []
    overview = result.get("ai_overview")
    if overview is not None:
        if not isinstance(overview, dict):
            raise SnapshotError(f"result {ordinal} ai_overview must be an object")
        reference_rows = _validate_references(query_id, overview.get("references", []))
        blocks = _validate_blocks(overview.get("text_blocks", []), len(reference_rows))
        paragraph = " ".join(block["snippet"] for block in blocks if block["type"] == "paragraph")
        overview_row = {"query_id": query_id, "paragraph": paragraph, "text_blocks": blocks}

    query_row = {"query_id": query_id, "ordinal": ordinal, "query": query, "hl": hl, "gl": gl,
                 "status": "ok", "error_code": None, "error_message": None}
    return (query_row, overview_row, reference_rows,
            _validate_organic(query_id, result.get("organic_results", [])))


def normalize_snapshot(snapshot):
    """Return validated per-table rows for a native scraper snapshot."""
    if not isinstance(snapshot, dict):
        raise SnapshotError("snapshot must be an object")
    run_id = _required_text(snapshot.get("run_id"), "run_id")
    if not _RUN_ID.fullmatch(run_id):
        raise SnapshotError("run_id must be a lowercase UUID")
    label = _required_text(snapshot.get("label", ""), "label", allow_empty=True)
    source_file = os.path.basename(_required_text(snapshot.get("source_file", ""), "source_file", allow_empty=True))
    captured_at = _required_text(snapshot.get("captured_at"), "captured_at")
    try:
        datetime.datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        raise SnapshotError("captured_at must be an ISO 8601 timestamp") from None

    results = snapshot.get("results")
    if not isinstance(results, list) or not results:
        raise SnapshotError("snapshot must contain at least one result")
    planned = snapshot.get("planned_query_count", len(results))
    if isinstance(planned, bool) or not isinstance(planned, int) or planned != len(results):
        raise SnapshotError("snapshot is incomplete: result count differs from planned_query_count")

    queries, overviews, references, organic = [], [], [], []
    seen_ids = set()
    for ordinal, entry in enumerate(results, start=1):
        if not isinstance(entry, dict):
            raise SnapshotError(f"result {ordinal} entry must be an object")
        query_id = _required_text(entry.get("query_id"), f"result {ordinal} query_id")
        if query_id in seen_ids:
            raise SnapshotError(f"duplicate query_id {query_id!r}")
        seen_ids.add(query_id)
        query_row, overview_row, reference_rows, organic_rows = _normalize_result(
            ordinal, query_id, entry.get("result"))
        queries.append(query_row)
        if overview_row is not None:
            overviews.append(overview_row)
        references.extend(reference_rows)
        organic.extend(organic_rows)

    tables = {"queries": queries, "ai_overviews": overviews,
              "ai_references": references, "organic_results": organic}
    metadata = {key: len(value) for key, value in tables.items()}
    metadata["fingerprint"] = hashlib.sha256(_dump(tables).encode("utf-8")).hexdigest()
    normalized = {
        "scrape_runs": [{"run_id": run_id, "label": label, "source_file": source_file,
                         "captured_at": captured_at, "metadata": metadata}],
        **tables,
    }
    _dump(normalized)  # reject non-finite values anywhere in the payload
    return normalized


def _coerce_count(value):
    if isinstance(value, bool):
        raise UploadError("Supabase Management API returned a non-numeric count")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise UploadError("Supabase Management API returned a non-numeric count")


def _validate_response(rows, expected_run_id, metadata):
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise UploadError("Supabase Management API returned an unexpected result shape")
    row = rows[0]
    if str(row.get("run_id", "")).lower() != expected_run_id:
        raise UploadError("Supabase Management API returned a different run_id")
    for key in _COUNT_KEYS:
        if _coerce_count(row.get(key)) != metadata[key]:
            raise UploadError(f"Supabase Management API returned an unexpected {key} count")
    return {"run_id": expected_run_id, **{key: metadata[key] for key in _COUNT_KEYS}}


def upload_snapshot(snapshot, project_id, access_token, timeout=90):
    """Validate a snapshot and insert it as one atomic run through Supabase."""
    if not isinstance(project_id, str) or not _PROJECT_REF.fullmatch(project_id):
        raise ConfigurationError("SUPABASE_PROJECT_ID must be a 20-character project reference")
    if not isinstance(access_token, str) or not access_token.strip():
        raise ConfigurationError("SUPABASE_ACCESS_TOKEN must be a non-empty personal access token")

    normalized = normalize_snapshot(snapshot)
    run_row = normalized["scrape_runs"][0]
    body = json.dumps({"query": UPLOAD_SQL, "parameters": [_dump(normalized)], "read_only": False}).encode("utf-8")
    request = urllib.request.Request(
        f"{_MANAGEMENT_HOST}/v1/projects/{project_id}/database/query",
        data=body,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _open(request, timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise UploadError(f"Supabase Management API returned HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise UploadError(f"Supabase Management API request failed ({type(exc).__name__})") from None
    try:
        rows = json.loads(raw)
    except ValueError:
        raise UploadError("Supabase Management API returned an unreadable response") from None
    return _validate_response(rows, run_row["run_id"], run_row["metadata"])


__all__ = ["SnapshotError", "ConfigurationError", "UploadError", "UPLOAD_SQL",
           "sanitize_error_message", "normalize_snapshot", "upload_snapshot"]
