"""
Save the Grafana dashboard of the newest run as a PNG picture (results/dashboard.png).

Grafana's image renderer opens the dashboard in a browser of its own and takes the
picture. It is an extra container that only starts with the "screenshot" profile:

    docker compose --profile screenshot up -d       (and let one night run)
    python -m src.dashboard_screenshot

The Grafana login comes from .env (GRAFANA_ADMIN_USER, GRAFANA_ADMIN_PASSWORD) and is never printed.
"""

import os
from pathlib import Path

import httpx
from influxdb_client import InfluxDBClient

import settings

GRAFANA_URL = "http://localhost:3000"
OUTPUT_FILE = Path(__file__).resolve().parent.parent / "results" / "dashboard.png"


def newest_run():
    """Out: the name of the newest run stored in InfluxDB (the run tag)."""
    with InfluxDBClient(url=settings.INFLUXDB_URL, token=settings.INFLUXDB_TOKEN, org=settings.INFLUXDB_ORG,
                        timeout=60000) as client:
        tables = client.query_api().query(
            'import "influxdata/influxdb/schema"\n'
            f'schema.tagValues(bucket: "{settings.INFLUXDB_BUCKET}", tag: "run", start: 2020-01-01T00:00:00Z)')
        runs = [record.get_value() for table in tables for record in table.records]
    return sorted(runs)[-1]


def save_dashboard_picture(run_name, night_start_utc, night_end_utc):
    """
    Ask Grafana to render the dashboard for one run and save it as a PNG.

    In: run name, start and end of the time range (ISO texts in UTC).
    Out: the path of the saved picture.
    """
    parameters = {
        "orgId": 1, "from": night_start_utc, "to": night_end_utc, "var-run": run_name,
        "width": 1500, "height": 1060, "tz": "Europe/Berlin", "theme": "light", "kiosk": 1,
    }
    login = (os.environ["GRAFANA_ADMIN_USER"], os.environ["GRAFANA_ADMIN_PASSWORD"])
    # The renderer's browser can take longer than its 30 s start-up timeout the very first
    # time, and then Grafana answers 500. A second try finds it running.
    for attempt in range(2):
        response = httpx.get(f"{GRAFANA_URL}/render/d/depot-charging/depot-charging", params=parameters,
                             auth=login, timeout=180)
        if response.status_code != 500:
            break
    response.raise_for_status()
    if not response.content.startswith(b"\x89PNG"):
        raise RuntimeError("Grafana did not answer with a PNG picture")
    OUTPUT_FILE.parent.mkdir(exist_ok=True)
    OUTPUT_FILE.write_bytes(response.content)
    return OUTPUT_FILE


def main():
    run_name = newest_run()
    # The normal night, 14 January 2026 15:30 to 15 January 08:00 German time (UTC + 1).
    path = save_dashboard_picture(run_name, "2026-01-14T14:30:00.000Z", "2026-01-15T07:00:00.000Z")
    print(f"saved {path} for the run '{run_name}'")


if __name__ == "__main__":
    main()
