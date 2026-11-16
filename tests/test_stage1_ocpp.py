"""
Stage 1 tests: one charger and the controller talking OCPP 2.0.1.

The first tests check small calculation functions without any network.
The later ones start a real controller on this computer and connect a
simulated charger to it, so the full OCPP conversation is tested.
"""

import asyncio

import pytest
from ocpp.v201 import call
from ocpp.v201.enums import ConnectorStatusEnumType
from websockets.asyncio.client import connect

import settings
from src.chargers.simulated_charger import SimulatedCharger, energy_after_step_wh, run_demo_session
from src.controller.central_system import (
    build_charging_profile,
    planning_loop,
    read_power_and_energy,
    start_csms_server,
)

# Run the simulation very fast in tests: one simulated minute = 0.01 real seconds.
FAST_SIMULATION_SPEED = 6000
FAST_PLANNING_INTERVAL_S = settings.PLANNING_INTERVAL_MINUTES * 60 / FAST_SIMULATION_SPEED


@pytest.fixture
async def csms():
    """Start a controller on a free port. Gives (port, dict of connected chargers), and stops it after the test."""
    connected_chargers = {}
    # 127.0.0.1 and port 0: let the operating system pick any free port.
    server = await start_csms_server("127.0.0.1", 0, connected_chargers)
    port = server.sockets[0].getsockname()[1]
    yield port, connected_chargers
    server.close()
    await server.wait_closed()


# ---------------------------------------------------------------------------
# Calculations, no network
# ---------------------------------------------------------------------------

def test_energy_counter_adds_power_times_time():
    # 22 kW for one hour is 22 kWh = 22000 Wh.
    assert energy_after_step_wh(0.0, 22.0, 60) == pytest.approx(22000.0)
    # 11 kW for 15 minutes is 2.75 kWh, added to a meter already at 1000 Wh.
    assert energy_after_step_wh(1000.0, 11.0, 15) == pytest.approx(3750.0)


def test_charging_profile_is_in_watts_for_the_given_transaction():
    profile = build_charging_profile("tx-1", 11.0, "2025-01-15T17:00:00+01:00")
    schedule = profile["charging_schedule"][0]

    assert profile["transaction_id"] == "tx-1"
    assert profile["charging_profile_purpose"] == "TxProfile"
    assert schedule["charging_rate_unit"] == "W"
    assert schedule["charging_schedule_period"][0]["limit"] == 11000.0


def test_meter_reading_is_converted_to_kw_and_wh():
    readings = [{
        "timestamp": "2025-01-15T17:05:00+01:00",
        "sampled_value": [
            {"value": 11000.0, "measurand": "Power.Active.Import", "unit_of_measure": {"unit": "W"}},
            {"value": 2.5, "measurand": "Energy.Active.Import.Register", "unit_of_measure": {"unit": "kWh"}},
        ],
    }]
    power_kw, energy_wh = read_power_and_energy(readings)
    assert power_kw == pytest.approx(11.0)
    assert energy_wh == pytest.approx(2500.0)


def test_charger_rejects_a_limit_when_no_van_is_charging():
    # The charger object is created without a network connection: we only call its handler directly.
    charger = SimulatedCharger("CP01", connection=None)
    profile = build_charging_profile("tx-1", 11.0, "2025-01-15T17:00:00+01:00")

    response = charger.on_set_charging_profile(evse_id=1, charging_profile=profile)

    assert response.status == "Rejected"
    assert charger.power_limit_kw == settings.CHARGER_MAX_POWER_KW


def test_charger_accepts_a_limit_for_its_own_transaction():
    charger = SimulatedCharger("CP01", connection=None)
    charger.transaction_id = "tx-1"
    profile = build_charging_profile("tx-1", 7.4, "2025-01-15T17:00:00+01:00")

    response = charger.on_set_charging_profile(evse_id=1, charging_profile=profile)

    assert response.status == "Accepted"
    assert charger.power_limit_kw == pytest.approx(7.4)


# ---------------------------------------------------------------------------
# Full OCPP conversation over a real WebSocket
# ---------------------------------------------------------------------------

async def test_boot_heartbeat_and_status_reach_the_controller(csms):
    port, connected_chargers = csms
    address = f"ws://127.0.0.1:{port}/CP01"

    async with connect(address, subprotocols=[settings.OCPP_SUBPROTOCOL]) as websocket:
        charger = SimulatedCharger("CP01", websocket)
        listener_task = asyncio.create_task(charger.start())

        heartbeat_interval_s = await charger.boot()
        await charger.call(call.Heartbeat())
        await charger.send_status(ConnectorStatusEnumType.available)

        controller_view = connected_chargers["CP01"]
        assert heartbeat_interval_s == settings.HEARTBEAT_INTERVAL_S
        assert controller_view.last_heartbeat_utc is not None
        assert controller_view.connector_status == "Available"
        listener_task.cancel()


async def test_controller_follows_the_transaction_from_start_to_end(csms):
    port, connected_chargers = csms
    address = f"ws://127.0.0.1:{port}/CP01"

    async with connect(address, subprotocols=[settings.OCPP_SUBPROTOCOL]) as websocket:
        charger = SimulatedCharger("CP01", websocket, simulation_speed=FAST_SIMULATION_SPEED)
        listener_task = asyncio.create_task(charger.start())
        await charger.boot()
        controller_view = connected_chargers["CP01"]

        await charger.plug_in_van()
        assert controller_view.connector_status == "Occupied"
        assert controller_view.transaction_id == charger.transaction_id

        await charger.charge_for(10)
        # 22 kW for 10 minutes = 3667 Wh, and the controller saw the same number.
        assert controller_view.charge_power_kw == pytest.approx(22.0)
        assert controller_view.energy_meter_wh == pytest.approx(22000 * 10 / 60, abs=0.1)

        await charger.unplug_van()
        assert controller_view.transaction_id is None
        assert controller_view.connector_status == "Available"
        listener_task.cancel()


async def test_charger_slows_down_to_the_limit_from_the_controller(csms):
    port, connected_chargers = csms
    planning_task = asyncio.create_task(planning_loop(connected_chargers, 11.0, FAST_PLANNING_INTERVAL_S))

    charger = await run_demo_session("CP01", "127.0.0.1", port, charge_minutes=60,
                                     simulation_speed=FAST_SIMULATION_SPEED)
    planning_task.cancel()

    powers_kw = [power_kw for _, power_kw in charger.power_log]
    # Full power before the first plan arrives, 11 kW once it has arrived.
    assert powers_kw[0] == pytest.approx(22.0)
    assert powers_kw[-1] == pytest.approx(11.0)

    # The meter must agree with the power that was really drawn, minute by minute.
    expected_energy_wh = sum(powers_kw) / 60 * 1000
    assert charger.energy_meter_wh == pytest.approx(expected_energy_wh)


async def test_charger_never_goes_above_its_hardware_limit(csms):
    port, connected_chargers = csms
    # The controller asks for more than the charger can give.
    planning_task = asyncio.create_task(planning_loop(connected_chargers, 50.0, FAST_PLANNING_INTERVAL_S))

    charger = await run_demo_session("CP01", "127.0.0.1", port, charge_minutes=60,
                                     simulation_speed=FAST_SIMULATION_SPEED)
    planning_task.cancel()

    assert max(power_kw for _, power_kw in charger.power_log) <= settings.CHARGER_MAX_POWER_KW
