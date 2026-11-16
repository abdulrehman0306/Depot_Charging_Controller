"""
Shared test helpers. pytest loads this file by itself; every test can ask
for the "csms" fixture by naming it as an argument.
"""

import asyncio

import pytest

import settings
from src.controller.central_system import start_csms_server
from src.controller.meter_reader import create_meter_client
from src.meter.modbus_meter import SimulatedMeter, start_meter_server
from src.sim_clock import SimulationClock

# Run the simulation very fast in tests: one simulated minute = 3 real milliseconds.
TEST_SIMULATION_SPEED = 20000


@pytest.fixture
async def csms():
    """
    Start a controller on a free port with its own fast simulated clock,
    and stop it after the test.

    Gives a dict with: port, chargers (connected chargers by id), clock, site_log.
    """
    clock = SimulationClock(settings.SIMULATION_START_TIME, TEST_SIMULATION_SPEED)
    connected_chargers = {}
    site_log = []

    # 127.0.0.1 and port 0: let the operating system pick any free port.
    server = await start_csms_server("127.0.0.1", 0, connected_chargers, clock, site_log)
    port = server.sockets[0].getsockname()[1]
    yield {"port": port, "chargers": connected_chargers, "clock": clock, "site_log": site_log}
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
async def running_meter(meter):
    """Same as the meter fixture, but the meter keeps its registers up to date by itself, like a real one."""
    update_task = asyncio.create_task(meter["meter"].run())
    yield meter
    update_task.cancel()
