"""Google AI Overview scraper with conservative, SerpApi-shaped JSON output."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


class ScrapeError(RuntimeError):
    """The page could not be interpreted safely as a Google results page."""


_ENV_LOADED = False


def _load_env_file() -> None:
    """Populate os.environ from a .env file next to this module.

    Real exported variables win over .env values, so callers can still
    override anything on the command line.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScrapeError(f"Could not read .env file at {env_path}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("export "):
            stripped = stripped[7:].strip()
        key, sep, value = stripped.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


GOOGLE = "https://www.google.com"
NOISE = re.compile(r"^(?:An AI Overview is not available for this search|Can't generate an AI overview right now\. Try again later\.)$", re.I)


def _clean(text: str) -> str:
    return " ".join(text.split())


def _source_name(title: str, aria: str, link: str) -> str:
    label = re.sub(r"\. Opens in new tab\.?$", "", aria, flags=re.I).strip()
    for sep in (" - ", " | ", " – ", " — "):
        if sep in label:
            return label.rsplit(sep, 1)[-1].strip()
    host = urlparse(link).hostname or ""
    return label or (host.removeprefix("www.") if host else title)


def _safe_reference_url(raw: str) -> str | None:
    resolved = urljoin(GOOGLE, raw)
    parsed = urlparse(resolved)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.hostname in ("google.com", "www.google.com") and parsed.path == "/url":
        params = parse_qs(parsed.query)
        target = (params.get("q") or params.get("url") or [""])[0]
        destination = urlparse(target)
        if destination.scheme in ("http", "https") and destination.hostname:
            return target
        # Google also supplies opaque URL-safe tokens, not decodable URLs.
        # Preserve the redirect itself without following it or inventing a link.
        if re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", target):
            return resolved
        return None
    if raw.startswith("/goto?") or urlparse(raw).scheme in ("http", "https"):
        return resolved
    return None


def _organic_url(raw: str) -> str | None:
    parsed = urlparse(urljoin(GOOGLE, raw))
    if parsed.hostname in ("www.google.com", "google.com") and parsed.path == "/url":
        raw = (parse_qs(parsed.query).get("q") or parse_qs(parsed.query).get("url") or [""])[0]
        parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host == "google.com" or host.endswith(".google.com"):
        # Google's /goto destination is opaque. Keep the redirect URL, as we
        # do for AI references, rather than dropping a valid result or fetching it.
        if host in ("google.com", "www.google.com") and parsed.path == "/goto" and parse_qs(parsed.query).get("url"):
            return urljoin(GOOGLE, raw)
        return None
    return urljoin(GOOGLE, raw)


def _organic_results(results_page: Tag) -> list[dict]:
    """Read standard result cards in DOM order, excluding AIO and nested cards."""
    rso = results_page if results_page.get("id") == "rso" else results_page.select_one("#rso")
    scope = rso or results_page
    results = []
    # Some Google layouts use .MjjYud as the card without an inner .g.
    for card in scope.select(".g, .MjjYud"):
        if (card.find_parent(class_="g") or card.get("id") == "eKIzJc"
                or card.find_parent(id="eKIzJc") or card.find(id="eKIzJc")
                or ("MjjYud" in card.get("class", []) and card.select_one(".g"))):
            continue
        heading = card.select_one("h3")
        anchor = heading.find_parent("a", href=True) if heading else None
        if not anchor:
            continue
        link = _organic_url(anchor["href"])
        title = _clean(heading.get_text(" ", strip=True))
        if not link or not title:
            continue
        snippet_node = card.select_one(".VwiC3b, .IsZvec, .yXK7lf")
        results.append({"position": len(results) + 1, "title": title, "link": link,
                        "snippet": _visible_text(snippet_node) if snippet_node else ""})
    return results


def _reference_key(raw: str) -> str | None:
    """Match source cards and inline citations without Google's tracking fields."""
    link = _safe_reference_url(raw)
    if link is None:
        return None
    parsed = urlparse(link)
    if parsed.hostname in ("google.com", "www.google.com") and parsed.path in ("/url", "/goto"):
        params = parse_qs(parsed.query)
        target = (params.get("q") or params.get("url") or [""])[0]
        if target:
            return f"{GOOGLE}{parsed.path}?url={quote_plus(target)}"
    return link


def _reference_indexes(node: Tag, by_href: dict[str, int]) -> list[int]:
    indexes = []
    for anchor in node.select("a[href]"):
        index = by_href.get(_reference_key(anchor.get("href", "")))
        if index is not None and index not in indexes:
            indexes.append(index)
    return indexes


def _list_block(list_node: Tag, by_href: dict[str, int]) -> dict | None:
    items = []
    for item in list_node.find_all("li", recursive=True):
        snippet = _visible_text(item)
        if not snippet:
            continue
        block_item = {"snippet": snippet}
        indexes = _reference_indexes(item, by_href)
        if indexes:
            block_item["reference_indexes"] = indexes
        items.append(block_item)
    return {"type": "list", "list": items} if items else None


def _visible_text(node: Tag) -> str:
    clone = BeautifulSoup(str(node), "html.parser")
    for hidden in clone.select('[hidden], [aria-hidden="true"], script, style, button, [role="button"], [data-src-id]'):
        hidden.decompose()
    for fallback in clone.select('[jsname="lGsj1"], [jsname="L1sC6e"]'):
        fallback.decompose()
    text = _clean(clone.get_text(" ", strip=True))
    return "" if NOISE.fullmatch(text) else text


def parse_search_html(html: str, query: str, hl: str, gl: str) -> dict:
    """Return search data with optional AIO and ordered organic results."""
    soup = BeautifulSoup(html, "html.parser")
    sample = soup.get_text(" ", strip=True)[:5000].lower()
    if "/sorry/" in sample or "unusual traffic" in sample or "recaptcha" in sample:
        raise ScrapeError("Google blocked the request")

    results_page = soup.select_one("#search, #rso")
    if results_page is None:
        raise ScrapeError("Google returned an incomplete page, not a search results page")

    response = {"search_parameters": {"engine": "google", "q": query, "hl": hl, "gl": gl}}
    organic = _organic_results(results_page)
    if organic:
        response["organic_results"] = organic
    else:
        rso = results_page if results_page.get("id") == "rso" else results_page.select_one("#rso")
        scope = rso or results_page
        print(f"[parser] no organic cards found (rso={rso is not None}, "
              f"headings={len(scope.select('h3'))}, .g={len(scope.select('.g'))}, "
              f".MjjYud={len(scope.select('.MjjYud'))})", file=sys.stderr)

    container = soup.select_one("#eKIzJc")
    if container is None:
        return response
    # Google may split the first answer sentence into a fragment span and siblings.
    # Prefer the enclosing answer group when Google emits it.
    rendered_group = container.select_one('[data-subtree="aimc"]')
    answer = (container.select_one('[data-subtree="aimc"] [jsname="KFl8ub"]')
              or container.select_one('[data-subtree="aimfl,mfl"]')
              or rendered_group)
    if answer is None or not _visible_text(answer):
        # Two benign shapes land here: (a) Google's explicit "not available"
        # message for sensitive topics, and (b) a container shell whose answer
        # never streamed in. Both mean no AIO to report, not a scrape failure.
        note = container.get_text(" ", strip=True)[:120]
        if "not available" in note.lower():
            print(f"[parser] Google states no AI Overview for this query", file=sys.stderr)
        else:
            print(f"[parser] AIO container present but answer never rendered; treating as no-AIO (container text: {note!r})", file=sys.stderr)
        return response

    cards: list[dict] = []
    by_href: dict[str, int] = {}
    for card in container.select("[data-src-id]"):
        anchor = card.select_one("a[href]")
        if anchor is None:
            continue
        link = _safe_reference_url(anchor.get("href", ""))
        if not link:
            continue
        key = _reference_key(anchor.get("href", ""))
        if key in by_href:
            continue
        title = _clean(re.sub(r"\. Opens in new tab\.?$", "", anchor.get("aria-label", ""), flags=re.I))
        if not title:
            title = _clean(anchor.get_text(" ", strip=True))
        if not title:
            continue
        aria = anchor.get("aria-label", "")
        by_href[key] = len(cards)
        cards.append({"index": len(cards), "title": title, "link": link,
                      "source": _source_name(title, aria, link)})

    blocks: list[dict] = []
    # Current Google markup has no <p> for its opening answer. Read KFl8ub's
    # direct children in order so the opening text, headings, and lists survive.
    if rendered_group is not None and answer.get("jsname") == "KFl8ub":
        direct_nodes = [node for node in answer.find_all(recursive=False) if isinstance(node, Tag)]
        # Google appends a follow-up prompt after the answer. Only the English
        # prompt text is recognized; other locales are a documented limitation.
        followup_prefix = "If you'd like, I can share information about:"
        for node in direct_nodes:
            if not isinstance(node, Tag):
                continue
            if followup_prefix in node.get_text(" ", strip=True):
                continue
            text = _visible_text(node)
            if not text:
                continue
            heading = node if node.get("role") == "heading" or node.name in ("h1", "h2", "h3") else node.select_one('[role="heading"], h1, h2, h3')
            if heading is not None and _visible_text(heading) == text:
                blocks.append({"type": "heading", "snippet": text})
                continue
            list_node = node if node.name in ("ul", "ol") else node.find(["ul", "ol"])
            if list_node is not None:
                list_block = _list_block(list_node, by_href)
                if list_block:
                    blocks.append(list_block)
                continue
            # Remove source chips from answer text; their link anchors remain
            # available for unambiguous citation-index mapping.
            answer_text = _visible_text(node)
            chip_texts = [_clean(button.get_text(" ", strip=True)) for button in node.select('button[aria-label*="Related results"]')]
            for chip in chip_texts:
                if chip:
                    answer_text = answer_text.replace(chip, " ").strip()
            if answer_text:
                block = {"type": "paragraph", "snippet": answer_text}
                indexes = _reference_indexes(node, by_href)
                if indexes:
                    block["reference_indexes"] = indexes
                blocks.append(block)
    else:
        block_nodes = answer.select('h1, h2, h3, [role="heading"], p, li')
        covered: set[int] = set()
        for node in block_nodes:
            text = _visible_text(node)
            if not text:
                continue
            if node.name == "li":
                parent_list = node.find_parent(["ul", "ol"])
                if parent_list is not None:
                    key = id(parent_list)
                    if key in covered:
                        continue
                    covered.add(key)
                    list_block = _list_block(parent_list, by_href)
                    if list_block:
                        blocks.append(list_block)
                continue
            kind = "heading" if node.name in ("h1", "h2", "h3") or node.get("role") == "heading" else "paragraph"
            block = {"type": kind, "snippet": text}
            ref_indexes = _reference_indexes(node, by_href)
            if ref_indexes:
                block["reference_indexes"] = ref_indexes
            blocks.append(block)

    # Generic Google markup may not use semantic elements. Keep its answer rather
    # than silently returning an empty overview, but do not infer block structure.
    if not blocks:
        blocks = [{"type": "paragraph", "snippet": _visible_text(answer)}]
        for anchor in answer.select("a[href]"):
            index = by_href.get(_reference_key(anchor.get("href", "")))
            if index is not None:
                blocks[0].setdefault("reference_indexes", []).append(index)

    response["ai_overview"] = {"text_blocks": blocks, "references": cards}
    return response


# --- Google CAPTCHA challenge detection -------------------------------------

_CHALLENGE_SELECTORS = (
    "#captcha-form",
    "#recaptcha-anchor",
    "#g-recaptcha-response",
)


class ChallengeDetected(RuntimeError):
    """Google served a CAPTCHA / sorry page instead of search results."""

    def __init__(self, message: str, website_url: str = "unknown", website_key: str = "", is_enterprise: bool = False):
        self.website_url = website_url
        self.website_key = website_key
        self.is_enterprise = is_enterprise
        super().__init__(message)


def is_challenge_page(html: str) -> bool:
    """True when the html looks like Google's /sorry CAPTCHA interstitial."""
    soup = BeautifulSoup(html, "html.parser")
    sample = soup.get_text(" ", strip=True)[:5000].lower()
    if not ("/sorry/" in sample or "unusual traffic" in sample or "recaptcha" in sample):
        return False
    # Require at least one structural captcha marker so ordinary pages that
    # merely mention the word "recaptcha" in a result snippet are not flagged.
    if any(soup.select_one(sel) is not None for sel in _CHALLENGE_SELECTORS):
        return True
    return bool(soup.select("iframe[src*='recaptcha']"))


def _detect_enterprise(soup: BeautifulSoup) -> bool:
    """True when any recaptcha iframe uses the /enterprise/ path."""
    for frame in soup.select("iframe[src*='recaptcha']"):
        if "/recaptcha/enterprise/" in frame.get("src", ""):
            return True
    return False


def _extract_s_value(soup: BeautifulSoup) -> str | None:
    """Best-effort extraction of the enterprisePayload 's' parameter."""
    for script in soup.select("script"):
        body = script.string or script.get_text() or ""
        match = re.search(r"[\" ']s[\" ']\s*:\s*[\" ']([0-9A-Za-z_\-=/+]{8,})[\" ']", body)
        if match:
            return match.group(1)
    return None


def extract_challenge_params(html: str, page_url: str) -> dict:
    """Pull the parameters a solver needs from a challenge page.

    Raises ChallengeDetected when no sitekey can be recovered; the exception
    carries page_url so callers can still report which URL was blocked.
    """
    soup = BeautifulSoup(html, "html.parser")
    is_enterprise = _detect_enterprise(soup)

    website_key = None
    key_source = None
    keyed = soup.select_one("[data-sitekey]")
    if keyed and keyed.get("data-sitekey"):
        website_key = keyed["data-sitekey"].strip()
        key_source = "data-sitekey"
    if not website_key:
        # Anchor iframes carry the sitekey as the ?k= query parameter.
        for frame in soup.select("iframe[src*='/recaptcha/']"):
            parsed = urlparse(frame.get("src", ""))
            candidate = (parse_qs(parsed.query).get("k") or [None])[0] if parsed.query else None
            if candidate and re.fullmatch(r"[0-9A-Za-z_-]{40}", candidate):
                website_key = candidate
                key_source = f"iframe:{parsed.path}"
                break
    if not website_key:
        raise ChallengeDetected(
            f"Challenge page found but sitekey could not be extracted from {page_url}",
            website_url=page_url,
            is_enterprise=is_enterprise,
        )

    form = soup.select_one("#captcha-form")
    data_s = None
    s_tag = soup.select_one("[data-s]")
    if s_tag and s_tag.get("data-s"):
        data_s = s_tag["data-s"].strip()
    return {
        "website_url": page_url,
        "website_key": website_key,
        "key_source": key_source,
        "is_enterprise": is_enterprise,
        "form_action": form.get("action") if form else None,
        "s_value": _extract_s_value(soup),
        "data_s": data_s,
    }


def _proxy_settings(mode: str | None = None) -> dict | None:
    """Select direct access or explicitly enable stored DataImpulse credentials."""
    mode = (mode if mode is not None else os.getenv("GOOGLE_AIO_PROXY", "direct")).strip().lower() or "direct"
    if mode == "direct":
        return None
    if mode != "dataimpulse":
        raise ScrapeError("GOOGLE_AIO_PROXY must be 'direct' or 'dataimpulse' (or use --proxy)")
    proxy_url = os.getenv("DATAIMPULSE_PROXY_URL")
    if proxy_url:
        try:
            parsed = urlparse(proxy_url)
            port = parsed.port or 823
        except ValueError as exc:
            raise ScrapeError("DATAIMPULSE_PROXY_URL has an invalid port") from exc
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not 1 <= port <= 65535:
            raise ScrapeError("DATAIMPULSE_PROXY_URL must be a valid HTTP(S) proxy URL")
        config = {"server": f"{parsed.scheme}://{parsed.hostname}:{port}"}
        # urlparse leaves userinfo percent-encoded; Playwright expects the
        # literal credentials.
        if parsed.username is not None:
            config["username"] = unquote(parsed.username)
        if parsed.password is not None:
            config["password"] = unquote(parsed.password)
        return config
    user = os.getenv("DATAIMPULSE_PROXY_USER")
    password = os.getenv("DATAIMPULSE_PROXY_PASSWORD")
    if bool(user) != bool(password):
        raise ScrapeError("Set both DATAIMPULSE_PROXY_USER and DATAIMPULSE_PROXY_PASSWORD")
    if not user:
        raise ScrapeError("DataImpulse proxy requested but credentials are missing; set DATAIMPULSE_PROXY_URL or both DATAIMPULSE_PROXY_USER and DATAIMPULSE_PROXY_PASSWORD")
    host = os.getenv("DATAIMPULSE_PROXY_HOST", "gw.dataimpulse.com")
    port = os.getenv("DATAIMPULSE_PROXY_PORT", "823")
    return {"server": f"http://{host}:{port}", "username": user, "password": password}


# --- 2Captcha client (in.php / res.php, mirrors the official 2captcha-python lib) ---

TWOCAPTCHA_IN_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RES_URL = "https://2captcha.com/res.php"


class CaptchaSolverError(RuntimeError):
    """The 2Captcha round-trip failed or returned no usable token."""


def _solve_variants(params: dict) -> list[dict]:
    """Task shapes mirroring TwoCaptcha.recaptcha(sitekey, url, enterprise=1).

    Google Search specifics per the official lib: pass the challenge page's
    data-s value, our browser cookies, and our User-Agent so the worker solves
    in (approximately) our session context. Enterprise shape first, classic
    v2 as fallback.
    """
    base = {
        "googlekey": params["website_key"],
        "pageurl": params["website_url"],
        "json": "1",
    }
    if params.get("user_agent"):
        base["userAgent"] = params["user_agent"]
    if params.get("cookies"):
        base["cookies"] = params["cookies"]
    if params.get("data_s"):
        base["data-s"] = params["data_s"]
    variants = []
    if params.get("is_enterprise"):
        variants.append(dict(base, method="userrecaptcha", enterprise="1"))
    variants.append(dict(base, method="userrecaptcha"))
    return variants


def _captcha_http_json(url: str, data: bytes | None = None, timeout_seconds: float = 30.0) -> dict:
    """Blocking HTTP request to 2Captcha decoding the json=1 envelope."""
    from urllib.request import Request, urlopen

    request = Request(url, data=data, headers={"User-Agent": "aio-scraper/0.1"})
    with urlopen(request, timeout=timeout_seconds) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CaptchaSolverError(f"2Captcha returned non-JSON response: {body[:200]}") from exc
    return payload


def _submit_and_poll_sync(api_key: str, params: dict, poll_seconds: float = 5.0, max_wait_seconds: float = 180.0) -> dict:
    """Submit a task and poll until a token arrives or the deadline passes.

    Returns {"token": ..., "cookies": "..."} — worker cookies are returned by
    res.php (json=1) when the request carried a cookies parameter.
    """
    from urllib.parse import urlencode

    last_error = "unknown"
    for variant in _solve_variants(params):
        body = urlencode({"key": api_key, **variant}).encode("utf-8")
        try:
            payload = _captcha_http_json(TWOCAPTCHA_IN_URL, data=body)
        except CaptchaSolverError as exc:
            last_error = str(exc)
            continue
        except Exception as exc:
            raise CaptchaSolverError(f"Could not submit task to 2Captcha: {exc}") from exc
        if str(payload.get("status")) != "1":
            last_error = f"2Captcha submit error: {payload.get('request', payload)}"
            continue
        task_id = str(payload.get("request", ""))
        if not task_id:
            last_error = "2Captcha accepted the task but returned no task id"
            continue

        deadline = time.monotonic() + max_wait_seconds
        res_query = urlencode({"key": api_key, "action": "get", "id": task_id, "json": "1"})
        _NOT_READY = ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY")
        while time.monotonic() < deadline:
            time.sleep(poll_seconds)
            try:
                res = _captcha_http_json(f"{TWOCAPTCHA_RES_URL}?{res_query}")
            except CaptchaSolverError as exc:
                if any(mark in str(exc) for mark in _NOT_READY):
                    continue
                raise
            except Exception as exc:
                raise CaptchaSolverError(f"Could not poll 2Captcha: {exc}") from exc
            if str(res.get("status")) == "1":
                token = res.get("request")
                if token:
                    return {"token": str(token), "cookies": str(res.get("cookies", ""))}
            if not any(mark in str(res.get("request", "")) for mark in _NOT_READY):
                raise CaptchaSolverError(f"2Captcha error: {res.get('request', res)}")
        last_error = f"2Captcha did not return a token within {max_wait_seconds:.0f}s"
    raise CaptchaSolverError(f"No 2Captcha variant could return a token ({last_error})")


async def solve_recaptcha_v2(api_key: str, params: dict) -> dict:
    """Async wrapper so the event loop stays responsive while polling runs."""
    import functools

    loop = asyncio.get_running_loop()
    partial = functools.partial(_submit_and_poll_sync, api_key, params)
    return await loop.run_in_executor(None, partial)


async def _maybe_solve_challenge(page, page_url: str, html: str, timeout_ms: int, solver_used: dict) -> str | None:
    """If page is a challenge, solve via 2Captcha and navigate back to /search.

    Returns the new page's html, or None when the page was not a challenge.
    Marks solver_used["token"] so callers know when the solver ran. If a token
    is accepted but the page stays on /sorry/, retry once: reload the query,
    and if the challenge re-appears, solve with a fresh token (the risk
    decision can differ once session cookies have been updated).
    """
    if not is_challenge_page(await page.content()):
        return None
    api_key = os.getenv("TWOCAPTCHA_API_KEY", "").strip()
    if not api_key:
        raise ChallengeDetected(
            "Google blocked the request with a CAPTCHA page and TWOCAPTCHA_API_KEY is not set",
            website_url=page_url,
        )

    max_attempts = 2
    last_stay_url = page_url
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            print(f"[captcha] token rejected; retrying with a fresh token (attempt {attempt}/{max_attempts})", file=sys.stderr)
            await asyncio.sleep(3)
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(min(2000, timeout_ms // 4))
            except Exception as exc:
                raise ChallengeDetected(f"Retry navigation to the challenge page failed: {exc}", website_url=page_url) from exc
            if not is_challenge_page(await page.content()):
                # The previous token took effect after a plain reload.
                print("[captcha] retry landed on the SERP without a new solve", file=sys.stderr)
                return await page.content()

        params = extract_challenge_params(await page.content(), page_url)
        # Google-Search-specific context alignment (per official 2captcha lib):
        # give the worker our cookies and User-Agent, and any data-s value the
        # challenge page carries, so the token is issued against our session.
        params["user_agent"] = await page.evaluate("navigator.userAgent")
        google_cookies = await page.context.cookies("https://www.google.com")
        params["cookies"] = ";".join(f"{c['name']}:{c['value']}" for c in google_cookies if c.get("value"))
        print(f"[captcha] challenge detected at {urlparse(page_url).path}; submitting to 2Captcha (cookie alignment on)", file=sys.stderr)
        try:
            solve_result = await solve_recaptcha_v2(api_key, params)
        except CaptchaSolverError as exc:
            raise ChallengeDetected(
                f"2Captcha solver failed: {exc}",
                website_url=page_url,
                website_key=params["website_key"],
                is_enterprise=params["is_enterprise"],
            ) from exc
        token = solve_result["token"]
        worker_cookies = solve_result.get("cookies", "")
        if worker_cookies:
            # Adopt the worker's session cookies so the submitting context matches
            # the context the token was issued for.
            adopted = []
            for pair in worker_cookies.split(";"):
                name, sep, value = pair.strip().partition("=")
                if sep and name:
                    adopted.append({"name": name, "value": value, "domain": ".google.com", "path": "/"})
            if adopted:
                try:
                    await page.context.add_cookies(adopted)
                    print(f"[captcha] adopted {len(adopted)} worker cookies", file=sys.stderr)
                except Exception as exc:
                    print(f"[captcha] could not adopt worker cookies: {exc}", file=sys.stderr)
        solver_used["token"] = True
        print(f"[captcha] token received (len={len(token)}), injecting", file=sys.stderr)

        # The /sorry form reads the page-level #g-recaptcha-response textarea on
        # submit. Set it and POST the form directly. Do not fire the widget
        # callback: it can reset the textarea before the form posts.
        try:
            await page.wait_for_selector("#g-recaptcha-response", state="attached", timeout=10000)
            await page.eval_on_selector(
                "#g-recaptcha-response",
                "(el, value) => { el.style.display = 'block'; el.value = value; }",
                token,
            )
            await page.evaluate(
                "() => { const f = document.querySelector('#captcha-form'); if (f) f.submit(); }"
            )
            print("[captcha] challenge form submitted", file=sys.stderr)
        except Exception as exc:
            # A reloaded challenge variant can omit the widget entirely; a
            # fresh reload on the next attempt usually restores it.
            last_error = f"Could not inject 2Captcha token into the challenge page: {exc}"
            print(f"[captcha] {last_error}", file=sys.stderr)
            continue

        # Wait for Google to process the token and land us back on /search.
        try:
            await page.wait_for_url("**/search?*", timeout=timeout_ms)
        except Exception:
            last_stay_url = page.url
            continue  # retry with a fresh token

        await page.wait_for_timeout(min(1500, timeout_ms // 4))
        print(f"[captcha] landed on {page.url[:80]}", file=sys.stderr)
        return await page.content()

    raise ChallengeDetected(
        last_error
        or f"Token submitted but page stayed on {last_stay_url} after {max_attempts} attempts (Google likely rejected it)",
        website_url=last_stay_url,
    )


def _browser_engine() -> str:
    # DataImpulse proxies hang Playwright Chromium but work with Playwright
    # Firefox; GOOGLE_AIO_BROWSER selects the engine when a proxy is set.
    engine = os.getenv("GOOGLE_AIO_BROWSER", "chromium").strip().lower()
    if engine not in ("chromium", "firefox"):
        raise ScrapeError("GOOGLE_AIO_BROWSER must be 'chromium' or 'firefox'")
    return engine


def _headless_flag() -> bool:
    # Headless Chromium scores much worse in Google's risk analysis, which can
    # make even a valid solver token fail on /sorry. GOOGLE_AIO_HEADLESS=0
    # runs a visible browser window instead.
    return os.getenv("GOOGLE_AIO_HEADLESS", "1") != "0"


class GoogleAIOScraper:
    """One browser session reused across many searches.

    Use as an async context manager. The browser (and its persistent profile,
    so the Google session and any solved-captcha cookies) stays alive for the
    whole lifetime, making batch runs cheap: only the page navigates.
    """

    def __init__(self, engine: str | None = None, headless: bool | None = None,
                 proxy: dict | None = None, timeout_ms: int = 30000,
                 proxy_mode: str | None = None):
        self.engine = engine or _browser_engine()
        self.headless = _headless_flag() if headless is None else headless
        # A caller-supplied proxy dictionary overrides environment/mode selection.
        self.proxy = _proxy_settings(proxy_mode) if proxy is None else proxy
        self.timeout_ms = timeout_ms
        self._pw = None
        self.browser = None
        self.page = None
        self._aio_shell_seen = False

    async def __aenter__(self):
        from playwright.async_api import async_playwright

        if self.proxy and self.engine == "chromium":
            print("[proxy] Chromium navigation through a proxy may hang. Use --proxy direct or GOOGLE_AIO_BROWSER=firefox with a separate profile.", file=sys.stderr)
        self._pw = await async_playwright().start()
        # A persistent profile keeps Google's session cookies across runs, so
        # after one successful solve subsequent searches reuse the session and
        # skip the challenge entirely (saves 2Captcha credits).
        profile_dir = os.getenv("GOOGLE_AIO_PROFILE", str(Path(__file__).resolve().parent / ".aio-profile"))
        persistent = profile_dir.lower() not in ("", "0", "off", "none")
        try:
            browser_launcher = getattr(self._pw, self.engine)
            if persistent:
                Path(profile_dir).mkdir(parents=True, exist_ok=True)
                kwargs = dict(user_data_dir=profile_dir, headless=self.headless, proxy=self.proxy)
                if self.engine == "firefox":
                    kwargs["firefox_user_prefs"] = {"network.proxy.allow_hijacking_localhost": True}
                self.browser = await browser_launcher.launch_persistent_context(**kwargs)
            elif self.engine == "firefox":
                self.browser = await browser_launcher.launch(
                    headless=self.headless,
                    proxy=self.proxy,
                    firefox_user_prefs={"network.proxy.allow_hijacking_localhost": True},
                )
            else:
                self.browser = await browser_launcher.launch(headless=self.headless, proxy=self.proxy)
        except Exception as exc:
            raise ScrapeError(f"Browser launch failed ({self.engine}); install it with `playwright install {self.engine}`") from exc
        pages = getattr(self.browser, "pages", [])
        self.page = pages[0] if pages else await self.browser.new_page()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self.browser is not None:
                await self.browser.close()
        finally:
            self.browser = self.page = None
            if self._pw is not None:
                await self._pw.stop()

    async def _wait_for_aio_answer(self, page, max_wait_ms: int = 8000) -> bool:
        """The AIO container shell can render before its answer streams in.
        Poll briefly for the answer subtree so snapshots do not race the JS.
        Returns True when an AIO container was present (filled or not)."""
        if await page.query_selector("#eKIzJc") is None:
            return False
        deadline = time.monotonic() + max_wait_ms / 1000
        while time.monotonic() < deadline:
            answer = await page.query_selector('#eKIzJc [data-subtree]')
            if answer and (await answer.inner_text()).strip():
                return True
            await page.wait_for_timeout(400)
        print("[scrape] AIO answer did not stream within poll window", file=sys.stderr)
        return True

    async def search(self, query: str, hl: str = "en", gl: str = "us") -> dict:
        """Scrape one query with this session, including organic results."""
        page = self.page
        url = f"https://www.google.com/search?q={quote_plus(query)}&hl={quote_plus(hl)}&gl={quote_plus(gl)}"
        result = await self._search_once(page, url, query, hl, gl)
        if "ai_overview" not in result and self._aio_shell_seen:
            # A container rendered but its answer never streamed in. One
            # reload usually gets Google to serve the full AIO.
            print("[scrape] AIO shell seen but empty; retrying the query once", file=sys.stderr)
            result = await self._search_once(page, url, query, hl, gl, poll_ms=12000)
        return result

    async def _search_once(self, page, url: str, query: str, hl: str, gl: str, poll_ms: int = 8000) -> dict:
        stage = "navigation"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            stage = "page rendering"
            await page.wait_for_timeout(min(1500, self.timeout_ms // 4))
            solver_used: dict = {}
            stage = "challenge handling"
            html = await _maybe_solve_challenge(page, page.url, await page.content(), self.timeout_ms, solver_used) or await page.content()
            stage = "AI Overview rendering"
            self._aio_shell_seen = await self._wait_for_aio_answer(page, poll_ms)
            more = page.get_by_role("button", name=re.compile("show more ai overview", re.I))
            if await more.count():
                try:
                    await more.first.click(timeout=1500)
                except Exception:
                    pass
                await page.wait_for_timeout(500)
            html = await page.content()
        except (ChallengeDetected, CaptchaSolverError):
            raise
        except Exception as exc:
            if stage == "navigation" and isinstance(exc, (TimeoutError, PlaywrightTimeoutError)):
                detail = f"Google search navigation timed out after {self.timeout_ms}ms"
                if self.proxy and self.engine == "chromium":
                    detail += "; Chromium with a proxy may hang. Try --proxy direct or GOOGLE_AIO_BROWSER=firefox"
            else:
                detail = f"Google search failed during {stage} ({type(exc).__name__})"
            raise ScrapeError(detail) from exc
        return parse_search_html(html, query, hl, gl)


async def scrape(query: str, hl: str = "en", gl: str = "us", timeout_ms: int = 30000,
                 proxy_mode: str | None = None) -> dict:
    if timeout_ms <= 0:
        raise ScrapeError("--timeout must be a positive number of milliseconds")
    async with GoogleAIOScraper(timeout_ms=timeout_ms, proxy_mode=proxy_mode) as scraper:
        return await scraper.search(query, hl, gl)


def _read_query_file(path: str) -> list[str]:
    queries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                queries.append(line)
    return queries


CSV_FIELDS = ["Query", "Search Query", "HL", "GL", "Type", "Position", "Title",
              "Link", "Snippet", "Source", "Reference Indexes", "Error Code", "Error Message"]


def _csv_rows(result: dict, query_id: str) -> list[dict]:
    params = result["search_parameters"]
    base = {"Query": query_id, "Search Query": params["q"], "HL": params["hl"], "GL": params["gl"]}
    rows = []
    overview = result.get("ai_overview", {})
    for position, block in enumerate(overview.get("text_blocks", []), 1):
        if block["type"] == "list":
            for item_index, item in enumerate(block["list"], 1):
                rows.append({**base, "Type": "aio_list_item", "Position": f"{position}.{item_index}",
                             "Snippet": item["snippet"],
                             "Reference Indexes": ",".join(map(str, item.get("reference_indexes", [])))})
        else:
            rows.append({**base, "Type": f"aio_{block['type']}", "Position": position,
                         "Snippet": block["snippet"],
                         "Reference Indexes": ",".join(map(str, block.get("reference_indexes", [])))})
    for reference in overview.get("references", []):
        rows.append({**base, "Type": "aio_reference", "Position": reference["index"] + 1,
                     "Title": reference["title"], "Link": reference["link"], "Source": reference["source"]})
    for item in result.get("organic_results", []):
        rows.append({**base, "Type": "organic", "Position": item["position"],
                     "Title": item["title"], "Link": item["link"], "Snippet": item["snippet"]})
    if "error" in result:
        rows.append({**base, "Type": "error", "Error Code": result["error"]["code"],
                     "Error Message": result["error"]["message"]})
    rows = rows or [{**base, "Type": "no_results"}]
    # Google text is untrusted; quoting a CSV field alone does not stop Excel
    # from evaluating a leading formula when the CSV is opened in a spreadsheet.
    for row in rows:
        for field, value in row.items():
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                row[field] = "'" + value
    return rows


def _error_result(code: str, message: str, query: str, hl: str, gl: str) -> dict:
    return {"search_parameters": {"engine": "google", "q": query, "hl": hl, "gl": gl},
            "error": {"code": code, "message": str(message)}}


async def _run_batch(queries: list[str], hl: str, gl: str, timeout_ms: int, delay: float, out,
                     proxy_mode: str | None = None) -> int:
    started = time.monotonic()
    ok = aio_found = errors = 0
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
    writer.writeheader()
    try:
        async with GoogleAIOScraper(timeout_ms=timeout_ms, proxy_mode=proxy_mode) as scraper:
            for i, query in enumerate(queries):
                try:
                    result = await scraper.search(query, hl, gl)
                except (ChallengeDetected, CaptchaSolverError) as exc:
                    code = "CAPTCHA_REQUIRED" if isinstance(exc, ChallengeDetected) else "CAPTCHA_SOLVER_ERROR"
                    result = _error_result(code, str(exc), query, hl, gl)
                    errors += 1
                except ScrapeError as exc:
                    result = _error_result("SCRAPE_ERROR", str(exc), query, hl, gl)
                    errors += 1
                else:
                    ok += 1
                    if "ai_overview" in result:
                        aio_found += 1
                writer.writerows(_csv_rows(result, f"P{i + 1:02d}"))
                out.flush()
                if i < len(queries) - 1 and delay > 0:
                    # Jitter so the pacing does not look machine-perfect.
                    await asyncio.sleep(delay + random.uniform(0, min(2.0, delay)))
    except (ScrapeError, ChallengeDetected, CaptchaSolverError) as exc:
        if ok + errors < len(queries):
            code = ("CAPTCHA_REQUIRED" if isinstance(exc, ChallengeDetected) else
                    "CAPTCHA_SOLVER_ERROR" if isinstance(exc, CaptchaSolverError) else "SCRAPE_ERROR")
            writer.writerows(_csv_rows(_error_result(code, str(exc), queries[ok + errors], hl, gl),
                                       f"P{ok + errors + 1:02d}"))
        else:
            print(f"[batch] browser cleanup failed: {exc}", file=sys.stderr)
        return 2
    elapsed = time.monotonic() - started
    print(f"[batch] {len(queries)} queries in {elapsed:.1f}s — ok: {ok} (with AIO: {aio_found}), errors: {errors}", file=sys.stderr)
    return 0 if errors == 0 else 1


def main() -> int:
    try:
        _load_env_file()
    except ScrapeError as exc:
        print(json.dumps({"error": {"code": "ENV_ERROR", "message": str(exc)}}))
        return 2
    parser = argparse.ArgumentParser(description="Scrape Google AI Overviews and organic results to CSV")
    parser.add_argument("query", nargs="?", help="Single query (omit when using --queries)")
    parser.add_argument("--queries", help="File with one query per line (# comments and blank lines ignored)")
    parser.add_argument("--out", help="CSV output file (default: stdout)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Seconds between batch queries, plus 0-2s jitter (default: 2.0)")
    parser.add_argument("--hl", default="en")
    parser.add_argument("--gl", default="us")
    parser.add_argument("--timeout", type=int, default=30000, help="Navigation timeout in milliseconds")
    parser.add_argument("--proxy", choices=("direct", "dataimpulse"),
                        help="Connection mode; overrides GOOGLE_AIO_PROXY (default: direct)")
    args = parser.parse_args()
    try:
        if args.queries:
            if args.query:
                parser.error("pass either a single query or --queries, not both")
            queries = _read_query_file(args.queries)
            if not queries:
                print(json.dumps({"error": {"code": "USAGE_ERROR", "message": f"no queries found in {args.queries}"}}))
                return 2
            if args.out:
                with open(args.out, "w", encoding="utf-8", newline="") as out:
                    return asyncio.run(_run_batch(queries, args.hl, args.gl, args.timeout, args.delay, out, proxy_mode=args.proxy))
            return asyncio.run(_run_batch(queries, args.hl, args.gl, args.timeout, args.delay, sys.stdout, proxy_mode=args.proxy))
        if not args.query:
            parser.error("a query is required (or pass --queries FILE for batch mode)")
        result = asyncio.run(scrape(args.query, args.hl, args.gl, args.timeout, proxy_mode=args.proxy))
        code = 0
    except (ScrapeError, ChallengeDetected, CaptchaSolverError) as exc:
        error_code = ("CAPTCHA_REQUIRED" if isinstance(exc, ChallengeDetected) else
                      "CAPTCHA_SOLVER_ERROR" if isinstance(exc, CaptchaSolverError) else "SCRAPE_ERROR")
        result = _error_result(error_code, str(exc), args.query, args.hl, args.gl)
        code = 2 if isinstance(exc, ScrapeError) else 3
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(_csv_rows(result, "P01"))
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(_csv_rows(result, "P01"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
