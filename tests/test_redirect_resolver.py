import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import redirect_resolver as rr


class RedirectResponse:
    """Minimal APIResponse stand-in: a status plus a Location header."""

    def __init__(self, status, location=None):
        self.status = status
        self.headers = {"location": location} if location else {}


class RedirectRequestContext:
    """One recorded request per link, replaying canned per-link outcomes."""

    def __init__(self, outcomes):
        self.outcomes = dict(outcomes)
        self.requests = []

    async def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def resolution_page(request_context, url="about:blank"):
    page = SimpleNamespace(url=url)
    page.context = SimpleNamespace(request=request_context)
    return page


ENVELOPE = {
    "run_id": "00000000-0000-0000-0000-000000000009",
    "captured_at": "2026-01-01T00:00:00Z",
    "planned_query_count": 2,
    "results": [
        {"query_id": "P01", "result": {
            "search_parameters": {"q": "one"},
            "organic_results": [{"position": 1, "title": "One", "link": "https://www.google.com/goto?url=CAESa", "snippet": ""}],
            "ai_overview": {"references": [
                {"index": 0, "title": "Shared", "link": "https://www.google.com/url?sa=i&url=CAESshared"},
                {"index": 1, "title": "First", "link": "https://www.google.com/url?sa=i&url=CAESb"}]}}},
        {"query_id": "P02", "result": {
            "search_parameters": {"q": "two"},
            "organic_results": [{"position": 1, "title": "Two", "link": "https://www.google.com/goto?url=CAESa"}],
            "ai_overview": {"references": [
                {"index": 0, "title": "Shared", "link": "https://www.google.com/url?sa=i&url=CAESshared"}]}}},
    ]
}


def destinations():
    return {
        "https://www.google.com/goto?url=CAESa": "https://a.example/article?x=1",
        "https://www.google.com/url?sa=i&url=CAESshared": "https://shared.example/fact",
        "https://www.google.com/url?sa=i&url=CAESb": "https://b.example/page",
    }


class CaptureShapeTests(unittest.TestCase):
    def test_envelope_list_and_single_response_shapes_all_work(self):
        single = ENVELOPE["results"][0]["result"]
        for capture, expected in ((ENVELOPE, 2), (ENVELOPE["results"][0]["result"], 1),
                                  ([single, single], 2)):
            with self.subTest(expected=expected):
                self.assertEqual(len(rr.capture_results(capture)), expected)

    def test_invalid_shapes_are_rejected(self):
        for capture in (None, {}, {"results": []}, {"results": [None]}, {"unexpected": True}, [None]):
            with self.subTest(capture=capture):
                with self.assertRaises(ValueError):
                    rr.capture_results(capture)


class ResolutionCacheTests(unittest.TestCase):
    def test_missing_cache_starts_empty_and_writes_roundtrip_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            self.assertEqual(rr.read_resolutions(path), {})
            rr.write_resolutions(path, {"https://www.google.com/goto?url=x": "https://d.example/a"})
            self.assertEqual(rr.read_resolutions(path), {"https://www.google.com/goto?url=x": "https://d.example/a"})
            rr.write_resolutions(path, {"https://www.google.com/goto?url=y": "https://d.example/b"})
            self.assertEqual(len(rr.read_resolutions(path)), 1)

    def test_corrupt_caches_are_rejected_not_guesses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(json.dumps([{"link": "https://x.example"}]))
            with self.assertRaises(ValueError):
                rr.read_resolutions(path)


class ResolveCaptureLinksTests(unittest.IsolatedAsyncioTestCase):
    async def test_unique_links_resolve_once_and_metadata_is_per_result(self):
        capture = json.loads(json.dumps(ENVELOPE))
        context = RedirectRequestContext({
            link: RedirectResponse(302, destination)
            for link, destination in destinations().items()})
        summary = await rr.resolve_capture_links(capture, resolution_page(context), settle_seconds=0.0)
        self.assertEqual(len(context.requests), 3)  # one request per unique link, not per occurrence
        first, second = (entry["result"] for entry in capture["results"])
        self.assertEqual(first["organic_results"][0]["link"], "https://a.example/article?x=1")
        self.assertEqual(first["organic_results"][0]["redirect_link"], "https://www.google.com/goto?url=CAESa")
        self.assertEqual(first["ai_overview"]["references"][0]["link"], "https://shared.example/fact")
        self.assertEqual(second["organic_results"][0]["link"], "https://a.example/article?x=1")
        self.assertEqual(first["extraction_metadata"]["redirect_resolution"], {"attempted": 3, "resolved": 3})
        self.assertEqual(second["extraction_metadata"]["redirect_resolution"], {"attempted": 2, "resolved": 2})
        self.assertEqual(summary, {"results": 2, "unique_links": 3, "resolved": 3, "unresolved": 0})

    async def test_cached_links_skip_requests_and_checkpoints_persist(self):
        capture = json.loads(json.dumps(ENVELOPE))
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cache.json"
            rr.write_resolutions(cache_path, dict(list(destinations().items())[:1]))
            context = RedirectRequestContext({
                link: RedirectResponse(302, destination)
                for link, destination in destinations().items()})
            summary = await rr.resolve_capture_links(
                capture, resolution_page(context), cache_path=cache_path, settle_seconds=0.0)
            self.assertEqual(len(context.requests), 2)  # one link already cached
            self.assertEqual(summary["resolved"], 3)
            self.assertEqual(len(rr.read_resolutions(cache_path)), 3)

    async def test_interstitial_failures_stay_opaque_and_counted(self):
        capture = json.loads(json.dumps(ENVELOPE))
        context = RedirectRequestContext({
            link: RedirectResponse(200) for link in destinations()})
        summary = await rr.resolve_capture_links(capture, resolution_page(context), settle_seconds=0.0)
        first = capture["results"][0]["result"]
        self.assertEqual(first["organic_results"][0]["link"], "https://www.google.com/goto?url=CAESa")
        self.assertNotIn("redirect_link", first["organic_results"][0])
        self.assertEqual(first["extraction_metadata"]["redirect_resolution"], {"attempted": 3, "resolved": 0})
        self.assertEqual(summary, {"results": 2, "unique_links": 3, "resolved": 0, "unresolved": 3})

    async def test_sweep_failure_of_every_link_never_raises(self):
        capture = json.loads(json.dumps(ENVELOPE))
        context = RedirectRequestContext({
            link: TimeoutError("boom") for link in destinations()})
        summary = await rr.resolve_capture_links(capture, resolution_page(context), settle_seconds=0.0)
        self.assertEqual(summary["unresolved"], 3)
        self.assertEqual(capture["results"][0]["result"]["organic_results"][0]["link"],
                         "https://www.google.com/goto?url=CAESa")


if __name__ == "__main__":
    unittest.main()
