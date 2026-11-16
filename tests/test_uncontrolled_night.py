"""
A full night with ten chargers, the Modbus building meter, and uncontrolled
charging, run very fast.

This is the reason the project exists: without control, the depot goes
over its grid connection limit.
"""

import pytest

import settings
from src.meter.building_load import building_load_kw
from src.results import peak_site_power
from tests.conftest import run_scenario_night


async def test_uncontrolled_charging_goes_over_the_site_limit():
    night = await run_scenario_night("normal_evening", "uncontrolled")
    peak_kw, _ = peak_site_power(night["true_site_log"])
    assert peak_kw > settings.SITE_LIMIT_KW


async def test_uncontrolled_sends_no_limits():
    night = await run_scenario_night("normal_evening", "uncontrolled")
    assert night["controller"].sent_limits == []


async def test_building_load_comes_from_the_meter():
    night = await run_scenario_night("normal_evening", "uncontrolled")
    site_log = night["controller"].site_log
    # The controller only knows the building load through Modbus. It must match the
    # synthetic profile, except for a short delay around each hour change.
    matching = [sample for sample in site_log if sample["building_kw"] == building_load_kw(sample["time"])]
    assert len(matching) / len(site_log) > 0.9


async def test_state_of_charge_rises_with_the_energy_delivered():
    night = await run_scenario_night("normal_evening", "uncontrolled")
    for charger, van in zip(night["chargers"], night["vans"]):
        # Energy into the battery = grid energy x efficiency. Turn it into percent of the battery.
        grid_energy_kwh = charger.energy_meter_wh / 1000
        expected_rise_percent = grid_energy_kwh * settings.CHARGING_EFFICIENCY / settings.VAN_BATTERY_USABLE_KWH * 100

        # 100 % at the end: with 9+ hours parked and full power, every van fills up.
        assert van.arrival_state_of_charge_percent + expected_rise_percent == pytest.approx(100.0)
