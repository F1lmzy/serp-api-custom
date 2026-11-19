"""Resolve opaque Google redirect links by observing Google's redirect responses.

Google's organic /goto and AI-reference /url links carry server-side tokens,
not decodable URLs, and most reference links also carry psig/ust click
signatures issued against the capturing session. Destinations are therefore
observed from Google's own redirect responses inside a live browser session:
one bounded request per unique link through the page's context, redirects
never followed (`max_redirects=0`), no destination page content downloaded,
and nothing resolved offline or guessed.

`resolve_redirect_links` fixes one response in place (the live capture path).
`resolve_capture_links` fixes every response in a saved native capture with a
checkpointed link->destination cache, so interrupted runs resume without
re-observing completed links. The CLI resolves ad-hoc links or rewrites a
saved capture file; cache files are meant to be overwritten on every
checkpoint, unlike export outputs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

GOOGLE = "https://www.google.com"
_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))


def opaque_redirect(link: str) -> bool:
    """True for Google /url or /goto links whose target is not a decodable URL."""
    parsed = urlparse(link)
    if (parsed.hostname or "").lower() not in ("google.com", "www.google.com") or parsed.path not in ("/url", "/goto"):
        return False
    params = parse_qs(parsed.query)
    target = (params.get("url") or params.get("q") or [""])[0]
    destination = urlparse(unquote(target))
    return not (destination.scheme in ("http", "https") and destination.hostname)


def redirect_destination(response) -> str | None:
    """The observed non-Google HTTP(S) destination of one redirect response."""
    if response.status not in _REDIRECT_STATUSES:
        return None
    location = response.headers.get("location")
    if not location:
        return None
    destination = urljoin(GOOGLE, location.strip())
    parsed = urlparse(destination)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host or host == "google.com" or host.endswith(".google.com"):
        return None
    return destination


def _walk_content_dicts(response: dict, handle) -> None:
    """Call handle for every dict inside organic results and AIO content."""

    def visit(node):
        if isinstance(node, dict):
            handle(node)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(response.get("organic_results"))
    overview = response.get("ai_overview")
    if isinstance(overview, dict):
        visit(overview.get("references"))
        visit(overview.get("text_blocks"))


def collect_opaque_links(response: dict) -> list[str]:
    """Unique opaque redirect links in first-encounter order."""
    ordered: dict[str, None] = {}

    def collect(node):
        link = node.get("link")
        if isinstance(link, str) and opaque_redirect(link):
            ordered.setdefault(link, None)

    _walk_content_dicts(response, collect)
    return list(ordered)


def apply_redirect_resolutions(response: dict, resolutions: dict[str, str]) -> None:
    """Store observed destinations, retaining each original as redirect_link."""

    def apply(node):
        link = node.get("link")
        if isinstance(link, str) and link in resolutions:
            node["link"] = resolutions[link]
            node["redirect_link"] = link

    _walk_content_dicts(response, apply)


async def observe_redirects(request_context, links: list[str], referer: str | None, *,
                            deadline: float | None, per_link_timeout_ms: int,
                            concurrency: int) -> dict[str, str]:
    """Observe Google's redirect response once per link, never following it.

    One bounded request per link; a redirect status with an acceptable
    Location header supplies the destination, and no destination page content
    is ever downloaded. Any per-link failure simply leaves that link
    unresolved. Links past the deadline are left for the caller's retry.
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def observe(link: str) -> tuple[str, str | None]:
        async with semaphore:
            if deadline is not None and time.monotonic() >= deadline:
                return link, None
            try:
                response = await request_context.get(
                    link, max_redirects=0, timeout=per_link_timeout_ms, headers={"Referer": referer})
            except Exception:
                return link, None
            return link, redirect_destination(response)

    observed = await asyncio.gather(*(observe(link) for link in links))
    return {link: destination for link, destination in observed if destination}


