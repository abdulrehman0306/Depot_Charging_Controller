"""
Real German day-ahead prices from SMARD.de (Bundesnetzagentur), quarter hours, EUR/MWh.

The download and loading code is taken from my earlier project
Battery_Revenue_Study (src/download_data.py and src/load_data.py), cut down to
the price series and to the nights this project uses:
https://github.com/abdulrehman0306/Battery_Revenue_Study

Download the price files once (they are also kept in the repository, so this
is only needed for other nights):
    python -m src.prices

SMARD data licence: CC BY 4.0, source "Bundesnetzagentur | SMARD.de".
"""

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta

import pandas as pd

import settings

SMARD_BASE_URL = "https://www.smard.de/app/chart_data"

# Be polite to the website: wait a little between requests, and a bit
# longer before trying a failed request again.
PAUSE_BETWEEN_REQUESTS_S = 0.5
PAUSE_BEFORE_RETRY_S = 5
NUMBER_OF_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Download (from Battery_Revenue_Study/src/download_data.py)
# ---------------------------------------------------------------------------

def is_valid_smard_file(content):
    """True if the downloaded bytes are SMARD JSON with a 'series' list."""
    try:
        return "series" in json.loads(content)
    except ValueError:
        return False


def fetch_with_retries(url, is_valid):
    """
    Download one URL.

    Out: (content as bytes, None) on success,
         or (None, description of the last error) if every attempt failed.
    """
    last_error = None
    for attempt in range(1, NUMBER_OF_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                content = response.read()
            if is_valid(content):
                return content, None
            last_error = "the website answered, but not with the expected file type"
        except Exception as error:
            # Timeouts, lost connections and HTTP errors all end up here.
            last_error = str(error)

        print(f"    attempt {attempt} of {NUMBER_OF_ATTEMPTS} failed: {last_error}")
        if attempt < NUMBER_OF_ATTEMPTS:
            time.sleep(PAUSE_BEFORE_RETRY_S)
    return None, last_error


def night_period_ms(night_date):
    """
    In: the date a night starts. Out: (start, end) in Unix milliseconds, covering
    that whole day and the next one, so every quarter hour of the night is included.
    """
    start = datetime(night_date.year, night_date.month, night_date.day, tzinfo=settings.TIMEZONE)
    end = start + timedelta(days=2)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def smard_weeks_for_night(night_date):
    """
    Ask SMARD which weekly files exist and keep the ones that overlap the night.

    Out: list of week start times in Unix milliseconds, or None if the list could not be downloaded.
    """
    index_url = (f"{SMARD_BASE_URL}/{settings.SMARD_PRICE_SERIES_ID}/{settings.SMARD_PRICE_REGION}"
                 "/index_quarterhour.json")
    content, error = fetch_with_retries(index_url, lambda c: "timestamps" in json.loads(c))
    if content is None:
        print(f"  Could not get the list of weeks: {error}")
        return None
    week_starts_ms = sorted(json.loads(content)["timestamps"])
    start_ms, end_ms = night_period_ms(night_date)

    # A weekly file covers the time from its own start up to the start of the
    # next file. Keep it if that range overlaps the night at all.
    weeks_needed = []
    for position, week_start_ms in enumerate(week_starts_ms):
        if position + 1 < len(week_starts_ms):
            week_end_ms = week_starts_ms[position + 1]
        else:
            week_end_ms = float("inf")
        if week_start_ms < end_ms and week_end_ms > start_ms:
            weeks_needed.append(week_start_ms)
    return weeks_needed


def download_prices_for_night(night_date):
    """
    Download the SMARD weekly price files a night needs, skipping files that already exist.

    Out: list of failures (texts), empty if everything worked.
    """
    folder = settings.PRICE_DATA_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    weeks = smard_weeks_for_night(night_date)
    if weeks is None:
        return [f"night of {night_date}: list of weeks could not be downloaded"]

    failures = []
    for week_start_ms in weeks:
        file_name = f"{settings.SMARD_PRICE_SERIES_ID}_{settings.SMARD_PRICE_REGION}_quarterhour_{week_start_ms}.json"
        if (folder / file_name).exists():
            continue
        print(f"  downloading {file_name}")
        url = f"{SMARD_BASE_URL}/{settings.SMARD_PRICE_SERIES_ID}/{settings.SMARD_PRICE_REGION}/{file_name}"
        content, error = fetch_with_retries(url, is_valid_smard_file)
        if content is None:
            failures.append(f"{file_name}: {error}")
        else:
            (folder / file_name).write_bytes(content)
        time.sleep(PAUSE_BETWEEN_REQUESTS_S)
    return failures


# ---------------------------------------------------------------------------
# Loading (from Battery_Revenue_Study/src/load_data.py)
# ---------------------------------------------------------------------------

def unix_ms_to_berlin(timestamps_ms):
    """
    Convert a list of Unix millisecond timestamps to German local times.

    Unix time counts from 1970 in UTC and has no daylight saving jumps, so it
    is first read as UTC and only then converted to German local time.
    """
    utc_times = pd.to_datetime(timestamps_ms, unit="ms", utc=True)
    return utc_times.tz_convert("Europe/Berlin")


def load_prices(folder=None):
    """
    Read all weekly SMARD price files in the price folder.

    In: folder (default: PRICE_DATA_FOLDER from settings.py).
    Out: pandas Series of prices [EUR/MWh] indexed by German local time, one value
         per quarter hour, sorted. SMARD's null values become NaN; nothing is filled in.
    """
    if folder is None:
        folder = settings.PRICE_DATA_FOLDER
    files = sorted(folder.glob(f"{settings.SMARD_PRICE_SERIES_ID}_{settings.SMARD_PRICE_REGION}_quarterhour_*.json"))
    if not files:
        raise FileNotFoundError(f"No SMARD price files in {folder}. Run: python -m src.prices")

    timestamps_ms = []
    values = []
    for file in files:
        with open(file, encoding="utf-8") as handle:
            content = json.load(handle)
        for timestamp_ms, value in content["series"]:
            timestamps_ms.append(timestamp_ms)
            values.append(value)

    # dtype float turns SMARD's null (None in Python) into NaN.
    prices = pd.Series(values, index=unix_ms_to_berlin(timestamps_ms), dtype="float64").sort_index()

    # The same quarter hour may appear in two weekly files. That is only
    # harmless if both copies carry the same value.
    copies = prices[prices.index.duplicated(keep=False)]
    if (copies.groupby(level=0).nunique(dropna=False) > 1).any():
        raise ValueError("SMARD prices: duplicated timestamps with different values")
    return prices[~prices.index.duplicated(keep="first")]


def price_at(prices, sim_time):
    """
    The price of the quarter hour that contains sim_time.

    In: price Series from load_prices(), a time (datetime with time zone).
    Out: price [EUR/MWh]. Raises KeyError if that quarter hour has no price.
    """
    quarter_start = pd.Timestamp(sim_time).floor("15min")
    price = prices.get(quarter_start)
    if price is None or pd.isna(price):
        raise KeyError(f"no price for the quarter hour starting {quarter_start}")
    return float(price)


def main():
    all_failures = []
    for night_date in (settings.NORMAL_NIGHT_DATE, settings.NEGATIVE_PRICE_NIGHT_DATE):
        print(f"Day-ahead prices for the night of {night_date}")
        all_failures += download_prices_for_night(night_date)

    if all_failures:
        print(f"\n{len(all_failures)} download(s) failed:")
        for failure in all_failures:
            print(f"  {failure}")
        sys.exit(1)
    print("All price files are there.")


if __name__ == "__main__":
    main()
