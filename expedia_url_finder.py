"""Find Expedia hotel URLs for hotel rows in hotels.xlsx.

Faster variant of expedia_latest.txt:
- 2 focused query variants per row instead of 4
- 10 workers, 5s timeout, 0.3s global request gap
- resumes from the output file, checkpoints every 10 rows
"""
import re
import time
import threading
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from ddgs import DDGS

INPUT_FILE = "hotels.xlsx"
OUTPUT_FILE = "hotels_with_expedia_urls.xlsx"
CHECKPOINT_EVERY = 10

MAX_WORKERS = 10
DDGS_TIMEOUT = 5
DDGS_BACKEND = "lite"
RESULTS_PER_QUERY = 8
MIN_GLOBAL_GAP = 0.3

HOTEL_URL_PATTERN = re.compile(
    r"expedia\.[a-z]{2,4}(?:\.[a-z]{2,3})?/[^\s\"']*?\.h\d+\.[A-Za-z-]*Information",
    re.IGNORECASE,
)

_rate_lock = threading.Lock()
_last_call_time = [0.0]


def _throttle():
    with _rate_lock:
        now = time.monotonic()
        wait = MIN_GLOBAL_GAP - (_last_call_time[0] and (now - _last_call_time[0]))
        if wait and wait > 0:
            time.sleep(wait)
        _last_call_time[0] = time.monotonic()


def _clean(s):
    s = str(s or "").strip()
    return "" if s.lower() in ("nan", "none") else s


def build_queries(row):
    name = _clean(row.get("name"))
    a1 = _clean(row.get("address1"))
    a2 = _clean(row.get("address2"))
    city = _clean(row.get("city"))
    country = _clean(row.get("Country")) or _clean(row.get("country"))
    postal = _clean(row.get("Postal")) or _clean(row.get("postal_code"))

    # 1) most precise: full address with postal
    full = " ".join(p for p in [name, a1, a2, city, postal, country] if p)
    if full:
        yield full + " expedia"

    # 2) fallback: name + city + country
    short = " ".join(p for p in [name, city, country] if p)
    if short and short != full:
        yield short + " expedia"


def clean_expedia_url(url):
    if not url:
        return url
    m = re.search(r"^(https?://[^\s\"']*?Information)", url, re.IGNORECASE)
    return m.group(1) if m else url


def is_valid_hotel_url(url):
    return bool(url) and bool(HOTEL_URL_PATTERN.search(url))


def search_expedia_url(row):
    for query in build_queries(row):
        try:
            _throttle()
            with DDGS(timeout=DDGS_TIMEOUT) as ddgs:
                results = list(ddgs.text(query, max_results=RESULTS_PER_QUERY,
                                        backend=DDGS_BACKEND))
            for r in results:
                url = r.get("href") or r.get("link")
                if is_valid_hotel_url(url):
                    return clean_expedia_url(url)
        except Exception as e:
            print(f"    ! search error ({e}) — skipping query")
    return None


def process_row(idx, row):
    url = search_expedia_url(row)
    return idx, (url if url else "Not Found")


def main():
    try:
        df = pd.read_excel(OUTPUT_FILE)
        print(f"Resuming from existing '{OUTPUT_FILE}'")
    except FileNotFoundError:
        df = pd.read_excel(INPUT_FILE)
        print(f"Starting fresh from '{INPUT_FILE}'")

    if "expedia_url" not in df.columns:
        df["expedia_url"] = None

    mask = df["expedia_url"].isna() | (df["expedia_url"] == "Not Found")
    todo_idx = list(df.index[mask])
    total = len(df)
    print(f"{total} rows total, {len(todo_idx)} still need processing.\n")

    save_lock = threading.Lock()
    completed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_row, idx, df.loc[idx]): idx
                   for idx in todo_idx}
        for future in as_completed(futures):
            idx, result = future.result()
            with save_lock:
                df.at[idx, "expedia_url"] = result
                completed += 1
                print(f"[{completed}/{len(todo_idx)}] {df.at[idx, 'name']!r} -> {result}")
                if completed % CHECKPOINT_EVERY == 0:
                    df.to_excel(OUTPUT_FILE, index=False)
                    print(f"    (checkpoint saved: {completed}/{len(todo_idx)})")

    df.to_excel(OUTPUT_FILE, index=False)
    found = (df["expedia_url"] != "Not Found").sum()
    print(f"\nDone. {found}/{total} found. Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
