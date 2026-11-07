import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from scripts import fetch_locations as scraper


URL = f"{scraper.BASE}/oxford-summertown"
DOCUMENT = {
    "id": "3481", "name": "PizzaExpress", "slug": "oxford-summertown",
    "meta": {"entityType": {"id": "restaurant"}},
    "address": {"line1": "232-236 Banbury Road"},
    "yextDisplayCoordinate": {"latitude": 51.77, "longitude": -1.26},
    "hours": {"monday": {"openIntervals": [{"start": "11:30", "end": "21:30"}]}},
    "__": {"layout": "rendering configuration"}, "_env": {}, "siteId": 1,
}


def page(document):
    props = quote(json.dumps({"document": document}))
    return ('<script type="module">const unrelated = 1;\n'
            f'pageProps: JSON.parse(decodeURIComponent("{props}"))'
            '</script>')


def sitemap(kind, urls):
    child = "sitemap" if kind == "sitemapindex" else "url"
    return (f'<{kind} xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<{child}><loc>{url}</loc></{child}>" for url in urls)
            + f"</{kind}>")


class ScraperTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch("time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_sitemap_index_includes_all_restaurants_excludes_directories(self):
        sources = {
            scraper.SITEMAP: sitemap("sitemapindex", [f"{scraper.BASE}/sitemap1.xml"]),
            f"{scraper.BASE}/sitemap1.xml": sitemap("urlset", [
                URL, f"{scraper.BASE}/restaurants", URL,
                f"{scraper.BASE}/restaurants/oxfordshire"]),
        }
        with patch.object(scraper, "download", side_effect=sources.__getitem__):
            self.assertEqual(scraper.location_urls(), [URL])

    def test_extracts_public_document_without_rendering_metadata(self):
        with patch.object(scraper, "download", return_value=page(DOCUMENT)):
            record = scraper.restaurant(URL)
        self.assertEqual(record["id"], "3481")
        self.assertEqual(record["hours"], DOCUMENT["hours"])
        self.assertFalse({"__", "_env", "meta", "siteId"} & record.keys())

    def test_rejects_error_pages_wrong_entities_and_bad_coordinates(self):
        invalid = []
        for field, value in [("id", ""), ("address", {}),
                             ("yextDisplayCoordinate", {"latitude": 91, "longitude": 0}),
                             ("meta", {"entityType": {"id": "dm_region"}})]:
            document = copy.deepcopy(DOCUMENT)
            document[field] = value
            invalid.append(page(document))
        for body in ["<html>Service unavailable</html>", *invalid]:
            with self.subTest(body=body[:60]), patch.object(scraper, "download", return_value=body):
                with self.assertRaises(ValueError):
                    scraper.restaurant(URL)

    def test_partial_fetch_failure_preserves_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            output.write_text("previous snapshot")
            def fetch(url):
                if url.endswith("-2"):
                    raise RuntimeError("HTTP 404")
                return DOCUMENT
            with patch.object(scraper, "location_urls", return_value=[URL, URL + "-2"]), \
                 patch.object(scraper, "restaurant", side_effect=fetch):
                with self.assertRaises(RuntimeError):
                    scraper.scrape(output)
            self.assertEqual(output.read_text(), "previous snapshot")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_empty_directory_and_duplicate_ids_preserve_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            output.write_text("previous snapshot")
            for urls in [[], [URL, URL + "-2"]]:
                with self.subTest(urls=urls), \
                     patch.object(scraper, "location_urls", return_value=urls), \
                     patch.object(scraper, "restaurant", return_value=DOCUMENT):
                    with self.assertRaises(RuntimeError):
                        scraper.scrape(output)
                    self.assertEqual(output.read_text(), "previous snapshot")

    def test_writes_complete_sorted_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            second = {**DOCUMENT, "id": "100"}
            with patch.object(scraper, "location_urls", return_value=[URL, URL + "-2"]), \
                 patch.object(scraper, "restaurant", side_effect=[DOCUMENT, second]):
                scraper.scrape(output)
            self.assertEqual([r["id"] for r in json.loads(output.read_text())], ["100", "3481"])
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_http_errors_are_not_accepted_as_data(self):
        failure = subprocess.CalledProcessError(22, "curl")
        with patch.object(scraper.subprocess, "run", side_effect=failure) as run:
            with self.assertRaisesRegex(RuntimeError, "Download failed"):
                scraper.download(URL)
            self.assertIn("--fail", run.call_args.args[0])
            self.assertIn("--max-time", run.call_args.args[0])
            self.assertNotIn("--retry-all-errors", run.call_args.args[0])

    def test_refresh_failure_retains_all_page_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            output.write_text("[]")
            def urls(**kwargs):
                if kwargs.get("fresh"):
                    raise RuntimeError("sitemap unavailable")
                return [URL, URL + "-2"]
            def fetch(url):
                raise scraper.DownloadError(url, 404 if url == URL else 503, "failed")
            with patch.object(scraper, "location_urls", side_effect=urls), \
                 patch.object(scraper, "restaurant", side_effect=fetch):
                with self.assertRaises(RuntimeError) as raised:
                    scraper.scrape(output)
            self.assertIn(URL, str(raised.exception))
            self.assertIn(URL + "-2", str(raised.exception))
            self.assertIn("HTTP 503", str(raised.exception))
            self.assertIn("sitemap unavailable", str(raised.exception))
            self.assertEqual(output.read_text(), "[]")

    def test_count_drop_guard_preserves_snapshot_and_allows_ten_percent(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            baseline = json.dumps([{**DOCUMENT, "id": str(i)} for i in range(10)])
            output.write_text(baseline)
            for count in (8, 9):
                records = [{**DOCUMENT, "id": str(i)} for i in range(count)]
                with patch.object(scraper, "location_urls", return_value=[URL + str(i) for i in range(count)]), \
                     patch.object(scraper, "restaurant", side_effect=lambda url: records[int(url[len(URL):])]):
                    if count == 8:
                        with self.assertRaisesRegex(RuntimeError, "count drop"):
                            scraper.scrape(output)
                        self.assertEqual(output.read_text(), baseline)
                    else:
                        scraper.scrape(output)
                        self.assertEqual(len(json.loads(output.read_text())), 9)

    def test_full_scrape_recovers_once_after_delay_and_reports_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            with patch.object(scraper, "location_urls", return_value=[URL]), \
                 patch.object(scraper, "restaurant", side_effect=[RuntimeError("HTTP 503"), DOCUMENT]) as fetch, \
                 patch("time.sleep") as sleep:
                scraper.scrape(output)
            self.assertEqual(fetch.call_count, 2)
            sleep.assert_called_once_with(20)
            self.assertEqual(len(json.loads(output.read_text())), 1)

    def test_missing_pages_are_reconciled_only_against_fresh_sitemap(self):
        gone = URL + "-gone"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            def fetch(url):
                if url == gone:
                    raise scraper.DownloadError(url, 404, "missing")
                return DOCUMENT
            with patch.object(scraper, "location_urls", side_effect=[[URL, gone], [URL]]) as urls, \
                 patch.object(scraper, "restaurant", side_effect=fetch), \
                 patch("time.sleep") as sleep:
                scraper.scrape(output)
            self.assertEqual(len(json.loads(output.read_text())), 1)
            self.assertEqual(urls.call_args.kwargs, {"fresh": True})
            sleep.assert_called_once_with(5)

    def test_still_listed_missing_page_is_not_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            output.write_text("[]")
            with patch.object(scraper, "location_urls", return_value=[URL]), \
                 patch.object(scraper, "restaurant", side_effect=scraper.DownloadError(URL, 410, "gone")):
                with self.assertRaisesRegex(RuntimeError, "HTTP 410"):
                    scraper.scrape(output)
            self.assertEqual(output.read_text(), "[]")

    def test_refresh_includes_new_restaurants_before_publishing(self):
        gone, new = URL + "-gone", URL + "-new"
        def fetch(url):
            if url == gone:
                raise scraper.DownloadError(url, 410, "gone")
            return {**DOCUMENT, "id": url}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            with patch.object(scraper, "location_urls", side_effect=[[URL, gone], [URL, new]]), \
                 patch.object(scraper, "restaurant", side_effect=fetch):
                scraper.scrape(output)
            self.assertEqual({r["id"] for r in json.loads(output.read_text())}, {URL, new})

    def test_retry_after_http_date_and_excessive_delay(self):
        from datetime import datetime, timezone
        with patch.object(scraper, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 1, tzinfo=timezone.utc)
            self.assertEqual(scraper.retry_after("Retry-After: Sun, 01 Nov 2026 00:00:09 GMT"), 9)
        def response(args, **kwargs):
            Path(args[args.index("--dump-header") + 1]).write_text("Retry-After: 120\r\n")
            return subprocess.CompletedProcess(args, 22, "429", "rate limited")
        with patch.object(scraper.subprocess, "run", side_effect=response) as run, patch("time.sleep") as sleep:
            with self.assertRaises(scraper.DownloadError):
                scraper.download(URL)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    def test_action_summary_reports_success_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            summary = Path(directory) / "summary.md"
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), \
                 patch.object(scraper, "location_urls", return_value=[URL]), \
                 patch.object(scraper, "restaurant", return_value=DOCUMENT):
                scraper.scrape(output)
            self.assertIn("1 validated", summary.read_text())
            summary.write_text("")
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}), \
                 patch.object(scraper, "location_urls", return_value=[URL]), \
                 patch.object(scraper, "restaurant", side_effect=RuntimeError("unavailable")):
                with self.assertRaises(RuntimeError):
                    scraper.scrape(output)
            self.assertIn("Snapshot preserved", summary.read_text())
            self.assertIn(URL, summary.read_text())

    def test_partial_transfer_is_retried_even_after_http_200(self):
        attempts = iter([18, 0])
        def response(args, **kwargs):
            Path(args[args.index("--output") + 1]).write_text("complete")
            return subprocess.CompletedProcess(args, next(attempts), "200", "partial transfer")
        with patch.object(scraper.subprocess, "run", side_effect=response) as run:
            self.assertEqual(scraper.download(URL), "complete")
        self.assertEqual(run.call_count, 2)

    def test_permanent_http_error_is_not_retried(self):
        with patch.object(scraper.subprocess, "run", return_value=subprocess.CompletedProcess([], 22, "404", "missing")) as run, \
             patch("time.sleep") as sleep:
            with self.assertRaises(scraper.DownloadError) as raised:
                scraper.download(URL)
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    def test_retry_after_is_honoured(self):
        statuses = iter([429, 200])
        def response(args, **kwargs):
            Path(args[args.index("--output") + 1]).write_text("body")
            Path(args[args.index("--dump-header") + 1]).write_text("Retry-After: 7\r\n")
            return subprocess.CompletedProcess(args, 0, str(next(statuses)), "")
        with patch.object(scraper.subprocess, "run", side_effect=response), patch("time.sleep") as sleep:
            self.assertEqual(scraper.download(URL), "body")
        sleep.assert_called_once_with(7)

    def test_transient_http_retry_uses_exponential_backoff(self):
        def response(args, **kwargs):
            Path(args[args.index("--output") + 1]).write_text("body")
            Path(args[args.index("--dump-header") + 1]).write_text("")
            return subprocess.CompletedProcess(args, 0, str(next(statuses)), "")
        statuses = iter([503, 503, 200])
        with patch.object(scraper.subprocess, "run", side_effect=response) as run, \
             patch("time.sleep") as sleep:
            self.assertEqual(scraper.download(URL), "body")
        self.assertEqual(run.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])


if __name__ == "__main__":
    unittest.main()
