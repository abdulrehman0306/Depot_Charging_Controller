"""
Tests of the OCPP 2.0.1 conversation between one charger and the controller.

The first tests call single functions without any network. The later ones
start a real controller on this computer and connect a simulated charger.
"""

import asyncio
from datetime import timedelta

import pytest
from ocpp.v201 import call
from ocpp.v201.enums import ConnectorStatusEnumType
from websockets.asyncio.client import connect

import settings
from src.chargers.simulated_charger import SimulatedCharger, run_charger
from src.chargers.van import Van
from src.controller.central_system import (
    build_charging_profile,
    build_default_profile,
    read_meter_values,
    send_power_limit,
)
from src.sim_clock import SimulationClock
from tests.conftest import TEST_SIMULATION_SPEED

START = settings.SIMULATION_START_TIME


def make_test_van(clock):
    """A van that arrives one simulated minute from now, with 20 %, and stays two hours."""
    now = clock.now()
    return Van("V01", now + timedelta(minutes=1), now + timedelta(minutes=121), 20.0)


# ---------------------------------------------------------------------------
# Single functions, no network
# ---------------------------------------------------------------------------

def test_charging_profile_is_in_amps_for_the_given_transaction():
    profile = build_charging_profile("tx-1", 11.0, START.isoformat())
    schedule = profile["charging_schedule"][0]
    period = schedule["charging_schedule_period"][0]

    assert profile["transaction_id"] == "tx-1"
    assert profile["charging_profile_purpose"] == "TxProfile"
    assert schedule["charging_rate_unit"] == "A"
    # 11 kW / (3 x 230 V) = 15.94 A, rounded down to 15.9 A.
    assert period["limit"] == 15.9
    assert period["number_phases"] == 3


def test_meter_reading_is_converted_to_kw_wh_and_percent():
    readings = [{
        "timestamp": START.isoformat(),
        "sampled_value": [
            {"value": 11000.0, "measurand": "Power.Active.Import", "unit_of_measure": {"unit": "W"}},
            {"value": 2.5, "measurand": "Energy.Active.Import.Register", "unit_of_measure": {"unit": "kWh"}},
            {"value": 47.5, "measurand": "SoC", "unit_of_measure": {"unit": "Percent"}},
        ],
    }]
    values = read_meter_values(readings)
    assert values["power_kw"] == pytest.approx(11.0)
    assert values["energy_wh"] == pytest.approx(2500.0)
    assert values["state_of_charge_percent"] == pytest.approx(47.5)


def test_charger_rejects_a_limit_when_no_van_is_charging():
    # The charger object is created without a network connection: we only call its handler directly.
    charger = SimulatedCharger("CP01", None, SimulationClock(START, 60))
    profile = build_charging_profile("tx-1", 11.0, START.isoformat())

    response = charger.on_set_charging_profile(evse_id=1, charging_profile=profile)

    assert response.status == "Rejected"
    assert charger.power_limit_kw == settings.CHARGER_MAX_POWER_KW


def test_charger_turns_the_amps_limit_back_into_kw():
    charger = SimulatedCharger("CP01", None, SimulationClock(START, 60))
    charger.transaction_id = "tx-1"
    # 11.04 kW is exactly 16 A on three phases at 230 V.
    profile = build_charging_profile("tx-1", 11.04, START.isoformat())

    response = charger.on_set_charging_profile(evse_id=1, charging_profile=profile)

    assert response.status == "Accepted"
    assert charger.power_limit_kw == pytest.approx(11.04)


def test_charger_treats_a_limit_below_6_amps_as_pause():
    charger = SimulatedCharger("CP01", None, SimulationClock(START, 60))
    charger.transaction_id = "tx-1"
    # Built by hand, because our controller would never send 4 A.
    profile = build_charging_profile("tx-1", 11.0, START.isoformat())
    profile["charging_schedule"][0]["charging_schedule_period"][0]["limit"] = 4.0

    response = charger.on_set_charging_profile(evse_id=1, charging_profile=profile)

    assert response.status == "Accepted"
    assert charger.power_limit_kw == 0.0


# ---------------------------------------------------------------------------
# Full OCPP conversation over a real WebSocket
# ---------------------------------------------------------------------------

async def test_boot_heartbeat_and_status_reach_the_controller(csms):
    address = f"ws://127.0.0.1:{csms['port']}/CP01"

    async with connect(address, subprotocols=[settings.OCPP_SUBPROTOCOL]) as websocket:
        charger = SimulatedCharger("CP01", websocket, SimulationClock(START, TEST_SIMULATION_SPEED))
        listener_task = asyncio.create_task(charger.start())

        heartbeat_interval_s = await charger.boot()
        await charger.call(call.Heartbeat())
        await charger.send_status(ConnectorStatusEnumType.available)

        controller_view = csms["chargers"]["CP01"]
        assert heartbeat_interval_s == settings.HEARTBEAT_INTERVAL_S
        assert controller_view.last_heartbeat_time is not None
        assert controller_view.connector_status == "Available"
        listener_task.cancel()


