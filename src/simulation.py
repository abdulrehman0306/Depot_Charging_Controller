"""
Run the whole system for one night inside one Python program: the Modbus meter, the
controller with its control loop, and one simulated charger per van. They talk to each
other over real WebSockets (OCPP) and Modbus TCP on this computer, exactly as the
separate programs do.

Used by the tests (tests/conftest.py) and by the results (src/run_week.py).
"""

import asyncio
import sys

import settings
from src.chargers.run_chargers import run_all_chargers
from src.controller.central_system import start_csms_server
from src.controller.meter_reader import create_meter_client
from src.controller.run_controller import default_limit_for_strategy, fleet_schedule_from_vans
from src.controller.site_control import SiteController
from src.meter.modbus_meter import SimulatedMeter, start_meter_server
from src.results import true_site_log
from src.sim_clock import SimulationClock

# Simulation speeds for whole nights in one program (simulated seconds per real second).
# 3000 runs a night in about 20 seconds.
FAST_SIMULATION_SPEED = 3000

# The smart controller's optimiser needs about 80 real milliseconds per plan. At 3000x
# that would be 4 simulated minutes in which new limits wait, which is far from reality
# (at the normal speed of 60x it is 5 simulated seconds). So nights with the smart
# controller run slower: one simulated minute = 60 real milliseconds, a plan = about 1 minute.
SMART_SIMULATION_SPEED = 1000

# On Windows a short sleep lasts at least about 15 ms by default, which at these speeds
# is several simulated minutes: the controller would then only look at the site every
# few simulated minutes. Asking Windows for a 1 ms timer fixes that. Linux does not need it.
if sys.platform == "win32":
    import ctypes
    ctypes.windll.winmm.timeBeginPeriod(1)


def simulation_speed_for(strategy_name):
    """In: strategy name. Out: the simulation speed its nights run at (see above)."""
    if strategy_name == "smart":
        return SMART_SIMULATION_SPEED
    return FAST_SIMULATION_SPEED


async def run_night(vans, load_spikes, strategy_name, prices, site_limit_kw=settings.SITE_LIMIT_KW,
                    simulation_speed=FAST_SIMULATION_SPEED, start_time=settings.SIMULATION_START_TIME,
                    charger_outages=()):
    """
    Run the whole system for one night. It ends when the last van has left.

    In: list of Van, list of LoadSpike, strategy name, day-ahead prices (pandas Series),
        site limit [kW], simulation speed, start time, charger outages (see src/scenarios.py).
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
    chargers_by_id = {}
    csms_server = await start_csms_server("127.0.0.1", 0, chargers_by_id, clock,
                                          default_limit_for_strategy(strategy_name))
    csms_port = csms_server.sockets[0].getsockname()[1]
    controller = SiteController(chargers_by_id, clock, meter_client, strategy_name, site_limit_kw=site_limit_kw,
                                fleet_schedule=fleet_schedule_from_vans(vans), prices=prices)
    controller_task = asyncio.create_task(controller.run())

    # The chargers, until every van has left.
    chargers = await run_all_chargers(vans, "127.0.0.1", csms_port, simulation_speed, charger_outages)

    # Stop the control loop and the meter, and wait until they have really stopped,
    # so the Modbus client is not closed in the middle of a reading.
    controller_task.cancel()
    meter_task.cancel()
    await asyncio.gather(controller_task, meter_task, return_exceptions=True)
    meter_client.close()
    csms_server.close()
    await csms_server.wait_closed()
    await meter_server.shutdown()

    last_departure = max(van.departure_time for van in vans)
    real_site = true_site_log([charger.power_log for charger in chargers], load_spikes, start_time, last_departure)
    return {"chargers": chargers, "controller": controller, "true_site_log": real_site}


async def run_scenario_night(scenario, strategy_name, prices, site_limit_kw=settings.SITE_LIMIT_KW):
    """
    Run one night of a scenario (a dict from src/scenarios.get_scenario).

    In: scenario dict, strategy name, prices, site limit [kW].
    Out: the dict from run_night, plus "vans" and "load_spikes".
    """
    night = await run_night(scenario["vans"], scenario["load_spikes"], strategy_name, prices, site_limit_kw,
                            simulation_speed=simulation_speed_for(strategy_name),
                            start_time=scenario["start_time"], charger_outages=scenario["charger_outages"])
    night["vans"] = scenario["vans"]
    night["load_spikes"] = scenario["load_spikes"]
    return night
