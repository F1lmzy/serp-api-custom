"""Response storage for native captures and shared custom/SerpAPI records.

Both input paths use the same validation and two-table JSONB projection.
Native captures retain their run UUID and query order; comparison records
are grouped by provider and repeat round. Uploads insert one complete response
per query in one parameterized statement, without overwriting or retrying.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from response_sql import TABLE_COLUMNS, UPLOAD_SQL as COMPARISON_UPLOAD_SQL, RUN_UPLOAD_SQL as UPLOAD_SQL
from search_export import MEDIA_KEYS, DROP_SECTIONS, _all_reference_indexes, normalize_response
from serpapi_client import sanitize

_MANAGEMENT_HOST = "https://api.supabase.com"
_PROJECT_REF = re.compile(r"[a-z0-9]{20}\Z")
_RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+|(?<![\w.])\/[\w.~%/-]+)\?[^\s]*")
_SECRET_PATTERNS = (
    re.compile(r"sb_secret_[A-Za-z0-9_-]+"),
    re.compile(r"sbp_[A-Za-z0-9_-]+"),
    re.compile(r"GOOGLE_ABUSE[A-Za-z0-9_%:=.-]*"),
)
_COUNT_KEYS = ("queries", "ai_overviews", "ai_references", "organic_results")
STATUSES = {'present', 'absent', 'unavailable', 'empty', 'unresolved', 'invalid', 'error'}
BLOCK_KEYS = {'type', 'snippet', 'list', 'items', 'table', 'detailed', 'reference_indexes', 'snippet_links', 'text', 'link', 'colspan', 'rowspan', 'ordered', 'start', 'is_header'}
IGNORED = MEDIA_KEYS | DROP_SECTIONS | {'formatted', 'snippet_highlighted_words'}
SUPPORT_KEYS = {'inline', 'expanded', 'title', 'link', 'snippet', 'top', 'bottom', 'extensions', 'detected_extensions', 'attributes'}



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


def secrets():
    names = ('SERPAPI_API_KEY', 'TWOCAPTCHA_API_KEY', 'DATAIMPULSE_PROXY_USER', 'DATAIMPULSE_PROXY_PASSWORD',
             'DATAIMPULSE_PROXY_URL', 'SUPABASE_SECRET_KEY', 'SUPABASE_ACCESS_TOKEN')
    return tuple(os.environ[k] for k in names if os.environ.get(k))


def _text(value, name, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or (not optional and not value.strip()):
        raise ValueError(f'{name} must be text')
    return value


def _integer(value, name, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')
    return value


def _timestamp(value):
    if value is None:
        return None
    value = _text(value, 'capture time').replace(' UTC', '+00:00').replace('Z', '+00:00')
    stamp = dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('capture time must include its timezone')
    return stamp.astimezone(dt.timezone.utc).isoformat()


def _project(node, warnings, support=False, free_keys=False):
    if isinstance(node, list):
        return [_project(v, warnings, support, free_keys) for v in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if not isinstance(key, str):
            raise ValueError('content field names must be text')
        if key in IGNORED or key.endswith('_serpapi_link'):
            continue
        if not support and key not in BLOCK_KEYS:
            warnings.append(f'unknown_aio_content_field:{key}')
        if support and not free_keys and key not in SUPPORT_KEYS:
            continue
        if key in ('colspan', 'rowspan') and (isinstance(value, bool) or not str(value).isdigit() or int(value) < 1):
            warnings.append('invalid_table_span')
            continue
        if key == 'reference_indexes':
            if not isinstance(value, list):
                raise ValueError('reference_indexes must be an array')
            for index in value:
                _integer(index, 'citation index', 0)
        if key == 'link':
            value = _http_url(value, 'content link')
        out[key] = _project(value, warnings, support, free_keys or key in ('attributes', 'detected_extensions'))
    return out


def render_text(node, depth=0, item=False):
    if isinstance(node, str):
        return ('  ' * depth + '• ' if item else '') + node
    if isinstance(node, list):
        return '\n'.join(filter(None, (render_text(v, depth, item) for v in node)))
    if not isinstance(node, dict):
        return ''
    lines = []
    if node.get('snippet'):
        lines.append(('  ' * depth + '• ' if item else '') + _text(node['snippet'], 'snippet'))
    if isinstance(node.get('table'), list):
        for row in node['table']:
            if not isinstance(row, list):
                raise ValueError('table rows must be arrays')
            lines.append(' | '.join(render_text(cell) for cell in row))
    elif node.get('type') == 'table' and isinstance(node.get('detailed'), list):
        lines.extend(' | '.join(render_text(cell) for cell in row) for row in node['detailed'])
    for key in ('list', 'items'):
        if key in node:
            if not isinstance(node[key], list):
                raise ValueError('list items must be arrays')
            lines.extend(render_text(v, depth + int(item and bool(node.get('snippet'))), True) for v in node[key])
    return '\n'.join(filter(None, lines))


def _normalize_overview(overview, status, warnings):
    """Project ordered answer content without inventing citation mappings."""
    if overview is None:
        if status == 'present':
            raise ValueError('present AIO has no overview')
        return None
    if (not isinstance(overview, dict)
            or not isinstance(overview.get('blocks'), list)
            or not isinstance(overview.get('references'), list)):
        raise ValueError('overview must contain block and reference arrays')
    if status in ('absent', 'unavailable') and overview['blocks']:
        raise ValueError('AIO status contradicts nonempty answer blocks')

    blocks = []
    for block in overview['blocks']:
        if not isinstance(block, dict) or block.get('type') not in ('paragraph', 'heading', 'list', 'table'):
            raise ValueError('unsupported AIO block type')
        projected = _project(block, warnings)
        if block['type'] in ('paragraph', 'heading'):
            _text(projected.get('snippet'), 'AIO block snippet')
        if not render_text(projected):
            raise ValueError('AIO block has no readable content')
        blocks.append(projected)
    if status == 'present' and not blocks:
        raise ValueError('present AIO has no blocks')

    indexes, references = [], []
    for ordinal, reference in enumerate(overview['references'], 1):
        if not isinstance(reference, dict):
            raise ValueError('each reference must be an object')
        index = reference.get('index')
        if index is not None:
            _integer(index, 'reference index', 0)
            indexes.append(index)
        projected = {
            'ordinal': ordinal, 'index': index,
            'link': _http_url(reference.get('link'), 'reference link'),
            **{key: _text(reference.get(key), key, True) for key in ('title', 'source', 'snippet')},
        }
        references.append({key: value for key, value in projected.items() if value is not None})
    for index in _all_reference_indexes(blocks):
        if index not in indexes:
            warnings.append(f'unmapped_citation_index:{index}')
    if len(indexes) != len(set(indexes)):
        warnings.append('duplicate_reference_index')
    return {
        'paragraph': ' '.join(block['snippet'] for block in blocks if block['type'] == 'paragraph'),
        'text': '\n\n'.join(render_text(block) for block in blocks),
        'blocks': blocks, 'references': references,
    }


def _normalize_organic_results(results, count, warnings):
    """Keep source order separate from the provider's returned positions."""
    count = _integer(count, 'organic_count', 0)
    if not isinstance(results, list) or count != len(results):
        raise ValueError('organic_count does not match results')
    rows = []
    for ordinal, result in enumerate(results, 1):
        if not isinstance(result, dict):
            raise ValueError('each organic result must be an object')
        position = result.get('position')
        if position is not None:
            _integer(position, 'organic position')
        row = {
            'ordinal': ordinal, 'position': position,
            'link': _http_url(result.get('link'), 'organic link'),
        }
        for key in ('title', 'snippet', 'displayed_link', 'source', 'date', 'author'):
            row[key] = _text(result.get(key), key, True)
        for key in ('sitelinks', 'rich_snippet'):
            value = result.get(key)
            if value is not None and not isinstance(value, (dict, list)):
                raise ValueError(f'{key} must be structured content')
            row[key] = _project(value, warnings, support=True) if value is not None else None
        rows.append({key: value for key, value in row.items() if value is not None})
    return rows


