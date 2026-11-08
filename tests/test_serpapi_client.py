"""Offline tests for the bounded SerpApi request adapter.

All HTTP is patched through ``serpapi_client._request_json``. No test touches
the network or reads ``.env``.
"""

import io
import json
import math
import unittest
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

import serpapi_client as sc

# Fabricated responses. Keys/tokens are dummies, never real captures.
INLINE = {
    "search_metadata": {"id": "abc", "status": "Success"},
    "search_parameters": {"engine": "google", "q": "how tall", "hl": "en", "gl": "us"},
    "ai_overview": {"text_blocks": [{"type": "paragraph", "snippet": "Answer"}]},
    "organic_results": [{"position": 1, "link": "https://example.com/"}],
}
LAZY = {
    "search_metadata": {"id": "abc", "status": "Success"},
    "ai_overview": {
        "page_token": "TOKEN123",
        "serpapi_link": "https://serpapi.com/x?page_token=TOKEN123",
    },
    "organic_results": [{"position": 1, "link": "https://example.com/"}],
}
RESOLVED = {
    "search_metadata": {"id": "def", "status": "Success"},
    "ai_overview": {"text_blocks": [{"type": "paragraph", "snippet": "Full answer"}]},
}


class FetchSearchTests(unittest.TestCase):
    @patch.object(sc, "_request_json")
    def test_inline_overview_returns_ok_with_matching_parameters(self, request):
        request.return_value = dict(INLINE)

        result = sc.fetch_search("how tall", api_key="key123")

        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["error"])
        self.assertEqual(result["response"]["ai_overview"], INLINE["ai_overview"])
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], {
            "engine": "google",
            "q": "how tall",
            "hl": "en",
            "gl": "us",
            "api_key": "key123",
            "no_cache": True,
        })
        self.assertEqual(request.call_args.kwargs.get("timeout"), 30.0)

    @patch.object(sc, "_request_json")
    def test_absent_overview_is_still_ok(self, request):
        request.return_value = {"organic_results": [{"position": 1}]}

        result = sc.fetch_search("q", api_key="key123")

        self.assertEqual(result["status"], "ok")
        self.assertNotIn("ai_overview", result["response"])
        request.assert_called_once()

    @patch.object(sc, "_request_json")
    def test_inline_overview_with_page_token_does_not_follow_up(self, request):
        request.return_value = {
            "ai_overview": {"page_token": "T", "text_blocks": [{"type": "paragraph"}]},
        }

        result = sc.fetch_search("q", api_key="key123")

        self.assertEqual(result["status"], "ok")
        request.assert_called_once()

    @patch.object(sc, "_request_json")
    def test_lazy_overview_is_resolved_with_follow_up(self, request):
        request.side_effect = [dict(LAZY), dict(RESOLVED)]

        result = sc.fetch_search("q", api_key="key123")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(request.call_count, 2)
        initial_params, followup_params = (call.args[0] for call in request.call_args_list)
        self.assertEqual(initial_params["engine"], "google")
        self.assertEqual(followup_params["engine"], "google_ai_overview")
        self.assertEqual(followup_params["page_token"], "TOKEN123")
        self.assertEqual(followup_params["api_key"], "key123")
        self.assertEqual(result["response"]["ai_overview"], RESOLVED["ai_overview"])
        self.assertIn("initial_overview", result["evidence"])
        self.assertIn("follow_up", result["evidence"])

    @patch.object(sc, "_request_json")
    def test_failed_follow_up_returns_unresolved_and_preserves_initial(self, request):
        request.side_effect = [dict(LAZY), sc.SerpApiRequestError("boom https://x?api_key=key123")]

        result = sc.fetch_search("q", api_key="key123")

        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["response"]["organic_results"], LAZY["organic_results"])
        self.assertIn("page_token", result["response"]["ai_overview"])
        self.assertIsNotNone(result["error"])
        self.assertNotIn("key123", result["error"])
        self.assertIn("initial_overview", result["evidence"])

    @patch.object(sc, "_request_json")
    def test_empty_follow_up_is_unresolved(self, request):
        request.side_effect = [dict(LAZY), {"ai_overview": {"page_token": "T"}}]

        result = sc.fetch_search("q", api_key="key123")

        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(request.call_count, 2)

    @patch.object(sc, "_request_json")
    def test_api_error_field_returns_error(self, request):
        request.return_value = {"error": "Invalid API key"}

        result = sc.fetch_search("q", api_key="key123")

        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["response"])
        self.assertIn("Invalid API key", result["error"])
        request.assert_called_once()

    @patch.object(sc, "_request_json")
    def test_request_exception_returns_error(self, request):
        request.side_effect = sc.SerpApiRequestError("network down")

        result = sc.fetch_search("q", api_key="key123")

        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["response"])
        self.assertIn("network down", result["error"])

    @patch.object(sc, "_request_json")
    def test_non_object_response_returns_error(self, request):
        request.return_value = ["not", "an", "object"]

        result = sc.fetch_search("q", api_key="key123")

        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["response"])

    @patch.object(sc, "_request_json")
    def test_api_key_embedded_in_metadata_url_is_redacted(self, request):
        request.return_value = {
            "search_metadata": {
                "google_url": "https://www.google.com/search?q=q&api_key=key123",
                "json_endpoint": "https://serpapi.com/search.json?api_key=key123&q=q",
            },
        }

        result = sc.fetch_search("q", api_key="key123")

        dumped = repr(result["response"])
        self.assertNotIn("key123", dumped)
        self.assertIn("[REDACTED]", dumped)

    @patch.object(sc, "_request_json")
    def test_returned_page_token_is_redacted(self, request):
        request.return_value = {
            "ai_overview": {"page_token": "TOKEN123", "text_blocks": [{"type": "paragraph"}]},
        }

        result = sc.fetch_search("q", api_key="key123")

        self.assertNotEqual(result["response"]["ai_overview"]["page_token"], "TOKEN123")


