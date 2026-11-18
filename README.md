# Python Web Scraping Toolkit

Production scraping pipelines for matching large spreadsheet datasets (1,000+ records) against live travel websites — the toolkit behind my day-to-day data work.

## What's inside

| Script | What it does |
|---|---|
| `expedia_url_finder.py` | Takes a spreadsheet of hotel records and finds each property's Expedia URL via parallel DuckDuckGo searches (10 workers, global rate-gap, checkpointed every 10 rows, resumable from its own output). |
| `booking_url_finder.py` | Same pattern for Booking.com — thread-pooled searches with per-thread session reuse, jittered polite delays, and multiple query variants per record for a higher hit rate. |
| `live_property_fetch.py` | Given Expedia URLs, fetches the **real live** property name (page `<h1>`) and full address (schema.org PostalAddress microdata). Works around Expedia's bot wall by reading the same property page through regional mirrors (expedia.ca / .co.uk / .com.au) with a browser TLS fingerprint (`curl_cffi`). |

## Design principles

- **Checkpoint everything** — long batches survive crashes and rate-limit waves; a run resumes from its output file instead of starting over.
- **Verify, don't guess** — a URL is only accepted on a pattern match against the result, and live verification reads the actual page (name + address), never the URL slug or a search snippet.
- **Polite parallelism** — thread pools with global request gaps and jitter, tuned to stay under search-engine throttling.

## Usage

```bash
pip install pandas ddgs curl_cffi openpyxl

python expedia_url_finder.py     # hotels.xlsx -> hotels_with_expedia_urls.xlsx
python booking_url_finder.py     # check_cl.xlsx -> check_cl_with_booking.xlsx
python live_property_fetch.py INPUT.xlsx OUTPUT.xlsx
```

Input/output filenames are configured at the top of each script.