def _response_row(record, run_id, ordinal):
    """Validate one shared record and build its complete stored response."""
    provenance = record['provenance']
    status = record['aio_status']
    capture_status = record.get('capture_status', 'error' if status == 'error' else 'ok')
    if status not in STATUSES or capture_status not in ('ok', 'error', 'unresolved'):
        raise ValueError('invalid capture/AIO status')
    warnings = record.get('warnings', [])
    if not isinstance(warnings, list) or any(not isinstance(warning, str) for warning in warnings):
        raise ValueError('warnings must be an array of text')
    warnings = list(warnings)
    settings = provenance.get('settings') or {}
    if not isinstance(settings, dict):
        raise ValueError('settings must be an object')
    error = record.get('error')
    error_message = error.get('message') if isinstance(error, dict) else error
    row = {
        'run_id': run_id, 'query_id': provenance['query_id'], 'ordinal': ordinal,
        'query': _text(provenance['query'], 'query'),
        'hl': _text(settings.get('hl'), 'hl', True),
        'gl': _text(settings.get('gl'), 'gl', True),
        'status': 'ok' if capture_status == 'ok' and status in ('present', 'absent', 'unavailable') else 'error',
        'error_code': _text(error.get('code'), 'error code', True) if isinstance(error, dict) else None,
        'error_message': sanitize_error_message(error_message, secrets()) if error_message else None,
        'snapshot_id': provenance['snapshot_id'],
        'captured_at': _timestamp(provenance.get('captured_at')),
        'capture_status': capture_status, 'aio_status': status,
        'device': _text(settings.get('device'), 'device', True),
        'google_domain': _text(settings.get('google_domain'), 'google_domain', True),
        'warnings': warnings, 'response_schema_version': 1,
        'aio_response': None, 'organic_results': [],
    }
    row['aio_response'] = _normalize_overview(record.get('ai_overview'), status, warnings)
    row['organic_results'] = _normalize_organic_results(
        record['organic_results'], record.get('organic_count'), warnings,
    )
    row['warnings'] = sorted(set(warnings))
    return row