class TokenRedactionTests(unittest.TestCase):
    def test_inline_token_cannot_leak_elsewhere_in_response_or_evidence(self):
        payload = {'ai_overview': {'page_token': 'TOKENxyz', 'text_blocks': [{'type': 'paragraph'}],
                                  'note': 'token TOKENxyz'}, 'note': 'TOKENxyz'}
        with patch.object(sc, '_request_json', return_value=payload):
            result = sc.fetch_search('q', api_key='fake')
        self.assertNotIn('TOKENxyz', json.dumps(result))


class TransportTests(unittest.TestCase):
    def test_transport_uses_fixed_endpoint_and_encodes_parameters(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(INLINE).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(sc.urllib.request, 'build_opener', return_value=opener):
            result = sc._request_json({'q': 'one & two', 'api_key': 'fake'}, timeout=7)
        request = opener.open.call_args.args[0]
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(url.scheme, 'https')
        self.assertEqual(url.netloc, 'serpapi.com')
        self.assertEqual(url.path, '/search.json')
        self.assertEqual(urllib.parse.parse_qs(url.query)['q'], ['one & two'])
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 7)
        self.assertEqual(result, INLINE)

    def test_redirect_handler_refuses_following_urls(self):
        handler = sc._NoRedirect()
        request = sc.urllib.request.Request(sc.SERPAPI_ENDPOINT)
        self.assertIsNone(handler.redirect_request(request, None, 302, 'Found', {}, 'https://elsewhere.example/'))

    def test_transport_errors_are_generic_and_invalid_json_is_rejected(self):
        for error in (urllib.error.HTTPError(sc.SERPAPI_ENDPOINT, 401, 'error', {}, io.BytesIO()),
                      urllib.error.URLError('network down'), TimeoutError()):
            with self.subTest(error=type(error)), patch.object(sc.urllib.request, 'build_opener') as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(sc.SerpApiRequestError):
                    sc._request_json({}, timeout=1)
        with patch.object(sc.urllib.request, 'build_opener') as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value = b'not json'
            with self.assertRaisesRegex(sc.SerpApiRequestError, 'invalid JSON'):
                sc._request_json({}, timeout=1)

    def test_wrong_overview_shape_is_not_successful_absence(self):
        with patch.object(sc, '_request_json', return_value={'ai_overview': {'text_blocks': 'not an array'}}):
            self.assertEqual(sc.fetch_search('q', api_key='fake')['status'], 'error')

    def test_follow_up_error_cannot_be_hidden_by_text_blocks(self):
        follow = {'error': 'follow up failed', 'ai_overview': RESOLVED['ai_overview']}
        with patch.object(sc, '_request_json', side_effect=[LAZY, follow]):
            self.assertEqual(sc.fetch_search('q', api_key='fake')['status'], 'unresolved')

    def test_api_error_payload_is_retained_as_sanitized_evidence(self):
        payload = {'error': 'API failed', 'search_metadata': {'id': 'abc'}, 'api_key': 'fake-key-123'}
        with patch.object(sc, '_request_json', return_value=payload):
            result = sc.fetch_search('q', api_key='fake-key-123')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['evidence']['initial_response']['search_metadata']['id'], 'abc')
        self.assertNotIn('fake-key-123', json.dumps(result))

    def test_http_error_json_payload_is_preserved(self):
        error = urllib.error.HTTPError(sc.SERPAPI_ENDPOINT, 401, 'error', {},
                                      io.BytesIO(b'{"error": "Invalid API key", "request_id": "abc"}'))
        with patch.object(sc.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = error
            result = sc.fetch_search('q', api_key='fake')
        self.assertEqual(result['evidence']['initial_response']['request_id'], 'abc')

    def test_empty_or_invalid_first_page_is_an_error(self):
        for invalid in ({}, {'organic_results': {}}, {'ai_overview': 'wrong'}, {'search_parameters': []}):
            with self.subTest(invalid=invalid), patch.object(sc, '_request_json', return_value=invalid):
                self.assertEqual(sc.fetch_search('q', api_key='fake')['status'], 'error')


class ValidationTests(unittest.TestCase):
    @patch.object(sc, "_request_json")
    def test_empty_api_key_raises_before_request(self, request):
        with self.assertRaises(ValueError):
            sc.fetch_search("q", api_key="   ")
        request.assert_not_called()

    @patch.object(sc, "_request_json")
    def test_empty_query_raises_before_request(self, request):
        with self.assertRaises(ValueError):
            sc.fetch_search("", api_key="key123")
        request.assert_not_called()

    @patch.object(sc, "_request_json")
    def test_non_finite_or_non_positive_timeout_raises(self, request):
        for timeout in (0, -1, math.inf, math.nan, True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                sc.fetch_search("q", api_key="key123", timeout=timeout)
        request.assert_not_called()

    @patch.object(sc, "_request_json")
    def test_blank_locale_raises_before_request(self, request):
        for kwargs in ({"hl": " "}, {"gl": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                sc.fetch_search("q", api_key="key123", **kwargs)
        request.assert_not_called()


class SanitizeTests(unittest.TestCase):
    def test_redacts_sensitive_keys_and_secret_values(self):
        payload = {
            "api_key": "key123",
            "page_token": "TOKEN123",
            "nested": {"password": "pw", "safe": "key123 in text"},
            "list": [{"access_token": "x"}],
        }

        cleaned = sc.sanitize(payload, secrets=("key123",))

        self.assertEqual(cleaned["api_key"], "[REDACTED]")
        self.assertEqual(cleaned["page_token"], "[REDACTED]")
        self.assertEqual(cleaned["nested"]["password"], "[REDACTED]")
        self.assertNotIn("key123", cleaned["nested"]["safe"])
        self.assertEqual(cleaned["list"][0]["access_token"], "[REDACTED]")

    def test_redacts_url_credential_params(self):
        text = "see https://serpapi.com/search.json?engine=google&api_key=SECRET123&q=x"

        cleaned = sc.sanitize(text)

        self.assertNotIn("SECRET123", cleaned)
        self.assertIn("api_key=[REDACTED]", cleaned)
        self.assertIn("engine=google", cleaned)

    def test_redacts_leading_credential_param(self):
        cleaned = sc.sanitize("api_key=SECRET123&q=x")
        self.assertNotIn("SECRET123", cleaned)

    def test_encoded_secret_and_token_values_and_keys_are_redacted(self):
        text = {'endpoint': 'https://x/?api_key=a%2Bb', 'note': 'value a%2Bb',
                'weird': {'page_token': 'TOKENxyz', 'note': 'uses TOKENxyz'}, 'a+b': 'safe'}
        cleaned = sc.sanitize(text, secrets=('a+b',))
        self.assertNotIn('a%2Bb', json.dumps(cleaned))
        self.assertNotIn('TOKENxyz', json.dumps(cleaned))
        self.assertNotIn('a+b', json.dumps(cleaned))

    def test_preserves_unrelated_structure_and_values(self):
        payload = {"a": [1, True, None, "text"], "b": {"c": 1.5}}

        self.assertEqual(sc.sanitize(payload), payload)


if __name__ == "__main__":
    unittest.main()
