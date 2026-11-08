"""Google AI Overview scraper with conservative, SerpApi-shaped JSON output."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Comment, Tag
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


class ScrapeError(RuntimeError):
    """The page could not be interpreted safely as a Google results page."""


_ENV_LOADED = False


def _load_env_file() -> None:
    """Populate os.environ from a .env file next to this module.

    Exported variables win over .env values. Duplicate file entries use the
    last value, so replacing a credential does not retain an earlier entry.
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
    file_values = {}
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
        if key:
            file_values[key] = value
    for key, value in file_values.items():
        os.environ.setdefault(key, value)


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
        item = {"position": len(results) + 1, "title": title, "link": link,
                "snippet": _visible_text(snippet_node) if snippet_node else ""}
        for field, selector in (("source", ".VuuXrf"), ("displayed_link", "cite"),
                                ("date", "time, [data-date], .LEwnzc"),
                                ("author", '[rel="author"], [itemprop="author"]')):
            node = card.select_one(selector)
            if node is not None and _visible_text(node):
                item[field] = _visible_text(node)
        if snippet_node:
            highlights = [_visible_text(node) for node in snippet_node.select("em") if _visible_text(node)]
            if highlights:
                item["snippet_highlighted_words"] = highlights
        sitelinks = []
        for node in card.select('[data-sitelinks] a[href], .sitelinks a[href], .HiHjCd a[href]'):
            destination = _organic_url(node["href"])
            label = _visible_text(node)
            if destination and label and node is not anchor:
                sitelinks.append({"title": label, "link": destination})
        if sitelinks:
            item["sitelinks"] = {"inline": sitelinks}
        extensions = [_visible_text(node) for node in card.select('[data-rich-snippet], [itemprop="aggregateRating"]')
                      if _visible_text(node)]
        if extensions:
            item["rich_snippet"] = {"top": {"extensions": extensions}}
        results.append(item)
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


def _text_payload(node: Tag, by_href: dict[str, int]) -> dict:
    payload = {"snippet": _visible_text(node)}
    indexes = _reference_indexes(node, by_href)
    if indexes:
        payload["reference_indexes"] = indexes
    links = []
    for anchor in node.select("a[href]"):
        link = _safe_reference_url(anchor["href"])
        if link:
            entry = {"link": link}
            text = _visible_text(anchor)
            if text:
                entry = {"text": text, **entry}
            if entry not in links:
                links.append(entry)
    if links:
        payload["snippet_links"] = links
    return payload


def _is_hidden(node: Tag) -> bool:
    # Browser snapshots annotate computed CSS visibility with `hidden` as well.
    return any(parent.has_attr("hidden") or parent.get("aria-hidden") == "true"
               or re.search(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important\s*)?(?:;|$)",
                            parent.get("style", ""), re.I)
               for parent in (node, *node.parents))


def _content_copy(node: Tag) -> Tag:
    clone = BeautifulSoup(str(node), "html.parser")
    for hidden in list(clone.select('[hidden], [aria-hidden="true"], [data-ignore-copy], script, style, button, [role="button"], [data-src-id], [jsname="lGsj1"], [jsname="L1sC6e"]')):
        if hidden.parent is not None:
            hidden.decompose()
    for styled in list(clone.select('[style]')):
        if styled.parent is not None and _is_hidden(styled):
            styled.decompose()
    return clone


def _list_block(list_node: Tag, by_href: dict[str, int]) -> dict | None:
    items = []
    # Google can wrap each <li> in a <div>. Select items owned by this
    # list, rather than only immediate children or items in nested lists.
    for item in list_node.find_all("li"):
        if item.find_parent(["ul", "ol"]) is not list_node:
            continue
        if item.has_attr("hidden") or item.get("aria-hidden") == "true":
            continue
        clone = _content_copy(item)
        for nested in reversed(clone.find_all(["ul", "ol"])):
            nested.decompose()
        block_item = _text_payload(clone, by_href)
        nested_items = []
        for nested in item.find_all(["ul", "ol"]):
            if nested.find_parent("li") is item:
                block = _list_block(nested, by_href)
                if block:
                    nested_items.extend(block["list"])
        if nested_items:
            block_item["list"] = nested_items
        if block_item["snippet"] or nested_items:
            items.append(block_item)
    return {"type": "list", "list": items} if items else None


def _table_block(table_node: Tag, by_href: dict[str, int]) -> dict | None:
    detailed = []
    for row in table_node.find_all("tr"):
        if (row.find_parent("table") is not table_node
                or row.has_attr("hidden") or row.get("aria-hidden") == "true"):
            continue
        cells = []
        for cell in row.find_all(["th", "td"], recursive=False):
            if cell.has_attr("hidden") or cell.get("aria-hidden") == "true":
                continue
            payload = _text_payload(_content_copy(cell), by_href)
            for attr in ("rowspan", "colspan"):
                if cell.has_attr(attr):
                    payload[attr] = cell[attr]
            cells.append(payload)
        if cells:
            detailed.append(cells)
    if not detailed:
        return None
    block = {"type": "table", "table": [[cell["snippet"] for cell in row] for row in detailed],
             "detailed": detailed}
    caption = table_node.find("caption", recursive=False)
    if caption and _visible_text(caption):
        block.update(_text_payload(_content_copy(caption), by_href))
    return block


