"""Read-only, loopback-only website for the private Supabase response store."""
from __future__ import annotations

import argparse
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid


class ViewerError(Exception):
    """An error safe to show in the browser."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SupabaseReader:
    def __init__(self, project_id, secret_key):
        if not re.fullmatch(r"[a-z0-9]{20}", project_id or "") or not secret_key:
            raise ViewerError("Set SUPABASE_PROJECT_ID and SUPABASE_SECRET_KEY in .env or your environment.")
        self.base_url = f"https://{project_id}.supabase.co/rest/v1/"
        self.secret_key = secret_key

    def get(self, table, params):
        # Both table and column names come only from the routes below.
        request = urllib.request.Request(
            self.base_url + table + "?" + urllib.parse.urlencode(params),
            headers={"apikey": self.secret_key, "Accept": "application/json"},
        )
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=25) as response:
                rows = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ViewerError("Supabase denied access. Check the server's secret key and database grants.") from None
            raise ViewerError("Supabase could not read the response store. Check the schema and Data API configuration.") from None
        except (OSError, ValueError):
            raise ViewerError("Cannot reach Supabase or its response was invalid. Please try again.") from None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ViewerError("Supabase returned an unexpected response.")
        return rows


def integer(params, name, default, maximum):
    try:
        value = int(params.get(name, str(default)))
    except ValueError:
        raise ValueError(f"Invalid {name}.") from None
    if not 0 <= value <= maximum or (name == "limit" and value == 0):
        raise ValueError(f"Invalid {name}.")
    return value


def run_uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A valid run_id is required.") from None


QUERY_SUMMARY = "run_id,query_id,ordinal,query,captured_at,status,capture_status,aio_status,hl,gl,device,google_domain"


def query_id(value):
    if not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("A valid query_id is required.")
    return value


def query_filters(params):
    """Fixed columns/operators only; applied by PostgREST before pagination."""
    filters = {}
    enums = {
        "aio_status": {"present", "absent", "unavailable", "empty", "unresolved", "invalid", "error", "unknown", "not_present"},
        "capture_status": {"ok", "error", "unresolved", "unknown"},
        "has_warnings": {"yes", "no"}, "has_organic": {"yes", "no"},
    }
    for key, allowed in enums.items():
        value = params.get(key, "")
        if not value:
            continue
        if value not in allowed:
            raise ValueError(f"Invalid {key} filter.")
        if key == "aio_status" and value == "not_present":
            filters["or"] = "(aio_status.neq.present,aio_status.is.null)"
        elif key in ("aio_status", "capture_status"):
            filters[key] = "is.null" if value == "unknown" else "eq." + value
        else:
            filters["warnings" if key == "has_warnings" else "organic_results"] = "neq.[]" if value == "yes" else "eq.[]"
    for key in ("hl", "gl", "device"):
        value = params.get(key, "").strip()
        if value:
            if len(value) > 64 or any(ord(c) < 32 for c in value):
                raise ValueError(f"Invalid {key} filter.")
            filters[key] = "eq." + value
    bounds = []
    dates = {}
    for key in ("captured_from", "captured_to"):
        value = params.get(key, "")
        if not value:
            continue
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            dates[key] = dt.date.fromisoformat(value)
            boundary = dates[key] + dt.timedelta(days=1) if key == "captured_to" else dates[key]
        except (ValueError, OverflowError):
            raise ValueError(f"Invalid {key} date (use YYYY-MM-DD).") from None
        bounds.append(f"captured_at.{'lt' if key == 'captured_to' else 'gte'}.{boundary.isoformat()}T00:00:00Z")
    if len(dates) == 2 and dates["captured_from"] > dates["captured_to"]:
        raise ValueError("Capture start date must not be after the end date.")
    if bounds:
        filters["and"] = "(" + ",".join(bounds) + ")"
    return filters


def api_response(reader, path, params):
    """Route only allowlisted SELECTs; never accept SQL or arbitrary columns."""
    if path in ("/api/runs", "/api/queries"):
        limit = integer(params, "limit", 50, 100)
        offset = integer(params, "offset", 0, 10_000_000)
        if path == "/api/runs":
            table = "scrape_runs"
            filters = {"select": "run_id,label,provider,run_index,comparison_id,created_at",
                       "order": "created_at.desc,run_id.asc"}
        else:
            table = "queries"
            filters = {"select": QUERY_SUMMARY,
                       "run_id": "eq." + run_uuid(params.get("run_id")),
                       "order": "ordinal.asc,query_id.asc",
                       **query_filters(params)}
            search = params.get("search", "").strip()
            if len(search) > 300:
                raise ValueError("Search is limited to 300 characters.")
            if search:
                # PostgREST's ilike supports wildcard searches; URL encoding keeps
                # filter values separate from all other request parameters.
                filters["query"] = "ilike.%" + search + "%"
        rows = reader.get(table, {**filters, "limit": limit + 1, "offset": offset})
        return {"items": rows[:limit], "has_more": len(rows) > limit, "offset": offset}
    if path == "/api/comparison-matches":
        source_run = run_uuid(params.get("run_id"))
        target_run = run_uuid(params.get("compare_run_id"))
        source_query = query_id(params.get("query_id", ""))
        limit = integer(params, "limit", 50, 100)
        offset = integer(params, "offset", 0, 10_000_000)
        if source_run == target_run:
            raise ValueError("Choose two different capture runs.")
        source = reader.get("queries", {"select": "query,hl,gl", "run_id": "eq." + source_run,
                                        "query_id": "eq." + source_query, "limit": 1})
        if not source:
            return None
        anchor = source[0]
        filters = {"select": QUERY_SUMMARY, "run_id": "eq." + target_run,
                   "query": "eq." + anchor["query"], "order": "ordinal.asc,query_id.asc",
                   "limit": limit + 1, "offset": offset}
        for key in ("hl", "gl"):
            filters[key] = "is.null" if anchor.get(key) is None else "eq." + anchor[key]
        rows = reader.get("queries", filters)
        return {"items": rows[:limit], "has_more": len(rows) > limit, "offset": offset,
                "match_rule": "Exact query text, language and country; device may differ."}
    if path == "/api/response":
        identifier = query_id(params.get("query_id", ""))
        rows = reader.get("queries", {"select": "*", "run_id": "eq." + run_uuid(params.get("run_id")),
                                      "query_id": "eq." + identifier, "limit": 1})
        return rows[0] if rows else None
    raise KeyError(path)


ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8")}


class ViewerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Avoid recording query text, response data or upstream errors.
        pass

    def reply(self, status, data, content_type="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # Reject DNS rebinding and cross-site browser requests. No CORS access.
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host", "").lower() not in allowed or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.reply(403, {"error": "This viewer is available only from its local address."})
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in ASSETS:
            name, content_type = ASSETS[parsed.path]
            self.reply(200, files("aio_viewer").joinpath("static", name).read_bytes(), content_type)
            return
        try:
            params = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
            result = api_response(self.server.reader, parsed.path, params)
            self.reply(404 if result is None else 200, {"error": "Response not found."} if result is None else result)
        except KeyError:
            self.reply(404, {"error": "Not found."})
        except ValueError as exc:
            self.reply(400, {"error": str(exc)})
        except ViewerError as exc:
            self.reply(502, {"error": str(exc)})
        except Exception:
            self.reply(500, {"error": "The viewer encountered an unexpected error."})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000, help="Local port (default: 8000)")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    from aio_scraper import _load_env_file
    _load_env_file()
    try:
        reader = SupabaseReader(os.environ.get("SUPABASE_PROJECT_ID"), os.environ.get("SUPABASE_SECRET_KEY"))
    except ViewerError as exc:
        parser.error(str(exc))
    with ThreadingHTTPServer(("127.0.0.1", args.port), ViewerHandler) as server:
        server.reader = reader
        print(f"Search response viewer: http://127.0.0.1:{args.port}", flush=True)
        print("Read-only access. Keep the backend port private; press Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
