"""Normalize saved custom/SerpAPI AIO and organic responses into JSON-only exports."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from serpapi_client import sanitize

MEDIA_KEYS = {"thumbnail", "favicon", "source_icon", "image", "images", "video", "video_link", "duration", "key_moments", "image_url", "thumbnail_url", "icon"}
DROP_SECTIONS = {"ads", "related_questions", "related_searches", "inline_videos", "videos", "discussions_and_forums", "top_stories", "local_results", "knowledge_graph", "search_metadata", "search_parameters", "search_information", "pagination", "serpapi_pagination", "page_token", "api_key"}
AIO_TEXT_KEYS = {"snippet", "text", "table", "formatted", "detailed", "list", "type", "reference_indexes", "snippet_links"}
ORGANIC_KEYS = {"position", "title", "link", "displayed_link", "snippet", "source", "date", "author", "sitelinks", "rich_snippet", "snippet_highlighted_words", "redirect_link"}
VALID_AIO_STATUSES = {"present", "empty", "absent", "unavailable", "unresolved", "error", "invalid"}


def _scoped(value: Any) -> Any:
    """Recursively retain content while excluding known media/ancillary keys."""
    if isinstance(value, dict):
        return {k: _scoped(v) for k, v in value.items()
                if isinstance(k, str) and k.lower() not in MEDIA_KEYS | DROP_SECTIONS}
    if isinstance(value, list):
        return [_scoped(item) for item in value]
    return value


def _unwrap_overview(value: Any) -> Any:
    while isinstance(value, dict) and isinstance(value.get("ai_overview"), dict):
        value = value["ai_overview"]
    return value


def _shape_warnings(response: dict) -> list[str]:
    warnings = []
    for key in ("google", "collection", "extraction_metadata", "search_parameters", "search_metadata",
                "search_information", "organic_results", "google_extra_pages"):
        expected = list if key in ("organic_results", "google_extra_pages") else dict
        if key in response and not isinstance(response[key], expected):
            warnings.append(f"invalid_{key}_type")
    if isinstance(response.get("google"), dict):
        warnings.extend(_shape_warnings(response["google"]))
    params = response.get("search_parameters")
    if isinstance(params, dict):
        for key in ("q", "hl", "gl"):
            if key in params and not isinstance(params[key], str):
                warnings.append(f"invalid_search_parameter:{key}")
    overview = _unwrap_overview(response.get("ai_overview"))
    if overview is not None and not isinstance(overview, dict):
        warnings.append("invalid_ai_overview_type")
    if isinstance(overview, dict):
        for key in ("text_blocks", "references"):
            if key in overview and not isinstance(overview[key], list):
                warnings.append(f"invalid_aio_{key}_type")
            elif any(not isinstance(item, dict) for item in overview.get(key, [])):
                warnings.append(f"invalid_aio_{key}_item")
    organic = response.get("organic_results")
    if isinstance(organic, list) and any(not isinstance(item, dict) for item in organic):
        warnings.append("invalid_organic_result_item")
    return warnings


def _overview_status(response: dict, overview: Any, warnings: list[str]) -> str:
    if response.get("error") or (isinstance(response.get("google"), dict) and response["google"].get("error")):
        return "error"
    if any(item.startswith("invalid_") for item in warnings):
        return "invalid"
    custom = response.get("extraction_metadata")
    if isinstance(custom, dict) and custom.get("aio_status") in VALID_AIO_STATUSES:
        return custom["aio_status"]
    collection = response.get("collection")
    if isinstance(collection, dict) and collection.get("overview_status") in ("unresolved", "resolved_lazy"):
        return "unresolved" if collection["overview_status"] == "unresolved" else "present"
    if overview is None:
        return "absent"
    if not isinstance(overview, dict):
        return "invalid"
    if overview.get("text_blocks"):
        return "present"
    if overview.get("page_token"):
        return "unresolved"
    return "empty"


def _opaque_google_redirect(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    if (parsed.hostname or "").lower() not in {"google.com", "www.google.com"} or parsed.path not in {"/url", "/goto"}:
        return False
    query = parse_qs(parsed.query)
    target = (query.get("url") or query.get("q") or [""])[0]
    decoded = unquote(target)
    return not (urlparse(decoded).scheme in {"http", "https"} and urlparse(decoded).hostname)


def normalize_response(response: dict, *, provider: str, query_id: str = "P01", snapshot_id: str,
                       source_file: str | None = None, captured_at: str | None = None,
                       settings: dict | None = None) -> dict:
    """Convert a native response or saved snapshot to the shared comparison record."""
    if not isinstance(response, dict):
        raise ValueError("response must be a JSON object")
    if provider not in {"custom", "serpapi"} or not isinstance(query_id, str) or not query_id.strip() or not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ValueError("provider, query_id, and snapshot_id must be non-empty valid identifiers")
    # Discover and redact credentials before dropping the fields containing them.
    response = sanitize(response)
    google = response.get("google") if isinstance(response.get("google"), dict) else response
    collection = response.get("collection") if isinstance(response.get("collection"), dict) else {}
    warnings = _shape_warnings(response)
    params = google.get("search_parameters") if isinstance(google.get("search_parameters"), dict) else {}
    metadata = google.get("search_metadata") if isinstance(google.get("search_metadata"), dict) else {}
    supplied = response.get("ai_overview", google.get("ai_overview"))
    overview = _unwrap_overview(supplied)
    status = _overview_status(response, overview, warnings)
    if isinstance(overview, dict) and isinstance(overview.get("text_blocks"), list):
        blocks = []
        for block in overview["text_blocks"]:
            if not isinstance(block, dict):
                warnings.append("invalid_aio_block_item")
                continue
            unknown = set(block) - AIO_TEXT_KEYS
            if unknown:
                warnings.append("unknown_aio_block_fields:" + ",".join(sorted(unknown)))
            if block.get("type") not in {"paragraph", "heading", "list", "table"}:
                warnings.append(f"unknown_aio_block_type:{block.get('type')}")
            blocks.append(_scoped(block))
    else:
        blocks = []
    refs = []
    if isinstance(overview, dict) and isinstance(overview.get("references"), list):
        refs = [_scoped(ref) for ref in overview["references"] if isinstance(ref, dict)]
    indexes = []
    for ref in refs:
        index = ref.get("index")
        if isinstance(index, int) and not isinstance(index, bool):
            indexes.append(index)
    for index in set(indexes):
        if indexes.count(index) > 1:
            warnings.append(f"duplicate_reference_index:{index}")
    for block in blocks:
        for idx in _all_reference_indexes(block):
            if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
                warnings.append(f"invalid_citation_index:{idx}")
            elif idx not in indexes:
                warnings.append(f"unmapped_citation_index:{idx}")
    if refs and not any(True for _ in _all_reference_indexes(blocks)):
        warnings.append("references_without_citation_mappings")
    if any(_opaque_google_redirect(ref.get("link")) for ref in refs):
        warnings.append("opaque_google_redirect_url")
    organic = google.get("organic_results")
    if isinstance(organic, list):
        for item in organic:
            if isinstance(item, dict):
                missing = [key for key in ("position", "title", "link", "snippet") if key not in item]
                if missing:
                    warnings.append("organic_missing_fields:" + ",".join(missing))
    if not isinstance(organic, list):
        organic = []
    organic_results = []
    for item in organic:
        if not isinstance(item, dict):
            warnings.append("invalid_organic_result_item")
            continue
        scoped = {k: _scoped(v) for k, v in item.items()
                  if k in ORGANIC_KEYS or (isinstance(k, str) and k.lower() not in MEDIA_KEYS | DROP_SECTIONS)}
        organic_results.append(scoped)
        if _opaque_google_redirect(item.get("link")) or _opaque_google_redirect(item.get("redirect_link")):
            warnings.append("opaque_google_redirect_url")
    extra_pages = []
    pages = response.get("google_extra_pages", google.get("google_extra_pages", []))
    for page in pages if isinstance(pages, list) else []:
        if not isinstance(page, dict):
            warnings.append("invalid_extra_page_item")
            continue
        page_params = page.get("search_parameters") if isinstance(page.get("search_parameters"), dict) else {}
        page_meta = page.get("search_metadata") if isinstance(page.get("search_metadata"), dict) else {}
        page_results = page.get("organic_results")
        if not isinstance(page_results, list):
            warnings.append("invalid_extra_page_organic_results")
        extra_pages.append({"search_id": page_meta.get("id"), "start": page_params.get("start"),
                            "organic_count": len(page_results) if isinstance(page_results, list) else None})
    observed_settings = {k: params[k] for k in ("hl", "gl", "device", "google_domain") if k in params}
    supplied_settings = settings if isinstance(settings, dict) else collection.get("settings", {})
    if not isinstance(supplied_settings, dict):
        supplied_settings = {}
        warnings.append("invalid_settings_type")
    for key in observed_settings:
        if key in supplied_settings and supplied_settings[key] != observed_settings[key]:
            warnings.append(f"settings_conflict:{key}")
    provenance = {
        "snapshot_id": snapshot_id, "provider": provider, "query_id": query_id,
        "topic": response.get("topic"), "query_type": response.get("query_type"),
        "run_index": collection.get("run"),
        "query": params.get("q") or response.get("prompt"),
        "captured_at": metadata.get("created_at") or captured_at or collection.get("requested_at"),
        "settings": {**supplied_settings, **observed_settings},
        "source_file": source_file, "search_id": metadata.get("id"),
        "page_start": params.get("start", 0),
    }
    result = {"schema_version": 1, "provenance": provenance, "aio_status": status,
              "ai_overview": {"blocks": blocks, "references": refs} if isinstance(overview, dict) else None,
              "organic_count": len(organic_results), "organic_results": organic_results,
              "warnings": sorted(set(warnings))}
    if response.get("error") or (isinstance(response.get("google"), dict) and response["google"].get("error")):
        result["error"] = _scoped(response.get("error", response["google"].get("error") if isinstance(response.get("google"), dict) else None))
    if extra_pages:
        result["pagination"] = {"merged_pages": True, "extra_pages": extra_pages}
    return sanitize(result)


def _all_reference_indexes(node: Any):
    if isinstance(node, dict):
        indexes = node.get("reference_indexes")
        if isinstance(indexes, list):
            yield from indexes
        for value in node.values():
            yield from _all_reference_indexes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _all_reference_indexes(value)


def export_records(records: list[dict], output_dir: str | Path) -> None:
    """Publish a sanitized records.json atomically, refusing overwrite."""
    cleaned = sanitize(records)
    seen = set()
    for record in cleaned:
        key = (record["provenance"]["provider"], record["provenance"]["snapshot_id"])
        if key in seen:
            raise ValueError(f"duplicate snapshot ID for provider: {key[0]} {key[1]}")
        seen.add(key)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("output directory already exists")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".search-export-", dir=output_dir.parent) as temporary:
        staging = Path(temporary) / "complete"
        staging.mkdir()
        _write_export_files(cleaned, staging)
        # Reserve exclusively, then replace only our empty reservation with the
        # completed directory. Failed writes never publish a partial export.
        output_dir.mkdir(exist_ok=False)
        try:
            os.replace(staging, output_dir)
        except OSError:
            output_dir.rmdir()
            raise


def _write_export_files(cleaned: list[dict], output_dir: Path) -> None:
    (output_dir / "records.json").write_text(json.dumps(cleaned, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _entries(value: Any) -> list[tuple[str | None, dict]]:
    if isinstance(value, list):
        if any(not isinstance(item, dict) for item in value):
            raise ValueError("input list entries must be JSON objects")
        return [(None, item) for item in value]
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object, response batch, or list of snapshots")
    if "results" in value and "run_id" in value:
        if not isinstance(value["results"], list):
            raise ValueError("run results must be an array")
        entries = []
        for entry in value["results"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("result"), dict) or not isinstance(entry.get("query_id"), str):
                raise ValueError("run entries must contain query_id and result")
            item = dict(entry["result"])
            item.setdefault("collection", {"requested_at": value.get("captured_at"), "query_id": entry["query_id"]})
            entries.append((entry["query_id"], item))
        return entries
    if any(key in value for key in ("search_parameters", "search_metadata", "ai_overview", "organic_results", "error", "google", "prompt", "collection", "extraction_metadata")):
        return [(None, value)]
    if not value or any(not isinstance(item, dict) for item in value.values()):
        raise ValueError("batch entries must all be response objects")
    return [(str(key), item) for key, item in value.items()]


def load_input(paths: list[str | Path], *, provider: str) -> list[dict]:
    if provider not in {"custom", "serpapi"}:
        raise ValueError("provider must be custom or serpapi")
    records = []
    query_ids_by_text = {}
    for path_index, path in enumerate(paths, 1):
        path = Path(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        for entry_index, (key, item) in enumerate(_entries(value), 1):
            response = dict(item)
            google = response.get("google") if isinstance(response.get("google"), dict) else response
            collection = response.get("collection") if isinstance(response.get("collection"), dict) else {}
            params = google.get("search_parameters") if isinstance(google.get("search_parameters"), dict) else {}
            query = params.get("q") or response.get("prompt")
            query_id = collection.get("query_id") or key
            repeated_key = re.fullmatch(r"(P\d+)_run(\d+)", key or "")
            if repeated_key and not collection.get("query_id"):
                query_id = repeated_key[1]
                collection = {**collection, "run": int(repeated_key[2])}
                response["collection"] = collection
            if isinstance(query, str):
                if query_id:
                    query_ids_by_text.setdefault(query, query_id)
                else:
                    if query not in query_ids_by_text:
                        candidate = 1
                        while f"P{candidate:02d}" in query_ids_by_text.values():
                            candidate += 1
                        query_ids_by_text[query] = f"P{candidate:02d}"
                    query_id = query_ids_by_text[query]
            query_id = query_id or f"P{entry_index:02d}"
            metadata = google.get("search_metadata") if isinstance(google.get("search_metadata"), dict) else {}
            api_id = collection.get("search_id") if isinstance(collection, dict) else None
            api_id = api_id or metadata.get("id")
            sid = f"{api_id}-input{path_index:02d}-record{entry_index:04d}" if api_id else f"input{path_index:02d}-record{entry_index:04d}"
            records.append(normalize_response(response, provider=provider, query_id=str(query_id), snapshot_id=sid,
                source_file=str(path), captured_at=collection.get("requested_at") if isinstance(collection, dict) else None,
                settings=collection.get("settings") if isinstance(collection, dict) else None))
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False, description=__doc__)
    parser.add_argument("--provider", choices=("custom", "serpapi"), required=True)
    parser.add_argument("--input", nargs="+", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        records = load_input(args.input, provider=args.provider)
        if not records:
            raise ValueError("no response records found")
        export_records(records, args.output_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps(sanitize({"error": str(exc)})))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
