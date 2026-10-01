import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aio_scraper as a


class ProxySelectionTests(unittest.TestCase):
    credentials = {
        "DATAIMPULSE_PROXY_HOST": "proxy.example",
        "DATAIMPULSE_PROXY_PORT": "823",
        "DATAIMPULSE_PROXY_USER": "user",
        "DATAIMPULSE_PROXY_PASSWORD": "password",
    }

    def test_saved_credentials_do_not_enable_proxy_by_default(self):
        with patch.dict("os.environ", self.credentials, clear=True):
            self.assertIsNone(a.GoogleAIOScraper().proxy)

    def test_blank_environment_mode_defaults_to_direct(self):
        for mode in ("", "   "):
            with self.subTest(mode=mode), patch.dict("os.environ", {**self.credentials, "GOOGLE_AIO_PROXY": mode}, clear=True):
                self.assertIsNone(a.GoogleAIOScraper().proxy)

    def test_direct_mode_ignores_even_invalid_proxy_credentials(self):
        env = {"GOOGLE_AIO_PROXY": "dataimpulse",
               "DATAIMPULSE_PROXY_URL": "http://user:password@proxy.example:bad"}
        with patch.dict("os.environ", env, clear=True):
            self.assertIsNone(a.GoogleAIOScraper(proxy_mode="direct").proxy)

    def test_environment_can_explicitly_enable_dataimpulse(self):
        with patch.dict("os.environ", {**self.credentials, "GOOGLE_AIO_PROXY": "dataimpulse"}, clear=True):
            self.assertEqual(a.GoogleAIOScraper().proxy, {
                "server": "http://proxy.example:823", "username": "user", "password": "password"})

    def test_explicit_proxy_dictionary_still_works(self):
        config = {"server": "http://custom.example:9000"}
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(a.GoogleAIOScraper(proxy=config).proxy, config)

    def test_requested_dataimpulse_without_credentials_fails(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(a.ScrapeError, "DataImpulse.*credentials"):
                a.GoogleAIOScraper(proxy_mode="dataimpulse")

    def test_invalid_environment_mode_fails_before_launch(self):
        with patch.dict("os.environ", {"GOOGLE_AIO_PROXY": "typo"}, clear=True):
            with self.assertRaisesRegex(a.ScrapeError, "GOOGLE_AIO_PROXY"):
                a.GoogleAIOScraper()

    def run_cli(self, args, env):
        selected = []

        async def enter(scraper):
            selected.append(scraper.proxy)
            return scraper

        async def exit_context(scraper, *args):
            pass

        async def search(scraper, query, hl, gl):
            return {"search_parameters": {"q": query, "hl": hl, "gl": gl}}

        output = io.StringIO()
        with patch.dict("os.environ", env, clear=True), \
                patch.object(a, "_load_env_file"), \
                patch.object(a.GoogleAIOScraper, "__aenter__", enter), \
                patch.object(a.GoogleAIOScraper, "__aexit__", exit_context), \
                patch.object(a.GoogleAIOScraper, "search", search), \
                patch("sys.argv", ["aio_scraper", *args]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = a.main()
        return status, selected, list(csv.DictReader(io.StringIO(output.getvalue())))

    def test_cli_selection_overrides_environment_for_single_and_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            queries = Path(directory) / "queries.txt"
            queries.write_text("first\nsecond\n", encoding="utf-8")
            for query_args in (["first"], ["--queries", str(queries), "--delay", "0"]):
                for mode, env_mode in (("direct", "dataimpulse"), ("dataimpulse", "direct")):
                    with self.subTest(query_args=query_args, mode=mode):
                        status, selected, rows = self.run_cli(
                            [*query_args, "--proxy", mode], {**self.credentials, "GOOGLE_AIO_PROXY": env_mode})
                        self.assertEqual(status, 0)
                        self.assertEqual(bool(selected[0]), mode == "dataimpulse")
                        self.assertEqual([r["Type"] for r in rows], ["no_results"] * len(rows))
                        self.assertEqual(len(rows), 1 if query_args[0] == "first" else 2)

    def test_cli_default_is_direct_despite_saved_credentials(self):
        status, selected, rows = self.run_cli(["first"], self.credentials)
        self.assertEqual(status, 0)
        self.assertEqual(selected, [None])
        self.assertEqual(rows[0]["Type"], "no_results")

    def test_missing_credentials_cli_outputs_safe_error_without_launch(self):
        status, selected, rows = self.run_cli(["first", "--proxy", "dataimpulse"], {})
        self.assertEqual(status, 2)
        self.assertEqual(selected, [])
        self.assertEqual(rows[0]["Error Code"], "SCRAPE_ERROR")
        self.assertIn("credentials", rows[0]["Error Message"])