def _answer_blocks(answer: Tag, by_href: dict[str, int]) -> list[dict]:
    """Traverse wrappers and mixed text once, preserving semantic boundaries."""
    blocks = []
    followup_prefix = "If you'd like, I can share information about:"

    def walk(node):
        pending = []

        def flush():
            if pending:
                fragment = BeautifulSoup("".join(pending), "html.parser")
                payload = _text_payload(fragment, by_href)
                if payload["snippet"]:
                    blocks.append({"type": "paragraph", **payload})
                pending.clear()

        for child in node.children:
            if isinstance(child, Comment):
                continue
            if not isinstance(child, Tag):
                pending.append(str(child))
                continue
            if (child.has_attr("hidden") or child.get("aria-hidden") == "true"
                    or child.name in ("script", "style", "button") or child.get("role") == "button"
                    or child.has_attr("data-src-id") or child.get("jsname") in ("lGsj1", "L1sC6e")
                    or _clean(child.get_text(" ", strip=True)).startswith(followup_prefix)):
                continue
            heading = child.name in ("h1", "h2", "h3", "h4", "h5", "h6") or child.get("role") == "heading"
            if heading or child.name == "p":
                flush()
                payload = _text_payload(_content_copy(child), by_href)
                if payload["snippet"]:
                    blocks.append({"type": "heading" if heading else "paragraph", **payload})
            elif child.name in ("ul", "ol", "table"):
                flush()
                block = _table_block(child, by_href) if child.name == "table" else _list_block(child, by_href)
                if block:
                    blocks.append(block)
            elif (child.name in ("div", "section", "article")
                  or child.select_one('p, ul, ol, table, h1, h2, h3, h4, h5, h6, [role="heading"]')):
                flush()
                walk(child)
            else:
                pending.append(str(_content_copy(child)))
        flush()

    walk(_content_copy(answer))
    return blocks


def _visible_text(node: Tag) -> str:
    if _is_hidden(node):
        return ""
    text = _clean(_content_copy(node).get_text(" ", strip=True))
    return "" if NOISE.fullmatch(text) else text


def _aio_state(container: Tag | None) -> tuple[str, Tag | None, str | None]:
    """One completion classifier shared by polling and saved-HTML parsing."""
    if container is None:
        return "absent", None, None
    if _is_hidden(container):
        return "unresolved", None, "render_incomplete"
    if any(not _is_hidden(node) for node in container.select(
            '[role="progressbar"][aria-label="Generating"], [aria-busy="true"]')):
        return "unresolved", None, "generation_in_progress"
    answer = (container.select_one('[data-subtree="aimc"] [jsname="KFl8ub"]')
              or container.select_one('[data-subtree="aimfl,mfl"]')
              or container.select_one('[data-subtree="aimc"]'))
    if answer is not None and _visible_text(answer):
        return "present", answer, None
    notices = [_clean(node.get_text(" ", strip=True)) for node in container.select(
        '[jsname="lGsj1"], [jsname="L1sC6e"]') if not _is_hidden(node)]
    notices.append(_clean(_content_copy(container).get_text(" ", strip=True)))
    for notice in notices:
        if NOISE.fullmatch(notice):
            if notice.lower().startswith("can't generate"):
                return "unresolved", None, "generation_failed"
            return "unavailable", None, None
    return "unresolved", None, "render_incomplete"


async def _rendered_html(page, *, aio_only: bool = False) -> str:
    """Serialize a clone, recording computed AIO visibility without changing the page.

    This preserves stylesheet-based visibility for offline parsing/evidence too.
    """
    return await page.evaluate("""(aioOnly) => {
        const root = aioOnly ? document.querySelector('#eKIzJc') : document.documentElement;
        if (!root) return '';
        const clone = root.cloneNode(true);
        const elements = r => r.id === 'eKIzJc'
            ? [r, ...r.querySelectorAll('*')]
            : [...r.querySelectorAll('#eKIzJc, #eKIzJc *')];
        const originals = elements(root), copies = elements(clone);
        originals.forEach((element, i) => {
            const style = getComputedStyle(element);
            if (style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility))
                copies[i].setAttribute('hidden', '');
            if (element.id === 'eKIzJc') {
                for (let parent = element.parentElement; parent; parent = parent.parentElement) {
                    const ancestor = getComputedStyle(parent);
                    if (parent.hidden || parent.getAttribute('aria-hidden') === 'true'
                        || ancestor.display === 'none' || ancestor.visibility === 'hidden')
                        copies[i].setAttribute('hidden', '');
                }
            }
        });
        return clone.outerHTML;
    }""", aio_only)


