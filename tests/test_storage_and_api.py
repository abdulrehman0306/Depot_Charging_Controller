"""
Stage 6: the InfluxDB points, the Grafana dashboard and provisioning files,
keeping secrets out of git, and the REST API.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import settings
from src.api.app import create_app
from src.controller.site_control import SiteController
from src.controller.storage import MeasurementStore, charger_points, price_point, site_point
from src.sim_clock import SimulationClock

REPOSITORY = Path(__file__).resolve().parent.parent
NOW = settings.SIMULATION_START_TIME
RUN = "2026-11-17 10:00:00 smart normal_evening"


def make_charger(van_id=None, state_of_charge_percent=None, power_kw=0.0, allowed_power_kw=0.0):
    """A charger as the controller sees it, without a network connection."""
    return SimpleNamespace(is_connected=True, connector_status="Occupied" if van_id else "Available",
                           van_id=van_id, charging_state="Charging" if power_kw > 0 else None,
                           transaction_id="tx" if van_id else None, charge_power_kw=power_kw,
                           allowed_power_kw=allowed_power_kw, state_of_charge_percent=state_of_charge_percent,
                           arrival_state_of_charge_percent=state_of_charge_percent)


def make_controller():
    """A controller with two chargers (one with a van charging at 22 kW) and a 22 kW building load."""
    chargers = {"CP01": make_charger("V01", 55.0, power_kw=22.0, allowed_power_kw=22.0), "CP02": make_charger()}
    controller = SiteController(chargers, SimulationClock(NOW, 60), None, "smart",
                                fleet_schedule={"V01": NOW.replace(hour=6)})
    controller.building_kw = 22.0
    controller.planned_shortfall_kwh = {"V01": 0.0}
    controller.record_sample(NOW)
    return controller


# ---------------------------------------------------------------------------
# InfluxDB points
# ---------------------------------------------------------------------------

def test_site_point_holds_the_site_values_at_simulated_time():
    sample = {"time": NOW, "building_kw": 22.0, "charging_kw": 113.0, "site_kw": 135.0, "estimated_site_kw": 135.0}
    line = site_point(sample, RUN, 150.0, 135.0).to_line_protocol()
    assert line.startswith("site,run=2026-11-17\\ 10:00:00\\ smart\\ normal_evening ")
    for text in ["building_kw=22", "charging_kw=113", "site_kw=135", "site_limit_kw=150", "target_kw=135"]:
        assert text in line
    # The time stamp is the simulated time (in nanoseconds since 1970), not the computer's clock.
    assert line.endswith(str(int(NOW.timestamp()) * 1_000_000_000))


def test_charger_points_tag_the_van_only_while_one_is_plugged_in():
    chargers = {"CP01": make_charger("V01", 55.0, power_kw=11.0, allowed_power_kw=11.04), "CP02": make_charger()}
    with_van, without_van = [point.to_line_protocol() for point in charger_points(chargers, RUN, NOW)]
    assert "charger_id=CP01" in with_van and "van_id=V01" in with_van
    assert "power_kw=11" in with_van and "state_of_charge_percent=55" in with_van and "connected=1i" in with_van
    assert "charger_id=CP02" in without_van
    assert "van_id" not in without_van and "state_of_charge_percent" not in without_van


def test_price_point():
    assert "price_eur_per_mwh=82.36" in price_point(82.36, RUN, NOW).to_line_protocol()


async def test_store_keeps_going_when_influxdb_is_not_reachable():
    # Nothing listens on port 1: every write fails. That must never stop the controller.
    store = MeasurementStore("http://127.0.0.1:1", "not-a-real-token", "depot", "depot")
    await store.write([price_point(82.36, RUN, NOW)])
    assert store.failed_writes == 1
    await store.close()


# ---------------------------------------------------------------------------
# Grafana dashboard and provisioning
# ---------------------------------------------------------------------------

def written_fields():
    """Out: every field name the controller writes, per measurement (dict of sets)."""
    # A run name without spaces, so each line splits cleanly into
    # "measurement,tags", "fields" and "time" at its two spaces.
    run = "test-run"
    sample = {"time": NOW, "building_kw": 1.0, "charging_kw": 1.0, "site_kw": 1.0, "estimated_site_kw": 1.0}
    points = [site_point(sample, run, 150.0, 135.0), price_point(1.0, run, NOW)]
    points += charger_points({"CP01": make_charger("V01", 50.0, 1.0, 1.0)}, run, NOW)

    fields = {}
    for point in points:
        measurement_and_tags, field_part, _time = point.to_line_protocol().split(" ")
        measurement = measurement_and_tags.split(",")[0]
        for field in field_part.split(","):
            fields.setdefault(measurement, set()).add(field.split("=")[0])
    return fields


def test_dashboard_only_asks_for_fields_the_controller_writes():
    dashboard = json.loads((REPOSITORY / "grafana" / "dashboards" / "depot_charging.json").read_text())
    fields = written_fields()
    assert len(dashboard["panels"]) == 5
    for panel in dashboard["panels"]:
        query = panel["targets"][0]["query"]
        measurement = re.search(r'r._measurement == "(\w+)"', query).group(1)
        for field in re.findall(r'r._field == "(\w+)"', query):
            assert field in fields[measurement], f"panel '{panel['title']}' asks for {measurement}.{field}"


def test_dashboard_uses_the_provisioned_data_source():
    dashboard = json.loads((REPOSITORY / "grafana" / "dashboards" / "depot_charging.json").read_text())
    provisioning = (REPOSITORY / "grafana" / "provisioning" / "datasources" / "influxdb.yml").read_text()
    assert "uid: depot-influxdb" in provisioning
    for panel in dashboard["panels"]:
        assert panel["datasource"]["uid"] == "depot-influxdb"


def test_no_secret_is_written_in_the_provisioning_file():
    provisioning = (REPOSITORY / "grafana" / "provisioning" / "datasources" / "influxdb.yml").read_text()
    # The token is filled in from the environment when Grafana starts.
    assert "token: ${INFLUXDB_TOKEN}" in provisioning


def test_env_file_is_kept_out_of_git():
    ignored = (REPOSITORY / ".gitignore").read_text().splitlines()
    assert ".env" in ignored
    example = (REPOSITORY / ".env.example").read_text()
    # The example only has placeholders for the secrets.
    for name in ["INFLUXDB_TOKEN", "INFLUXDB_ADMIN_PASSWORD", "GRAFANA_ADMIN_PASSWORD"]:
        assert re.search(rf"^{name}=change-me", example, re.MULTILINE), name


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

def api_client(controller):
    """An HTTP client that talks to the FastAPI app directly, without a real network port."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(controller)), base_url="http://test")


