"""Download the complete public PizzaExpress location directory."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET


BASE = "https://locations.pizzaexpress.com"
SITEMAP = f"{BASE}/sitemap.xml"


def download(url):
    if urlsplit(url).netloc != "locations.pizzaexpress.com":
        raise ValueError(f"Unexpected source host: {url}")
    try:
        with tempfile.TemporaryDirectory() as directory:
            response = Path(directory) / "response"
            # A file lets curl discard partial responses when retrying a transfer.
            subprocess.run(
                ["curl", "--fail", "--location", "--silent", "--show-error",
                 "--retry", "3", "--retry-all-errors", "--connect-timeout", "10",
                 "--max-time", "60", "--output", str(response), url],
                check=True, capture_output=True, text=True, timeout=300,
            )
            return response.read_text(encoding="utf-8")
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Download failed for {url}: {error.stderr}") from error


def location_urls(url=SITEMAP):
    root = ET.fromstring(download(url))
    urls = [node.text for node in root.findall(".//{*}loc")]
    if root.tag == "{http://www.sitemaps.org/schemas/sitemap/0.9}sitemapindex":
        urls = [location for sitemap in urls for location in location_urls(sitemap)]
    elif root.tag != "{http://www.sitemaps.org/schemas/sitemap/0.9}urlset":
        raise ValueError("Expected a sitemap index or URL set")
    # The sitemap also lists regional directory pages, which aren't restaurants.
    return sorted(set(url for url in urls
                      if not urlsplit(url).path.startswith("/restaurants")))


class PageScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_script = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts.append(data)


def restaurant(url):
    parser = PageScripts()
    parser.feed(download(url))
    # Yext's server-rendered pages embed the public location document here.
    pattern = r'pageProps:\s*JSON\.parse\(decodeURIComponent\("([^"]+)"\)\)'
    matches = [match for script in parser.scripts
               for match in re.finditer(pattern, script)]
    if len(matches) != 1:
        raise ValueError(f"Expected one location document: {url}")
    document = json.loads(unquote(matches[0][1]))["document"]
    if document.get("meta", {}).get("entityType", {}).get("id") != "restaurant":
        raise ValueError(f"Expected a restaurant entity: {url}")
    for field in ("id", "name", "slug"):
        if not isinstance(document.get(field), str) or not document[field]:
            raise ValueError(f"Missing {field}: {url}")
    if not isinstance(document.get("address"), dict) or not document["address"].get("line1"):
        raise ValueError(f"Missing address: {url}")
    coordinates = document.get("yextDisplayCoordinate", {})
    for field, limit in (("latitude", 90), ("longitude", 180)):
        value = coordinates.get(field)
        if type(value) not in (int, float) or not -limit <= value <= limit:
            raise ValueError(f"Invalid {field}: {url}")
    # Exclude site rendering configuration and directory/listing infrastructure.
    infrastructure = {"businessId", "meta", "siteDomain", "siteId",
                      "siteInternalHostName", "uid", "path"}
    return {key: value for key, value in document.items()
            if not key.startswith(("_", "dm_", "ref_")) and key not in infrastructure}


def scrape(output):
    urls = location_urls()
    if not urls:
        raise ValueError("The sitemap contains no restaurant URLs")
    print(f"Fetching {len(urls)} restaurant pages", flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = []
        for record in pool.map(restaurant, urls):
            records.append(record)
            if len(records) % 50 == 0:
                print(f"Fetched {len(records)}/{len(urls)}", flush=True)
    if len({record["id"] for record in records}) != len(records):
        raise ValueError("Duplicate restaurant IDs in directory")
    records.sort(key=lambda record: record["id"])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=output.parent,
                                         encoding="utf-8", delete=False) as file:
            temporary = Path(file.name)
            json.dump(records, file, indent=2, ensure_ascii=False, allow_nan=False)
            file.write("\n")
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Saved {len(records)} restaurants to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=Path("pe-locations.json"))
    scrape(parser.parse_args().output)