def parse_search_html(html: str, query: str, hl: str, gl: str) -> dict:
    """Return search data with optional AIO and ordered organic results."""
    soup = BeautifulSoup(html, "html.parser")
    sample = soup.get_text(" ", strip=True)[:5000].lower()
    if "/sorry/" in sample or "unusual traffic" in sample or "recaptcha" in sample:
        raise ScrapeError("Google blocked the request")

    results_page = soup.select_one("#search, #rso")
    if results_page is None:
        raise ScrapeError("Google returned an incomplete page, not a search results page")

    response = {"search_parameters": {"engine": "google", "q": query, "hl": hl, "gl": gl},
                "extraction_metadata": {"aio_status": "absent"}}
    organic = _organic_results(results_page)
    response["extraction_metadata"]["organic_count"] = len(organic)
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
    status, answer, reason = _aio_state(container)
    if status != "present":
        response["extraction_metadata"]["aio_status"] = status
        if reason:
            response["extraction_metadata"]["aio_reason"] = reason
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
        reference = {"index": len(cards), "title": title, "link": link,
                     "source": _source_name(title, aria, link)}
        snippet_node = card.select_one('.VwiC3b, .IsZvec, .yXK7lf, [data-snippet]')
        if snippet_node and _visible_text(snippet_node):
            reference["snippet"] = _visible_text(snippet_node)
        cards.append(reference)

    blocks = _answer_blocks(answer, by_href)
    response["extraction_metadata"]["aio_status"] = "present" if blocks else "empty"
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
            # Match the actual destination, not /search inside a /sorry
            # continue= parameter. The old glob accepted rejected tokens.
            await page.wait_for_url(
                lambda url: urlparse(url).hostname == "www.google.com"
                and urlparse(url).path == "/search",
                timeout=timeout_ms,
            )
        except Exception:
            last_stay_url = page.url
            continue  # retry with a fresh token

        await page.wait_for_timeout(min(1500, timeout_ms // 4))
        html = await page.content()
        if is_challenge_page(html):
            last_stay_url = page.url
            continue
        print(f"[captcha] landed on {urlparse(page.url).path}", file=sys.stderr)
        return html

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

    async def _wait_for_aio_answer(self, page, max_wait_ms: int = 20000) -> bool:
        """Wait for a real answer or visible refusal, never stable loading text.

        Observe delayed containers for up to 8s; once seen, use the full budget.
        Unfinished shells remain unresolved for the bounded reload/retry policy.
        """
        start = time.monotonic()
        deadline = start + max_wait_ms / 1000
        seen = False
        status = "absent"
        previous = None
        unchanged_since = None
        while time.monotonic() < deadline:
            soup = BeautifulSoup(await _rendered_html(page, aio_only=True), "html.parser")
            container = soup.select_one("#eKIzJc")
            seen = seen or container is not None
            status, answer, reason = _aio_state(container)
            now = time.monotonic()
            if status == "absent":
                if not seen and now - start >= min(8.0, max_wait_ms / 1000):
                    return False
            elif status == "unavailable" or reason == "generation_failed":
                return True
            if status == "present":
                # Include semantic structure and citations, not only plain text.
                key = str((_answer_blocks(answer, {}),
                           [(card.get_text(" ", strip=True), [a.get("href") for a in card.select('a[href]')])
                            for card in container.select('[data-src-id]') if not _is_hidden(card)]))
                if key != previous:
                    unchanged_since = now
                elif unchanged_since is not None and now - unchanged_since >= 2.0:
                    return True
                previous = key
            else:
                previous = None
                unchanged_since = None
            await page.wait_for_timeout(400)
        if seen and status == "absent":
            raise ScrapeError("AI Overview container disappeared before rendering completed")
        if previous:
            raise ScrapeError("AI Overview answer did not stabilize within the rendering window")
        return seen

    async def search(self, query: str, hl: str = "en", gl: str = "us") -> dict:
        """Scrape one query with this session, including organic results."""
        page = self.page
        url = f"https://www.google.com/search?q={quote_plus(query)}&hl={quote_plus(hl)}&gl={quote_plus(gl)}"
        result = await self._search_once(page, url, query, hl, gl)
        if "ai_overview" not in result and self._aio_shell_seen:
            # A container rendered but its answer never streamed in. One
            # reload usually gets Google to serve the full AIO.
            print("[scrape] AIO shell seen but empty; retrying the query once", file=sys.stderr)
            result = await self._search_once(page, url, query, hl, gl, poll_ms=30000)
        return result

    async def _search_once(self, page, url: str, query: str, hl: str, gl: str, poll_ms: int = 20000) -> dict:
        requested_at = datetime.now(timezone.utc).isoformat()
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
            html = await _rendered_html(page)
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
        result = parse_search_html(html, query, hl, gl)
        result["search_metadata"] = {"created_at": requested_at}
        result["search_parameters"].update({"device": "desktop", "google_domain": "google.com"})
        return result


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


def _error_result(code: str, message: str, query: str, hl: str, gl: str) -> dict:
    return {"search_parameters": {"engine": "google", "q": query, "hl": hl, "gl": gl},
            "error": {"code": code, "message": str(message)}}


def main(argv=None) -> int:
    """Use the same JSON capture controller as the installed command."""
    from run_supabase_batch import main as capture_main
    return capture_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
