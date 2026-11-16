"""
Shared test helpers. pytest loads this file by itself; every test can ask
for a fixture (csms, meter) by naming it as an argument. run_night and
run_scenario_night run the whole system for one night.
"""

import asyncio
import sys

import pytest

import settings
from src.chargers.run_chargers import run_all_chargers
from src.controller.central_system import start_csms_server
from src.controller.meter_reader import create_meter_client
from src.controller.run_controller import default_limit_for_strategy
from src.controller.site_control import SiteController
from src.meter.modbus_meter import SimulatedMeter, start_meter_server
from src.results import true_site_log
from src.scenarios import get_scenario
from src.sim_clock import SimulationClock

# Run the simulation very fast in tests: one simulated minute = 6 real milliseconds.
TEST_SIMULATION_SPEED = 3000

# On Windows a short sleep lasts at least about 15 ms by default, which at test speed
# is several simulated minutes: the controller would then only look at the site every
# few simulated minutes. Asking Windows for a 1 ms timer fixes that. Linux does not need it.
if sys.platform == "win32":
    import ctypes
    ctypes.windll.winmm.timeBeginPeriod(1)


@pytest.fixture
async def csms():
    """
    Start a controller OCPP server on a free port with its own fast simulated clock,
    and stop it after the test. No control loop runs: the test decides what to send.

    Gives a dict with: port, chargers (connected chargers by id), clock.
    """
    clock = SimulationClock(settings.SIMULATION_START_TIME, TEST_SIMULATION_SPEED)
    connected_chargers = {}

    # 127.0.0.1 and port 0: let the operating system pick any free port.
    server = await start_csms_server("127.0.0.1", 0, connected_chargers, clock)
    port = server.sockets[0].getsockname()[1]
    yield {"port": port, "chargers": connected_chargers, "clock": clock}
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


async def run_night(vans, load_spikes, strategy_name, site_limit_kw=settings.SITE_LIMIT_KW,
                    simulation_speed=TEST_SIMULATION_SPEED, start_time=settings.SIMULATION_START_TIME):
    """
    Run the whole system for one night: meter, controller with its control loop, and one charger per van.
    It ends when the last van has left.

    In: list of Van, list of LoadSpike, strategy name, site limit [kW], simulation speed, start time.
    Out: dict with chargers (SimulatedCharger list), controller (SiteController),
         and true_site_log (the real site power, minute by minute).
    """
    # The meter, with the scenario's load spikes. One update first, so the
    # controller's first reading is not an empty register table.
    meter_server, meter_port = await start_meter_server("127.0.0.1", 0)
    simulated_meter = SimulatedMeter(meter_server, SimulationClock(start_time, simulation_speed), load_spikes)
    await simulated_meter.update_registers()
    meter_task = asyncio.create_task(simulated_meter.run())
    meter_client = create_meter_client("127.0.0.1", meter_port)
    await meter_client.connect()

    # The controller: OCPP server and control loop.
    clock = SimulationClock(start_time, simulation_speed)
    connected_chargers = {}
    csms_server = await start_csms_server("127.0.0.1", 0, connected_chargers, clock,
                                          default_limit_for_strategy(strategy_name))
    csms_port = csms_server.sockets[0].getsockname()[1]
    controller = SiteController(connected_chargers, clock, meter_client, strategy_name, site_limit_kw=site_limit_kw)
    controller_task = asyncio.create_task(controller.run())

    # The chargers, until every van has left.
    chargers = await run_all_chargers(vans, "127.0.0.1", csms_port, simulation_speed)

    controller_task.cancel()
    meter_task.cancel()
    meter_client.close()
    csms_server.close()
    await csms_server.wait_closed()
    await meter_server.shutdown()

    last_departure = max(van.departure_time for van in vans)
    real_site = true_site_log([charger.power_log for charger in chargers], load_spikes, start_time, last_departure)
    return {"chargers": chargers, "controller": controller, "true_site_log": real_site}


# A full night takes a few seconds, and several tests look at the same night.
# Each night is run once and the result kept here for the other tests.
_finished_nights = {}


async def run_scenario_night(scenario_name, strategy_name, site_limit_kw=settings.SITE_LIMIT_KW):
    """
    Run (or reuse) one night of a named scenario.

    In: scenario name, strategy name, site limit [kW].
    Out: the dict from run_night, plus "vans" and "load_spikes".
    """
    key = (scenario_name, strategy_name, site_limit_kw)
    if key not in _finished_nights:
        scenario = get_scenario(scenario_name)
        night = await run_night(scenario["vans"], scenario["load_spikes"], strategy_name, site_limit_kw)
        night["vans"] = scenario["vans"]
        night["load_spikes"] = scenario["load_spikes"]
        _finished_nights[key] = night
    return _finished_nights[key]