async def resolve_redirect_links(response: dict, page, *, budget_seconds: float = 75.0,
                                 settle_seconds: float = 45.0,
                                 per_link_timeout_ms: int = 10000, max_links: int = 50,
                                 concurrency: int = 4) -> None:
    """Resolve opaque redirect links by observing Google in the live session.

    Each resolved link becomes its observed non-Google HTTP(S) destination,
    retaining the original under redirect_link, with at most two observations
    per link: fresh in-session /goto links answer with an HTML interstitial
    whose own meta-refresh delay runs about 24-26s, and the identical link
    observes a clean 3xx once that window passes (28s in one recorded probe,
    with some links still interstitial near 38s, so the settle covers every
    window observed so far). One settle retry for still-unresolved links
    mirrors the pipeline's one lazy follow-up doctrine. Unresolved links stay
    exactly as parsed and stay flagged by the shared-record warning, so a
    failed or budget-exhausted resolution never fails the capture itself.
    `attempted` counts the unique links submitted to this step; `resolved`
    counts the destinations actually observed.
    """
    pending = collect_opaque_links(response)[:max_links]
    if not pending:
        return
    deadline = time.monotonic() + budget_seconds
    resolutions: dict[str, str] = {}
    try:
        referer = page.url
        request_context = page.context.request
        resolutions = await observe_redirects(
            request_context, pending, referer, deadline=deadline,
            per_link_timeout_ms=per_link_timeout_ms, concurrency=concurrency)
        unresolved = [link for link in pending if link not in resolutions]
        if unresolved and deadline - time.monotonic() > settle_seconds:
            await asyncio.sleep(settle_seconds)
            resolutions.update(await observe_redirects(
                request_context, unresolved, referer, deadline=deadline,
                per_link_timeout_ms=per_link_timeout_ms, concurrency=concurrency))
    except Exception as exc:
        print(f"[scrape] redirect resolution unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
    apply_redirect_resolutions(response, resolutions)
    response.setdefault("extraction_metadata", {})["redirect_resolution"] = {
        "attempted": len(pending), "resolved": len(resolutions)}
    print(f"[scrape] redirect resolution: {len(resolutions)}/{len(pending)} opaque links observed",
          file=sys.stderr)


def capture_results(capture) -> list[dict]:
    """Extract the response objects from a run envelope, a list, or one response."""
    if isinstance(capture, dict) and isinstance(capture.get("results"), list):
        results = [entry.get("result") for entry in capture["results"] if isinstance(entry, dict)]
    elif isinstance(capture, list):
        results = capture
    elif isinstance(capture, dict) and any(
            key in capture for key in ("search_parameters", "organic_results", "ai_overview", "extraction_metadata")):
        results = [capture]
    else:
        raise ValueError("capture must be a run envelope, a list of responses, or one response object")
    if not results or any(not isinstance(result, dict) for result in results):
        raise ValueError("capture must contain at least one response object")
    return results


def read_resolutions(path: str | Path) -> dict[str, str]:
    """Load a checkpointed link->destination cache; missing files start empty."""
    path = Path(path)
    if not path.is_file():
        return {}
    cache = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cache, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in cache.items()):
        raise ValueError(f"resolution cache at {path} must map link strings to destination strings")
    return cache


