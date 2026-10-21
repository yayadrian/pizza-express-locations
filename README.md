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

Every restaurant page in the sitemap must download successfully and contain
a uniquely identified restaurant with a name, address, and coordinates before
the saved file is replaced. Regional directory pages are excluded.
Failures preserve the previous snapshot and fail the workflow.

Run the scraper regression checks with
`python3 -m unittest discover -s tests -v`.
