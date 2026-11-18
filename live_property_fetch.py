"""
expedia_live_fetch.py — fetch the REAL live property name + address for
Expedia hotel URLs.

Why this works: expedia.com serves a "Bot or Not?" wall (HTTP 429) to
datacenter IPs, but Expedia's regional mirrors — expedia.ca, expedia.co.uk,
expedia.com.au, expedia-aarp.com — serve the SAME live property page (same
hotel id) without the wall. The script rewrites each URL's domain to a
mirror, fetches it with a browser TLS fingerprint (curl_cffi), and parses
the live page itself:

  - name    : the page <h1> (the property's current live name — NOT the
              name baked into the URL slug, which goes stale on rebrands)
  - address : schema.org PostalAddress microdata embedded in the page
              (streetAddress / addressLocality / addressRegion /
              postalCode / addressCountry)

Usage:
  python3 expedia_live_fetch.py INPUT OUTPUT
    INPUT  : .xlsx with a URL column (auto-detected, or --url-col NAME),
             or .csv, or .txt with one URL per line
    OUTPUT : .xlsx with live_name / live_address / fetch_status added

Options:
  --url-col NAME   column holding the Expedia URLs (xlsx/csv input)
  --delay SECONDS  base pause between requests (default 1.0, jittered)
  --limit N        only process the first N pending rows (testing)

Resumable: re-running the same command picks up where it left off —
rows already marked ok/not_found are skipped. Checkpoints every 20 rows.
Requires: pandas, openpyxl, curl_cffi  (pip install curl_cffi)
"""

import argparse
import html as html_mod
import random
import re
import sys
import time

import pandas as pd
from curl_cffi import requests as creq

# Mirrors tried in order; the original domain is the last resort.
MIRROR_DOMAINS = [
    "www.expedia.ca",
    "www.expedia.co.uk",
    "www.expedia.com.au",
    "www.expedia-aarp.com",
]
REQUEST_TIMEOUT = 30
MAX_ATTEMPTS_PER_DOMAIN = 2
CHECKPOINT_EVERY = 20

BOT_WALL_MARKERS = ("Bot or Not", "Show us your human side")
NOT_FOUND_MARKERS = (
    "we couldn't find this page",
    "we could not find this page",
    "page not found",
    "this property is no longer available",
)

URL_IN_TEXT = re.compile(r"https?://[^\s\"']*expedia[^\s\"']*\.h\d+\.[^\s\"']*", re.I)


def swap_domain(url, domain):
    return re.sub(r"^(https?://)[^/]+", r"\g<1>" + domain, url, count=1)


def parse_live_page(html):
    """Return (name, address) from a live Expedia property page, else
    (None, None)."""
    name = None
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    if m:
        name = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        name = html_mod.unescape(re.sub(r"\s+", " ", name))
    if not name:
        m = re.search(r'itemProp="name"\s+content="([^"]+)"', html)
        if m:
            name = m.group(1).strip()
    if not name:
        m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
        if m:
            t = m.group(1)
            t = re.split(
                r"\s+-\s+(?:Latest Reviews|Check Reviews|Reviews,).*?Expedia.*$",
                t,
            )[0]
            name = t.strip()

    def micro(prop):
        mm = re.search(r'itemProp="%s"\s+content="([^"]*)"' % prop, html)
        return html_mod.unescape(mm.group(1)).strip() if mm else ""

    parts = [
        micro("streetAddress"),
        micro("addressLocality"),
        micro("addressRegion"),
        micro("postalCode"),
        micro("addressCountry"),
    ]
    address = ", ".join(p for p in parts if p) or None
    if address:
        address = re.sub(r",\s*,", ",", address)
    return (name or None), address


def fetch_one(url, delay):
    """Try each mirror domain until one serves the real page.
    Returns (status, name, address, domain_used)."""
    domains = list(MIRROR_DOMAINS)
    m = re.match(r"https?://([^/]+)", url)
    if m and m.group(1) not in domains:
        domains.append(m.group(1))

    saw_botwall = False
    for domain in domains:
        target = swap_domain(url, domain)
        for attempt in range(MAX_ATTEMPTS_PER_DOMAIN):
            try:
                time.sleep(delay * (0.7 + random.random() * 0.6))
                r = creq.get(target, impersonate="chrome", timeout=REQUEST_TIMEOUT,
                             allow_redirects=True)
            except Exception:
                continue
            html = r.text or ""
            if r.status_code == 404:
                return "not_found", None, None, domain
            if r.status_code == 429 or any(b in html for b in BOT_WALL_MARKERS):
                saw_botwall = True
                break  # next domain, this one is walled
            if r.status_code != 200:
                continue
            name, address = parse_live_page(html)
            if name:
                return "ok", name, address, domain
            low = html.lower()
            if any(n in low for n in NOT_FOUND_MARKERS):
                return "not_found", None, None, domain
    return ("bot_blocked" if saw_botwall else "error"), None, None, None


def load_input(path, url_col):
    if path.lower().endswith(".txt"):
        urls = []
        with open(path) as f:
            for line in f:
                mm = URL_IN_TEXT.search(line)
                if mm:
                    urls.append(mm.group(0))
        return pd.DataFrame({"expedia_url": urls}), "expedia_url"
    if path.lower().endswith(".csv"):
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path)
    if url_col is None:
        for c in df.columns:
            sample = df[c].astype(str)
            if sample.str.contains(r"expedia\.[a-z.]+/", regex=True).any():
                url_col = c
                break
    if url_col is None:
        sys.exit("Could not find a column with Expedia URLs — pass --url-col.")
    return df, url_col


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--url-col", default=None)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    try:
        df = pd.read_excel(args.output)
        print(f"Resuming from existing '{args.output}'")
    except Exception:
        df, url_col = load_input(args.input, args.url_col)
        print(f"Starting fresh from '{args.input}' (url column: {url_col})")
    else:
        url_col = args.url_col
        if url_col is None:
            for c in df.columns:
                if df[c].astype(str).str.contains(r"expedia\.[a-z.]+/", regex=True).any():
                    url_col = c
                    break

    for col in ("live_name", "live_address", "fetch_status", "fetch_domain"):
        if col not in df.columns:
            df[col] = None

    done_mask = df["fetch_status"].isin(["ok", "not_found"])
    todo = [i for i in df.index[~done_mask]
            if isinstance(df.at[i, url_col], str)
            and "expedia" in df.at[i, url_col]
            and ".h" in df.at[i, url_col]]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(df)} rows total, {len(todo)} to fetch.\n")

    counts = {"ok": 0, "not_found": 0, "bot_blocked": 0, "error": 0}
    for n, idx in enumerate(todo, 1):
        url = df.at[idx, url_col].strip()
        status, name, address, domain = fetch_one(url, args.delay)
        df.at[idx, "live_name"] = name
        df.at[idx, "live_address"] = address
        df.at[idx, "fetch_status"] = status
        df.at[idx, "fetch_domain"] = domain
        counts[status] = counts.get(status, 0) + 1
        print(f"[{n}/{len(todo)}] {status:11s} {name or '-'} | {address or '-'}")
        if n % CHECKPOINT_EVERY == 0:
            df.to_excel(args.output, index=False)
            print(f"    (checkpoint saved: {n}/{len(todo)})")

    df.to_excel(args.output, index=False)
    print(f"\nDone. {counts}  Saved to {args.output}")


if __name__ == "__main__":
    main()
