"""
Storing the controller's measurements in InfluxDB, for the Grafana dashboard.

InfluxDB is a time series database: every entry is a "point" with a time, a
measurement name (like a table name), tags (text labels to filter by, like the
charger id) and fields (the numbers). In InfluxDB's text form, one point looks like:

    charger,run=...,charger_id=CP03,van_id=V03 power_kw=21.94,state_of_charge_percent=55.2 1768410000000000000

Three measurements are written once per control cycle (one simulated minute):
- site:    building_kw, charging_kw, site_kw, estimated_site_kw, site_limit_kw, target_kw
- charger: power_kw, allowed_power_kw, state_of_charge_percent, connected (one point per charger)
- price:   price_eur_per_mwh

The time of each point is the SIMULATED time, so the dashboard shows the night's real
clock times and prices. Every run gets a "run" tag (start time, strategy, scenario),
because two runs of the same night have the same simulated times and would mix otherwise.

Writing never holds up the control loop: each write runs as a background task, and if
InfluxDB is down the measurements are lost but charging control carries on.
"""

import asyncio
import logging

from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync


def site_point(sample, run_name, site_limit_kw, target_kw):
    """
    In: one site log sample (see SiteController.record_sample), run name, site limit and target [kW].
    Out: an InfluxDB Point for the "site" measurement.
    """
    return (Point("site")
            .tag("run", run_name)
            .field("building_kw", float(sample["building_kw"]))
            .field("charging_kw", float(sample["charging_kw"]))
            .field("site_kw", float(sample["site_kw"]))
            .field("estimated_site_kw", float(sample["estimated_site_kw"]))
            .field("site_limit_kw", float(site_limit_kw))
            .field("target_kw", float(target_kw))
            .time(sample["time"]))


def charger_points(chargers_by_id, run_name, sim_time):
    """
    In: dict of all chargers by id, run name, simulated time.
    Out: list of InfluxDB Points for the "charger" measurement, one per charger.
    """
    points = []
    for charger_id, charger in sorted(chargers_by_id.items()):
        point = (Point("charger")
                 .tag("run", run_name)
                 .tag("charger_id", charger_id)
                 .field("power_kw", float(charger.charge_power_kw))
                 .field("allowed_power_kw", float(charger.allowed_power_kw))
                 .field("connected", 1 if charger.is_connected else 0)
                 .time(sim_time))
        # Only while a van is plugged in: which van, and its battery level.
        if charger.van_id is not None:
            point.tag("van_id", charger.van_id)
        if charger.state_of_charge_percent is not None:
            point.field("state_of_charge_percent", float(charger.state_of_charge_percent))
        points.append(point)
    return points


def price_point(price_eur_per_mwh, run_name, sim_time):
    """In: day-ahead price [EUR/MWh], run name, simulated time. Out: an InfluxDB Point for "price"."""
    return Point("price").tag("run", run_name).field("price_eur_per_mwh", float(price_eur_per_mwh)).time(sim_time)


class MeasurementStore:
    """
    Writes points to InfluxDB in the background. A class is used because it keeps the
    open connection, and the writes that are still running.
    """

    def __init__(self, url, token, org, bucket):
        """In: InfluxDB address, token (secret, from .env), organisation and bucket names."""
        # timeout in milliseconds: a write that takes longer is given up.
        self.client = InfluxDBClientAsync(url=url, token=token, org=org, timeout=5000)
        self.write_api = self.client.write_api()
        self.bucket = bucket
        self.running_writes = set()
        self.failed_writes = 0
        self.events = logging.getLogger("STORAGE")

    def write_in_background(self, points):
        """Start writing the points and return at once. The control loop never waits for the database."""
        task = asyncio.create_task(self.write(points))
        # Keep a reference until the task is done; Python could otherwise remove it half-way.
        self.running_writes.add(task)
        task.add_done_callback(self.running_writes.discard)

    async def write(self, points):
        """Write the points. A failure is logged (not every time) and otherwise ignored."""
        try:
            await self.write_api.write(bucket=self.bucket, record=points)
        except Exception as error:
            self.failed_writes += 1
            # Warn at the first failure and then every 60th, so a database that is down
            # does not fill the log with one line per simulated minute.
            if self.failed_writes % 60 == 1:
                self.events.warning("could not write to InfluxDB (%d failed writes so far): %s: %s",
                                    self.failed_writes, type(error).__name__, error)

    async def close(self):
        """Wait for the writes that are still running, then close the connection."""
        if self.running_writes:
            await asyncio.gather(*self.running_writes, return_exceptions=True)
        await self.client.close()
