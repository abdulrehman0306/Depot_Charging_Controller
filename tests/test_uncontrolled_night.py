"""
A full night with ten chargers, the Modbus building meter, and uncontrolled
charging, run very fast.

This is the reason the project exists: without control, the depot goes
over its grid connection limit.
"""

import asyncio

import pytest

import settings
from src.chargers.run_chargers import run_all_chargers
from src.controller import strategies
from src.controller.central_system import planning_loop, site_monitor_loop
from src.meter.building_load import building_load_kw
from src.results import peak_site_power
from src.scenarios import generate_normal_evening
from tests.conftest import TEST_SIMULATION_SPEED


@pytest.fixture
async def uncontrolled_night(csms, running_meter):
    """Run one whole night of the normal evening scenario. Gives (chargers, vans, site log)."""
    planning_task = asyncio.create_task(planning_loop(csms["chargers"], strategies.uncontrolled, csms["clock"]))
    monitor_task = asyncio.create_task(site_monitor_loop(csms["chargers"], csms["clock"], csms["site_log"],
                                                         running_meter["client"]))

    vans = generate_normal_evening()
    chargers = await run_all_chargers(vans, "127.0.0.1", csms["port"], TEST_SIMULATION_SPEED)

    planning_task.cancel()
    monitor_task.cancel()
    return chargers, vans, csms["site_log"]


async def test_uncontrolled_charging_goes_over_the_site_limit(uncontrolled_night):
    chargers, vans, site_log = uncontrolled_night
    peak_kw, _ = peak_site_power(site_log)
    assert peak_kw > settings.SITE_LIMIT_KW


async def test_building_load_comes_from_the_meter(uncontrolled_night):
    chargers, vans, site_log = uncontrolled_night
    # The controller only knows the building load through Modbus. It must match the
    # synthetic profile, except for a short delay around each hour change.
    matching = [sample for sample in site_log if sample["building_kw"] == building_load_kw(sample["time"])]
    assert len(matching) / len(site_log) > 0.9


async def test_no_charger_draws_more_than_22_kw(uncontrolled_night):
    chargers, vans, site_log = uncontrolled_night
    for charger in chargers:
        assert max(power_kw for _, power_kw in charger.power_log) <= settings.CHARGER_MAX_POWER_KW


async def test_state_of_charge_rises_with_the_energy_delivered(uncontrolled_night):
    chargers, vans, site_log = uncontrolled_night
    for charger, van in zip(chargers, vans):
        # Energy into the battery = grid energy x efficiency. Turn it into percent of the battery.
        grid_energy_kwh = charger.energy_meter_wh / 1000
        expected_rise_percent = grid_energy_kwh * settings.CHARGING_EFFICIENCY / settings.VAN_BATTERY_USABLE_KWH * 100

        # 100 % at the end: with 9+ hours parked and full power, every van fills up.
        assert van.arrival_state_of_charge_percent + expected_rise_percent == pytest.approx(100.0)