def normalize_records(records, comparison_id, label=''):
    """Group shared records into stable provider runs and complete response rows."""
    comparison_uuid = uuid.UUID(comparison_id)
    if str(comparison_uuid) != comparison_id or not isinstance(records, list) or not records:
        raise ValueError('a canonical comparison UUID and nonempty records array are required')
    _dump(records)  # Reject nonfinite/non-JSON input before projection.
    records = sanitize(records, secrets())
    groups, query_texts = {}, {}
    for record in records:
        if not isinstance(record, dict) or type(record.get('schema_version')) is not int or record['schema_version'] != 1:
            raise ValueError('unsupported normalized record schema')
        provenance = record.get('provenance')
        if not isinstance(provenance, dict):
            raise ValueError('provenance must be an object')
        provider = provenance.get('provider')
        if provider not in ('custom', 'serpapi'):
            raise ValueError('unsupported provider')
        run_index = _integer(provenance.get('run_index') if provenance.get('run_index') is not None else 1, 'run_index')
        identity = (run_index, _text(provenance.get('query_id'), 'query_id'))
        query_text = _text(provenance.get('query'), 'query')
        if identity in query_texts and query_texts[identity] != query_text:
            raise ValueError('matching query IDs must have identical query text')
        query_texts[identity] = query_text
        groups.setdefault((provider, run_index), []).append(record)

    tables = {table: [] for table in TABLE_COLUMNS}
    for (provider, run_index), group in sorted(groups.items()):
        run_id = str(uuid.uuid5(comparison_uuid, f'{provider}:{run_index}'))
        query_ids, snapshot_ids, response_rows = set(), set(), []
        group.sort(key=lambda record: record['provenance']['query_id'])
        for ordinal, record in enumerate(group, 1):
            provenance = record['provenance']
            query_id = _text(provenance['query_id'], 'query_id')
            snapshot_id = _text(provenance['snapshot_id'], 'snapshot_id')
            if query_id in query_ids or snapshot_id in snapshot_ids:
                raise ValueError('duplicate query/snapshot identity within a provider round')
            query_ids.add(query_id)
            snapshot_ids.add(snapshot_id)
            response_rows.append(_response_row(record, run_id, ordinal))
        metadata = response_counts(response_rows)
        metadata.update(
            schema_version=3,
            fingerprint=hashlib.sha256(_dump({'queries': response_rows}).encode()).hexdigest(),
        )
        timestamps = [row['captured_at'] for row in response_rows if row['captured_at']]
        sources = {Path(record['provenance'].get('source_file') or '').name for record in group}
        tables['scrape_runs'].append({
            'run_id': run_id, 'comparison_id': comparison_id, 'provider': provider,
            'run_index': run_index, 'label': _text(label, 'label', True) or '',
            'source_file': next(iter(sources)) if len(sources) == 1 else '',
            'captured_at': min(timestamps) if timestamps else None, 'metadata': metadata,
        })
        tables['queries'].extend(response_rows)
    _dump(tables)
    return tables


