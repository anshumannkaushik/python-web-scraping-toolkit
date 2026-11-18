"""
Faster + higher hit-rate version of Booking_New__p.py
- Parallel searches with ThreadPoolExecutor
- Reuses DDGS session per thread (no re-create per query)
- More query variants + more results per query
- Smaller polite delay, jittered
"""

import re
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from ddgs import DDGS

# ============ CONFIG ============
INPUT_FILE = "check_cl.xlsx"
OUTPUT_FILE = "check_cl_with_booking.xlsx"
CHECKPOINT_EVERY = 20
MAX_WORKERS = 8                 # 5-10 is sweet spot for DDGS, higher = more rate-limit risk
MIN_DELAY, MAX_DELAY = 0.2, 0.7
RESULTS_PER_QUERY = 30         # was 15 -> more results per query
# =================================

HOTEL_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?booking\.com/hotel/[a-z]{2,3}/[^\"'\s]+\.html",
    re.IGNORECASE,
)

_thread_local = threading.local()
_print_lock = threading.Lock()
_save_lock = threading.Lock()

def get_ddgs():
    if not hasattr(_thread_local, "ddgs"):
        _thread_local.ddgs = DDGS()
    return _thread_local.ddgs

def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)

def build_queries(row):
    """Yield query variants, most specific first. More variants = more hits."""
    name = str(row.get("name") or "").strip()
    a1 = str(row.get("address1") or "").strip()
    a2 = str(row.get("address2") or "").strip()
    city = str(row.get("city") or "").strip()
    country = str(row.get("country") or row.get("Country") or "").strip()
    postal = str(row.get("postal_code") or "").strip()
    if postal.lower() in ("nan", "none", ""):
        postal = ""

    seen = set()
    def _yield(q):
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            yield q

    full = " ".join(p for p in [name, a1, a2, city, country] if p)
    if full:
        yield from _yield(full + " booking.com")
        yield from _yield(full + " site:booking.com")

    if postal:
        with_postal = " ".join(p for p in [name, postal, city, country] if p)
        yield from _yield(with_postal + " booking.com")

    short = " ".join(p for p in [name, city, country] if p)
    if short and short != full:
        yield from _yield(short + " booking.com")

    # NEW: quoted name = better exact match for generic names
    if name and city:
        yield from _yield(f'"{name}" {city} {country} booking.com')

    # NEW: name + address1 + city (helps when address2/postal noisy)
    addr = " ".join(p for p in [name, a1, city, country] if p)
    if addr and addr not in seen:
        yield from _yield(addr + " booking.com")

    if postal:
        yield from _yield(f"{name} {postal} booking.com")

    # NEW last resort: just name + city
    nc = " ".join(p for p in [name, city] if p)
    if nc:
        yield from _yield(nc + " booking.com hotel")


def clean_booking_url(url):
    if not url:
        return url
    # strip tracking params, keep .html part
    m = re.search(r"^(https?://[^\s\"']*?\.html)", url, re.IGNORECASE)
    if m:
        url = m.group(1)
    # normalize to https://www.booking.com/...
    url = re.sub(r"^https?://(www\.)?booking\.com", "https://www.booking.com", url, flags=re.I)
    return url


def is_valid_hotel_url(url):
    return bool(url) and bool(HOTEL_URL_PATTERN.search(url))


def search_booking_url(row):
    ddgs = get_ddgs()
    for query in build_queries(row):
        try:
            results = list(ddgs.text(query, max_results=RESULTS_PER_QUERY))
            for r in results:
                url = r.get("href") or r.get("link") or r.get("url")
                if is_valid_hotel_url(url):
                    return clean_booking_url(url)
        except Exception as e:
            # Don't spam on rate limit, just back off a bit
            err = str(e).lower()
            if "rate" in err or "429" in err or "202" in err:
                time.sleep(random.uniform(2, 5))
            # otherwise skip query silently (fast path)
            continue
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    return None


def process_one(idx, row_dict, pos, total):
    name = row_dict.get("name", "")
    safe_print(f"[{pos}/{total}] {name!r}")
    url = search_booking_url(row_dict)
    safe_print(f"    -> {url if url else 'Not Found'}")
    return idx, (url if url else "Not Found")


def main():
    try:
        df = pd.read_excel(OUTPUT_FILE)
        safe_print(f"Resuming from existing '{OUTPUT_FILE}'")
    except FileNotFoundError:
        df = pd.read_excel(INPUT_FILE)
        safe_print(f"Starting fresh from '{INPUT_FILE}'")

    if "booking_url" not in df.columns:
        df["booking_url"] = None

    # only blank rows — don't retry "Not Found" unless you clear them
    mask = df["booking_url"].isna() | (df["booking_url"] == "")
    todo_idx = df.index[mask].tolist()
    total_todo = len(todo_idx)
    safe_print(f"{len(df)} rows total, {total_todo} still need processing.")
    safe_print(f"Workers={MAX_WORKERS}, results/query={RESULTS_PER_QUERY}\n")

    if not todo_idx:
        safe_print("Nothing to do.")
        return

    # Convert to dicts once to avoid df.loc in threads
    rows = [(idx, df.loc[idx].to_dict()) for idx in todo_idx]

    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(process_one, idx, rd, pos, total_todo): idx
            for pos, (idx, rd) in enumerate(rows, start=1)
        }
        for fut in as_completed(futures):
            idx, url = fut.result()
            df.at[idx, "booking_url"] = url
            completed += 1
            if completed % CHECKPOINT_EVERY == 0:
                with _save_lock:
                    df.to_excel(OUTPUT_FILE, index=False)
                safe_print(f"    (checkpoint saved: {completed}/{total_todo})")

    with _save_lock:
        df.to_excel(OUTPUT_FILE, index=False)

    found = (df["booking_url"] != "Not Found").sum() - (df["booking_url"].isna().sum())
    safe_print(f"\nDone. {completed} processed this run. Results saved to {OUTPUT_FILE}")
    safe_print(f"Total with URL: {(df['booking_url'] != 'Not Found').sum()}/{len(df)}")


if __name__ == "__main__":
    main()
