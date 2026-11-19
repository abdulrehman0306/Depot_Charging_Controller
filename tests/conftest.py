"""
Shared test helpers. pytest loads this file by itself; every test can ask
for a fixture (csms, meter, mqtt_topic_prefix) by naming it as an argument. run_night and
run_scenario_night run the whole system for one night.
"""

import os
import socket
import uuid

import pytest

import settings
from src import simulation
from src.controller.central_system import start_csms_server
from src.controller.meter_reader import create_meter_client
from src.meter.modbus_meter import SimulatedMeter, start_meter_server
from src.prices import load_prices
from src.scenarios import get_scenario
from src.sim_clock import SimulationClock

# The real day-ahead prices, read once for all tests.
PRICES = load_prices()

# Simulation speeds of the test nights (see src/simulation.py for why the smart
# controller runs slower). Importing src.simulation also sets the 1 ms timer on Windows.
TEST_SIMULATION_SPEED = simulation.FAST_SIMULATION_SPEED
SMART_TEST_SIMULATION_SPEED = simulation.SMART_SIMULATION_SPEED


@pytest.fixture
async def csms():
    """
    Start a controller OCPP server on a free port with its own fast simulated clock,
    and stop it after the test. No control loop runs: the test decides what to send.

    Gives a dict with: port, chargers (connected chargers by id), clock.
    """
    clock = SimulationClock(settings.SIMULATION_START_TIME, TEST_SIMULATION_SPEED)
    chargers_by_id = {}

    # 127.0.0.1 and port 0: let the operating system pick any free port.
    server = await start_csms_server("127.0.0.1", 0, chargers_by_id, clock)
    port = server.sockets[0].getsockname()[1]
    yield {"port": port, "chargers": chargers_by_id, "clock": clock}
    server.close()
    await server.wait_closed()


@pytest.fixture
async def meter():
    """
    Start a simulated Modbus meter on a free port, with a Modbus client connected to it.
    The meter does NOT update by itself; tests call meter.update_registers() when they want.

    Gives a dict with: meter (SimulatedMeter), client (Modbus client), port.
    """
    server, port = await start_meter_server("127.0.0.1", 0)
    simulated_meter = SimulatedMeter(server, SimulationClock(settings.SIMULATION_START_TIME, TEST_SIMULATION_SPEED))
    client = create_meter_client("127.0.0.1", port)
    await client.connect()
    yield {"meter": simulated_meter, "client": client, "port": port}
    client.close()
    await server.shutdown()


@pytest.fixture
def mqtt_topic_prefix():
    """
    For tests that need the MQTT broker (Mosquitto at settings.MQTT_HOST:MQTT_PORT).

    If no broker is running, the test is skipped, with how to start one. In GitHub Actions
    (MQTT_BROKER_REQUIRED=1) the test fails instead, so it can never be skipped there unseen.

    Gives a topic prefix of this test's own, so tests never mix with each other or with
    a depot running on the same broker.
    """
    try:
        socket.create_connection((settings.MQTT_HOST, settings.MQTT_PORT), timeout=2).close()
    except OSError:
        message = (f"no MQTT broker at {settings.MQTT_HOST}:{settings.MQTT_PORT}; "
                   "start one with: docker compose up -d mqtt")
        if os.environ.get("MQTT_BROKER_REQUIRED") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return f"test-{uuid.uuid4().hex[:12]}"


# A full night takes a few seconds, and several tests look at the same night.
# Each night is run once and the result kept here for the other tests.
_finished_nights = {}


async def run_scenario_night(scenario_name, strategy_name, site_limit_kw=settings.SITE_LIMIT_KW):
    """
    Run (or reuse) one night of a named scenario, on the scenario's own night.

    In: scenario name, strategy name, site limit [kW].
    Out: the dict from src/simulation.run_night, plus "vans" and "load_spikes".
    """
    key = (scenario_name, strategy_name, site_limit_kw)
    if key not in _finished_nights:
        _finished_nights[key] = await simulation.run_scenario_night(get_scenario(scenario_name), strategy_name,
                                                                    PRICES, site_limit_kw)
    return _finished_nights[key]


async def run_night(vans, load_spikes, strategy_name, site_limit_kw=settings.SITE_LIMIT_KW,
                    simulation_speed=TEST_SIMULATION_SPEED, start_time=settings.SIMULATION_START_TIME,
                    charger_outages=(), building_load_source="modbus", mqtt_topic_prefix=settings.MQTT_TOPIC_PREFIX):
    """The whole system for one night with the test prices (see src/simulation.run_night)."""
    return await simulation.run_night(vans, load_spikes, strategy_name, PRICES, site_limit_kw, simulation_speed,
                                      start_time, charger_outages, building_load_source, mqtt_topic_prefix)