async def test_boot_sets_the_charger_clock_to_the_controller_clock(csms):
    address = f"ws://127.0.0.1:{csms['port']}/CP01"

    async with connect(address, subprotocols=[settings.OCPP_SUBPROTOCOL]) as websocket:
        # Start the charger's clock on purpose 5 hours wrong.
        charger_clock = SimulationClock(START + timedelta(hours=5), TEST_SIMULATION_SPEED)
        charger = SimulatedCharger("CP01", websocket, charger_clock)
        listener_task = asyncio.create_task(charger.start())

        await charger.boot()

        # After boot both clocks agree, apart from the few milliseconds the message took.
        difference = abs(charger_clock.now() - csms["clock"].now())
        assert difference < timedelta(minutes=2)
        listener_task.cancel()


async def test_controller_follows_the_transaction_from_start_to_end(csms):
    address = f"ws://127.0.0.1:{csms['port']}/CP01"

    async with connect(address, subprotocols=[settings.OCPP_SUBPROTOCOL]) as websocket:
        charger = SimulatedCharger("CP01", websocket, SimulationClock(START, TEST_SIMULATION_SPEED))
        listener_task = asyncio.create_task(charger.start())
        await charger.boot()
        controller_view = csms["chargers"]["CP01"]

        await charger.plug_in_van(make_test_van(charger.clock))
        assert controller_view.connector_status == "Occupied"
        assert controller_view.transaction_id == charger.transaction_id
        assert controller_view.state_of_charge_percent == pytest.approx(20.0)

        await charger.unplug_van()
        assert controller_view.transaction_id is None
        assert controller_view.connector_status == "Available"
        listener_task.cancel()


async def run_charger_with_one_limit(csms, limit_kw):
    """
    Run one charger with a two hour van visit. Once the van has charged for ten
    simulated minutes at full power, the controller sends it one limit.

    In: the csms fixture dict, the limit to send [kW].
    Out: (SimulatedCharger after the visit, the controller's ChargerConnection for it).
    """
    charger_task = asyncio.create_task(run_charger("CP01", make_test_van(csms["clock"]), "127.0.0.1",
                                                   csms["port"], TEST_SIMULATION_SPEED))
    # Wait until the controller sees the van charging, then ten more simulated minutes.
    while "CP01" not in csms["chargers"] or csms["chargers"]["CP01"].transaction_id is None:
        await asyncio.sleep(0.001)
    controller_view = csms["chargers"]["CP01"]
    await asyncio.sleep(csms["clock"].real_seconds(10))

    await send_power_limit(controller_view, limit_kw)
    charger = await charger_task
    return charger, controller_view


async def test_charger_slows_down_to_the_limit_from_the_controller(csms):
    charger, _ = await run_charger_with_one_limit(csms, 11.0)

    powers_kw = [step["power_kw"] for step in charger.power_log]
    # Full power before the limit arrives (this test server sends no default limit).
    assert powers_kw[0] == pytest.approx(22.0)
    # After it: 15.9 A x 3 x 230 V = 10.97 kW. Slightly under 11 kW because amps are rounded down.
    assert powers_kw[-1] == pytest.approx(10.971)
    assert powers_kw[-1] <= 11.0


async def test_plan_below_6_amps_is_sent_as_pause(csms):
    # 3 kW would be 4.3 A, which a charger cannot offer. The controller must send 0 A instead.
    charger, controller_view = await run_charger_with_one_limit(csms, 3.0)

    assert [limit_a for _, limit_a in controller_view.sent_limits_a] == [0.0]
    # Once paused, the van draws nothing.
    assert charger.power_log[-1]["power_kw"] == 0.0


async def test_charger_never_goes_above_its_hardware_limit(csms):
    # The controller asks for more than the charger can give.
    charger, _ = await run_charger_with_one_limit(csms, 50.0)
    assert max(step["power_kw"] for step in charger.power_log) <= settings.CHARGER_MAX_POWER_KW


async def test_default_profile_makes_a_new_van_wait(csms):
    address = f"ws://127.0.0.1:{csms['port']}/CP01"
    async with connect(address, subprotocols=[settings.OCPP_SUBPROTOCOL]) as websocket:
        charger = SimulatedCharger("CP01", websocket, SimulationClock(START, TEST_SIMULATION_SPEED))
        listener_task = asyncio.create_task(charger.start())
        await charger.boot()

        # The controller tells the charger: new transactions start at 0 A.
        controller_view = csms["chargers"]["CP01"]
        await controller_view.call(call.SetChargingProfile(evse_id=1, charging_profile=build_default_profile(0.0)))
        await charger.plug_in_van(make_test_van(charger.clock))

        assert charger.power_limit_kw == 0.0
        listener_task.cancel()
