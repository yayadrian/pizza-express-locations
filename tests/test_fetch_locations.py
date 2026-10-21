import copy
import json
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
            with patch.object(scraper, "location_urls", return_value=[URL, URL + "-2"]), \
                 patch.object(scraper, "restaurant", side_effect=[DOCUMENT, RuntimeError("HTTP 404")]):
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
                    with self.assertRaises(ValueError):
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
            self.assertIn("--retry-all-errors", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
