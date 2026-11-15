"""Download the complete public PizzaExpress location directory."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
import os
from html import escape
from pathlib import Path
import re
import subprocess
import tempfile
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET


BASE = "https://locations.pizzaexpress.com"
SITEMAP = f"{BASE}/sitemap.xml"


class DownloadError(RuntimeError):
    def __init__(self, url, status, detail):
        self.status = status
        super().__init__(f"Download failed for {url}: HTTP {status or 'unknown'}; {detail}")


def retry_after(headers):
    # curl includes redirect/proxy headers; use the last Retry-After value.
    values = re.findall(r"(?im)^Retry-After:\s*([^\r\n]+)", headers)
    if not values:
        return 0
    try:
        return max(0, int(values[-1]))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(values[-1]) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def download(url, *, fresh=False):
    if urlsplit(url).netloc != "locations.pizzaexpress.com" or urlsplit(url).scheme != "https":
        raise ValueError(f"Unexpected source host: {url}")
    with tempfile.TemporaryDirectory() as directory:
        response = Path(directory) / "response"
        headers = Path(directory) / "headers"
        for attempt in range(4):
            response.unlink(missing_ok=True)
            headers.unlink(missing_ok=True)
            args = ["curl", "--fail", "--location", "--silent", "--show-error",
                    "--connect-timeout", "10", "--max-time", "30",
                    "--proto", "=https", "--proto-redir", "=https",
                    "--output", str(response), "--dump-header", str(headers),
                    "--write-out", "%{http_code}"]
            if fresh:
                args += ["--header", "Cache-Control: no-cache", "--header", "Pragma: no-cache"]
            try:
                result = subprocess.run(args + [url], check=False, capture_output=True,
                                        text=True, timeout=35)
                status = int(result.stdout.strip() or 0)
                code, detail = result.returncode, result.stderr
            except subprocess.CalledProcessError as error:
                status, code, detail = 0, error.returncode, error.stderr
            except subprocess.TimeoutExpired:
                status, code, detail = 0, 28, "curl process timed out"
            if code == 0 and 200 <= status < 300:
                return response.read_text(encoding="utf-8")
            network_error = code in {5, 6, 7, 18, 28, 35, 52, 55, 56, 92}
            transient = status == 429 or 500 <= status <= 599 or network_error
            error = DownloadError(url, status, detail)
            if not transient or attempt == 3:
                raise error
            delay = max(2 ** (attempt + 1), retry_after(headers.read_text() if headers.exists() else ""))
            # Never retry earlier than requested; excessive waits fail closed.
            if delay > 60:
                raise error
            time.sleep(delay)


def location_urls(url=SITEMAP, *, fresh=False):
    root = ET.fromstring(download(url, fresh=True) if fresh else download(url))
    urls = [node.text for node in root.findall(".//{*}loc")]
    if root.tag == "{http://www.sitemaps.org/schemas/sitemap/0.9}sitemapindex":
        urls = [location for sitemap in urls for location in location_urls(sitemap, fresh=fresh)]
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


def write_summary(message):
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        try:
            with open(destination, "a", encoding="utf-8") as file:
                file.write(f"<h3>PizzaExpress scrape</h3><pre>{escape(message)}</pre>\n")
        except OSError as error:
            # Reporting must never turn a published snapshot into a failed scrape.
            print(f"Could not write Action summary: {error}", flush=True)


def scrape(output):
    errors = []
    for attempt in range(2):
        try:
            result = _scrape_once(output, fresh=bool(attempt))
            write_summary(f"Attempt {attempt + 1}: {result}" +
                          ("\nRecovered after:\n" + "\n".join(errors) if errors else ""))
            return
        except (RuntimeError, ValueError, OSError, ET.ParseError) as error:
            errors.append(f"Attempt {attempt + 1}: {error}")
            print(errors[-1], flush=True)
            if attempt == 0:
                print("Retrying full scrape once in 20 seconds", flush=True)
                time.sleep(20)
    message = "Snapshot preserved.\n" + "\n".join(errors)
    write_summary(message)
    raise RuntimeError(message)


def _scrape_once(output, *, fresh=False):
    urls = location_urls(fresh=True) if fresh else location_urls()
    if not urls:
        raise ValueError("The sitemap contains no restaurant URLs")
    print(f"Fetching {len(urls)} restaurant pages", flush=True)
    records_by_url, failures = {}, {}
    removed = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {url: pool.submit(restaurant, url) for url in urls}
        for url, future in futures.items():
            try:
                records_by_url[url] = future.result()
            except Exception as error:
                failures[url] = error
    if any(isinstance(error, DownloadError) and error.status in {404, 410}
           for error in failures.values()):
        time.sleep(5)
        try:
            refreshed = set(location_urls(fresh=True))
            if not refreshed:
                raise ValueError("Fresh sitemap contains no restaurant URLs")
        except Exception as error:
            failures[SITEMAP] = error
            refreshed = set(urls)  # A failed refresh never confirms a removal.
        for url, error in list(failures.items()):
            if isinstance(error, DownloadError) and error.status in {404, 410} and url not in refreshed:
                removed.append(url)
                print(f"Confirmed removed from fresh sitemap: {url}", flush=True)
                del failures[url]
        # Newly discovered pages must also be validated before publication.
        for url in sorted(refreshed - set(urls)):
            try:
                records_by_url[url] = restaurant(url)
            except Exception as error:
                failures[url] = error
    if failures:
        raise RuntimeError("Unresolved restaurant failures:\n" + "\n".join(
            f"{url}: {error}" for url, error in sorted(failures.items())))
    records = list(records_by_url.values())
    if not records:
        raise ValueError("No validated restaurants remain")
    if len({record["id"] for record in records}) != len(records):
        raise ValueError("Duplicate restaurant IDs in directory")
    if output.exists():
        previous = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(previous, list):
            raise ValueError("Existing snapshot must be a JSON array")
        print(f"Baseline guard: {len(previous)} previous, {len(records)} candidate (maximum drop 10%)", flush=True)
        if len(records) * 10 < len(previous) * 9:
            raise ValueError(f"Suspicious count drop: {len(previous)} -> {len(records)} exceeds 10%")
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
    return (f"{len(records)} validated, {len(removed)} confirmed removed, 0 unresolved failures."
            + ("\nRemoved URLs:\n" + "\n".join(removed) if removed else ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?", default=Path("pe-locations.json"))
    scrape(parser.parse_args().output)