def normalize_response_snapshot(snapshot):
    """Adapt a complete custom capture to the same one-row response contract."""
    try:
        _dump(snapshot)
        snapshot = sanitize(snapshot, secrets())
        if not isinstance(snapshot, dict):
            raise ValueError('snapshot must be an object')
        run_id = _text(snapshot.get('run_id'), 'run_id')
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError('run_id must be a lowercase UUID')
        captured_at = _timestamp(_text(snapshot.get('captured_at'), 'captured_at'))
        entries = snapshot.get('results')
        if not isinstance(entries, list) or not entries:
            raise ValueError('snapshot must contain at least one result')
        planned = _integer(snapshot.get('planned_query_count', len(entries)), 'planned_query_count')
        if planned != len(entries):
            raise ValueError('snapshot is incomplete: result count differs from planned_query_count')
        records, query_order = [], {}
        for ordinal, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                raise ValueError('snapshot entries must be objects')
            query_id = _text(entry.get('query_id'), 'query_id')
            if query_id in query_order:
                raise ValueError('duplicate query_id')
            query_order[query_id] = ordinal
            record = normalize_response(entry.get('result'), provider='custom', query_id=query_id,
                snapshot_id=f'{run_id}:{query_id}', source_file=snapshot.get('source_file', ''), captured_at=captured_at)
            if any(warning.startswith('invalid_') for warning in record['warnings']):
                raise ValueError('native response contains malformed scoped content')
            record['provenance']['run_index'] = snapshot.get('run_index', 1)
            if record['aio_status'] in ('invalid', 'error'):
                record['capture_status'] = 'error'
            elif record['aio_status'] in ('empty', 'unresolved'):
                record['capture_status'] = 'unresolved'
            else:
                record['capture_status'] = 'ok'
            records.append(record)
        tables = normalize_records(records, run_id, snapshot.get('label', ''))
        run = tables['scrape_runs'][0]
        run.update(run_id=run_id, comparison_id=None, captured_at=captured_at)
        for query in tables['queries']:
            query.update(run_id=run_id, ordinal=query_order[query['query_id']])
        tables['queries'].sort(key=lambda row: row['ordinal'])
        run['metadata']['fingerprint'] = hashlib.sha256(_dump({'queries': tables['queries']}).encode()).hexdigest()
        return tables
    except (ValueError, TypeError, KeyError) as exc:
        raise SnapshotError(str(exc)) from None


def response_counts(queries):
    """Logical content counts, not additional database rows."""
    return {'queries': len(queries),
            'ai_overviews': sum(q['aio_response'] is not None for q in queries),
            'ai_references': sum(len((q['aio_response'] or {}).get('references', [])) for q in queries),
            'organic_results': sum(len(q['organic_results']) for q in queries)}