def write_resolutions(path: str | Path, cache: dict[str, str]) -> None:
    """Publish the cache atomically; caches are overwritten on every checkpoint."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                      prefix=path.name + ".", suffix=".tmp", delete=False) as staging:
        staging.write(payload)
        staging.flush()
        os.replace(staging.name, path)


async def resolve_capture_links(capture, page, *, cache_path: str | Path | None = None,
                                settle_seconds: float = 45.0, per_link_timeout_ms: int = 10000,
                                concurrency: int = 4, chunk_size: int = 200,
                                chunk_pause_seconds: float = 0.2,
                                progress=None) -> dict:
    """Resolve every opaque link in a saved capture against the live session.

    Unique links resolve once across all responses; successes checkpoint to
    `cache_path` after every chunk so an interrupted rerun resumes instead of
    re-observing completed links. Still-unresolved links get one settle retry
    (pass `settle_seconds=0` for links already past their interstitial
    window, where a failure means the token is spent rather than fresh).
    Each response keeps its per-result `redirect_resolution` counts, and the
    returned summary reports unique-link totals, not per-response duplicates.
    """
    results = capture_results(capture)
    cache = read_resolutions(cache_path) if cache_path else {}
    referer = page.url if str(page.url).startswith("http") else GOOGLE + "/search"
    ordered: dict[str, None] = {}
    for result in results:
        for link in collect_opaque_links(result):
            ordered.setdefault(link, None)
    links = list(ordered)
    pending = [link for link in links if link not in cache]
    request_context = page.context.request
    report = progress or (lambda done, total, resolved: print(
        f"[resolve] {done}/{total} unique links observed ({resolved} resolved)", file=sys.stderr))

    async def sweep(targets: list[str]) -> None:
        for start in range(0, len(targets), chunk_size):
            chunk = targets[start:start + chunk_size]
            resolutions = await observe_redirects(
                request_context, chunk, referer, deadline=None,
                per_link_timeout_ms=per_link_timeout_ms, concurrency=concurrency)
            cache.update(resolutions)
            if cache_path:
                write_resolutions(cache_path, cache)
            report(min(start + chunk_size, len(targets)), len(targets), len(cache))
            if start + chunk_size < len(targets) and chunk_pause_seconds:
                await asyncio.sleep(chunk_pause_seconds)

    await sweep(pending)
    unresolved = [link for link in links if link not in cache]
    if unresolved and settle_seconds > 0:
        await asyncio.sleep(settle_seconds)
        await sweep(unresolved)

    for result in results:
        result_links = collect_opaque_links(result)
        apply_redirect_resolutions(result, cache)
        result.setdefault("extraction_metadata", {})["redirect_resolution"] = {
            "attempted": len(result_links), "resolved": sum(link in cache for link in result_links)}
    return {"results": len(results), "unique_links": len(links),
            "resolved": sum(link in cache for link in links), "unresolved": len(unresolved)}


async def _open_session(browser: str | None, proxy_mode: str | None):
    """One live browser session from the same engine/profile rules as capture."""
    from aio_scraper import GoogleAIOScraper
    return GoogleAIOScraper(engine=browser, proxy_mode=proxy_mode)


async def _run(argv: argparse.Namespace) -> int:
    if not argv.link and not argv.input:
        raise ValueError("pass --link or --input")
    if bool(argv.input) != bool(argv.output):
        raise ValueError("--input and --output are used together")
    async with await _open_session(argv.browser, argv.proxy) as scraper:
        page = scraper.page
        if argv.link:
            links = list(dict.fromkeys(argv.link))
            resolutions = await observe_redirects(
                page.context.request, links, page.url if str(page.url).startswith("http") else GOOGLE + "/search",
                deadline=None, per_link_timeout_ms=10000, concurrency=4)
            unresolved = [link for link in links if link not in resolutions]
            if unresolved and argv.settle_seconds > 0:
                await asyncio.sleep(argv.settle_seconds)
                resolutions.update(await observe_redirects(
                    page.context.request, unresolved, GOOGLE + "/search", deadline=None,
                    per_link_timeout_ms=10000, concurrency=4))
            print(json.dumps({link: resolutions.get(link) for link in links}))
            return 0
        capture = json.loads(argv.input.read_text(encoding="utf-8"))
        summary = await resolve_capture_links(
            capture, page, cache_path=argv.cache, settle_seconds=argv.settle_seconds)
        if argv.output.exists():
            raise FileExistsError(f"output already exists: {argv.output}")
        argv.output.write_text(json.dumps(capture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({**summary, "output": str(argv.output)}))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False, description=__doc__)
    parser.add_argument("--link", action="append", help="One opaque Google redirect link; repeatable")
    parser.add_argument("--input", type=Path, help="Saved native capture to resolve (run envelope, list or response)")
    parser.add_argument("--output", type=Path, help="Where to write the resolved capture; must not exist")
    parser.add_argument("--cache", type=Path, help="Checkpointed link->destination cache; overwritten as it grows")
    parser.add_argument("--settle-seconds", type=float, default=45.0,
                        help="Retry wait for fresh interstitial links; use 0 for stale captures (default 45)")
    parser.add_argument("--browser", choices=("chromium", "firefox"), help="Override GOOGLE_AIO_BROWSER")
    parser.add_argument("--proxy", choices=("direct", "dataimpulse"), help="Override GOOGLE_AIO_PROXY")
    args = parser.parse_args(argv)
    if args.settle_seconds < 0:
        parser.error("--settle-seconds must be nonnegative")
    try:
        return asyncio.run(_run(args))
    except (OSError, ValueError, json.JSONDecodeError, FileExistsError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