async def test_status_shows_the_site():
    async with api_client(make_controller()) as client:
        status = (await client.get("/status")).json()
    assert status["site_limit_kw"] == 150.0
    assert status["planning_target_kw"] == 135.0
    assert status["building_kw"] == 22.0
    assert status["site_kw"] == 44.0
    assert status["strategy"] == "smart"


async def test_chargers_and_vans_are_listed():
    async with api_client(make_controller()) as client:
        chargers = (await client.get("/chargers")).json()
        vans = (await client.get("/vans")).json()
    assert [charger["charger_id"] for charger in chargers] == ["CP01", "CP02"]
    assert chargers[0]["van_id"] == "V01" and chargers[0]["power_kw"] == 22.0
    assert vans == [{"van_id": "V01", "charger_id": "CP01", "state_of_charge_percent": 55.0,
                     "departure_time": NOW.replace(hour=6).isoformat(), "planned_shortfall_kwh": 0.0}]


async def test_site_limit_can_be_changed():
    controller = make_controller()
    async with api_client(controller) as client:
        response = await client.put("/site-limit", json={"site_limit_kw": 120.0})
    assert response.status_code == 200
    assert response.json() == {"old_site_limit_kw": 150.0, "site_limit_kw": 120.0, "planning_target_kw": 105.0}
    assert controller.site_limit_kw == 120.0


@pytest.mark.parametrize("bad_value", [5.0, 1000.0, "a lot"])
async def test_site_limit_outside_the_allowed_range_is_refused(bad_value):
    controller = make_controller()
    async with api_client(controller) as client:
        response = await client.put("/site-limit", json={"site_limit_kw": bad_value})
    # 422 = the request was understood but its content is not allowed.
    assert response.status_code == 422
    assert controller.site_limit_kw == 150.0


async def test_a_lower_limit_from_the_api_is_seen_by_the_safety_check_at_once():
    controller = make_controller()
    assert controller.estimated_site_kw() <= controller.target_kw()
    async with api_client(controller) as client:
        await client.put("/site-limit", json={"site_limit_kw": 50.0})
    # 22 kW building + 22 kW charging = 44 kW, above the new target of 50 - 15 = 35 kW:
    # the next control cycle starts a safety plan.
    assert controller.estimated_site_kw() > controller.target_kw()



def test_measurements_are_stored_only_until_the_night_is_over():
    controller = make_controller()
    # Without a store nothing is stored.
    assert controller.should_store_measurements() is False

    controller.storage = object()
    assert controller.should_store_measurements() is True

    # All chargers gone and the summary printed: the night is over.
    controller.summary_printed = True
    assert controller.should_store_measurements() is False