def _post_sql(body, project_id, access_token, timeout):
    """Send one fixed-host request through the redirect-refusing HTTP adapter.

    Callers retain their native/comparison exception and receipt contracts.
    A transport failure can follow a committed write, so this never retries.
    """
    request = urllib.request.Request(
        f"{_MANAGEMENT_HOST}/v1/projects/{project_id}/database/query",
        data=body,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        method="POST",
    )
    with _open(request, timeout) as response:
        return response.read()


def execute_sql(query, parameters=None, *, project_id, access_token, read_only=False, timeout=90):
    """Execute comparison SQL once, retaining its ValueError error contract."""
    if not isinstance(project_id, str) or not _PROJECT_REF.fullmatch(project_id):
        raise ValueError('invalid SUPABASE_PROJECT_ID')
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError('SUPABASE_ACCESS_TOKEN is required')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('invalid SQL request timeout')
    body = _dump({'query': query, 'parameters': parameters or [], 'read_only': read_only}).encode()
    try:
        result = json.loads(_post_sql(body, project_id, access_token, timeout))
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ValueError(f'Supabase SQL request failed: HTTP {exc.code}; inspect the saved batch before retrying writes') from None
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise ValueError(f'Supabase SQL transport/response failed ({type(exc).__name__}); do not retry an uncertain write automatically') from None
    if not isinstance(result, list):
        raise ValueError('unexpected SQL response')
    return result


def upload_records(records, comparison_id, project_id, access_token, label='', *, executor=None):
    """Normalize and upload shared records, validating the comparison receipt.

    The optional executor is the internal SQL seam used by the comparison
    command adapter and tests. It must perform one request without retries.
    """
    execute = execute_sql if executor is None else executor
    tables = normalize_records(records, comparison_id, label)
    receipt = execute(COMPARISON_UPLOAD_SQL, [_dump(tables)], project_id=project_id, access_token=access_token)
    expected = {'comparison_id': comparison_id, **{t: len(rows) for t, rows in tables.items()}}
    if (receipt != [expected] or not isinstance(receipt[0], dict)
            or any(type(receipt[0].get(t)) is not int for t in TABLE_COLUMNS)):
        raise ValueError('upload receipt does not match expected counts; inspect the batch before retrying')
    return receipt[0]


def sql_export(tables):
    """Portable PostgreSQL seed: hex encoding keeps data out of SQL syntax."""
    payload = _dump(tables).encode().hex()
    expression = f"convert_from(decode('{payload}', 'hex'), 'UTF8')::jsonb"
    return 'BEGIN;\n' + COMPARISON_UPLOAD_SQL.replace('$1::jsonb', expression) + ';\nCOMMIT;\n'


def upload_snapshot(snapshot, project_id, access_token, timeout=90):
    """Validate a snapshot and insert it as one atomic run through Supabase."""
    if not isinstance(project_id, str) or not _PROJECT_REF.fullmatch(project_id):
        raise ConfigurationError("SUPABASE_PROJECT_ID must be a 20-character project reference")
    if not isinstance(access_token, str) or not access_token.strip():
        raise ConfigurationError("SUPABASE_ACCESS_TOKEN must be a non-empty personal access token")

    normalized = normalize_response_snapshot(snapshot)
    run_row = normalized["scrape_runs"][0]
    body = json.dumps({"query": UPLOAD_SQL, "parameters": [_dump(normalized)], "read_only": False}).encode("utf-8")
    try:
        raw = _post_sql(body, project_id, access_token, timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise UploadError(f"Supabase Management API returned HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise UploadError(f"Supabase Management API request failed ({type(exc).__name__})") from None
    try:
        rows = json.loads(raw)
    except ValueError:
        raise UploadError("Supabase Management API returned an unreadable response") from None
    return _validate_response(rows, run_row["run_id"], run_row["metadata"])


__all__ = [
    "SnapshotError", "ConfigurationError", "UploadError", "UPLOAD_SQL",
    "sanitize_error_message", "normalize_response_snapshot", "upload_snapshot",
    "normalize_records", "upload_records", "execute_sql", "sql_export",
    "render_text", "response_counts", "secrets",
]
