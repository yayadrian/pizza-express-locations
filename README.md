# pizza-express-locations
Git-scraping of Pizza Express locations

The daily GitHub Actions workflow follows the public sitemap at
<https://locations.pizzaexpress.com/sitemap.xml> and downloads the restaurant
documents embedded in each location page into `pe-locations.json`.
Run it locally with `python3 scripts/fetch_locations.py` (requires Python 3.10+
and curl, with no third-party Python dependencies).
An optional first argument selects a different output file.

The previous Sitecore API stopped working in October 2026. New snapshots use
the location site's Yext document schema, including string `id`, `address`,
`yextDisplayCoordinate`, `hours`, and `c_displayName`, rather than the old
`restaurantId`, `latitude`, and `longitude` fields. IDs come from the new source;
consumers should not assume they match historical identifiers. Site rendering
and directory/listing metadata are excluded. Records are sorted by `id` to
keep diffs stable. The main site's `/mapi/restaurants` search endpoint is not
used because it caps results at ten, even with a larger `limit` parameter.

## Reliability and failure handling

The scraper uses four concurrent downloads and validates every restaurant's
unique ID, name, address and coordinates. Regional directory pages are excluded.
The JSON schema described above is unchanged by these reliability measures.

- **Temporary failures:** network/connection errors, HTTP 429 and HTTP 5xx get
  up to four total download attempts, with exponential delays of 2, 4 and 8
  seconds. `Retry-After` (seconds or an HTTP date) takes precedence if longer.
  A requested wait above 60 seconds fails that download instead of retrying
  too early. Other HTTP errors are not immediately retried.
- **Stale sitemap entries:** if any restaurant returns HTTP 404 or 410, wait
  five seconds and fetch the sitemap index and its child sitemaps with
  `Cache-Control: no-cache` and `Pragma: no-cache`. A missing page is omitted
  only if its URL has also disappeared from that fresh sitemap. A still-listed
  missing page, an empty/unavailable fresh sitemap, or another unresolved error
  prevents publication. Newly discovered URLs in the fresh sitemap must also
  download and validate. This confirms removal of a listing, not necessarily
  permanent closure of the restaurant.
- **Recovery attempt:** after an unsuccessful scrape, wait 20 seconds and try
  the complete scrape once more, starting with fresh sitemap requests. Failed
  URLs and their errors are collected rather than stopping at the first page.
- **Data-loss guard:** when the output file already exists, a candidate with
  more than a 10% decrease in restaurant count is rejected. Exactly 10% is
  allowed. Empty directories, invalid records and duplicate IDs are rejected
  regardless of the count. This guard does not detect every possible upstream
  omission; smaller changes still need inspection in Git diffs.
- **Atomic publication:** only a complete, validated candidate replaces the
  output file. Failed attempts preserve the previous snapshot; yesterday's
  records are never silently substituted for failed downloads.
- **Reporting:** logs show reconciliation and baseline checks. In GitHub Actions,
  the run summary reports validated/confirmed-removed counts or unresolved
  failures, including errors from an unsuccessful first attempt after recovery.

Each curl transfer has a 10-second connection timeout and a 30-second transfer
limit. The Action has a 15-minute scrape-job timeout as an overall safety cap;
large upstream outages may still fail the job rather than publish partial data.
No code can guarantee that the upstream sitemap and pages are always consistent.

## Workflow and local verification

The workflow is scheduled daily at **23:03 UTC** (GitHub may delay scheduled
runs). Pushes and pull requests run the tests. Only `main` runs the live scrape
and commits changed data; feature branches and PRs cannot publish snapshots.
Manual dispatch on `main` also scrapes. Workflow runs on the same ref are
serialized, and the scrape job checks out the latest `main`.

Run the scraper regression checks with
`python3 -m unittest discover -s tests -v`.

To test a live scrape without changing the tracked snapshot, copy the current
snapshot to a scratch path and use that path as the output:

```sh
cp pe-locations.json /your/scratch/pe-locations-check.json
python3 scripts/fetch_locations.py /your/scratch/pe-locations-check.json
```

Copying the baseline exercises the count-drop guard; using a nonexistent output
path creates a first snapshot with no previous-count comparison. If the guard
blocks a legitimate large change, inspect the source and proposed removals
before deliberately generating a separate new snapshot and replacing the tracked
one. Do not automatically remove the baseline or skip unresolved pages to force
a green run.
