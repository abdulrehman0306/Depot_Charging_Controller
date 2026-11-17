"""
Start the central controller: the OCPP server for the chargers and the
control loop that reads the meter and decides the power limits.

The controller owns the simulated clock. Chargers set their clocks from it
over OCPP, and the meter through its clock registers.

Start the meter first, then run:
    python -m src.controller.run_controller
"""

import asyncio
import logging

import settings
from src.controller.central_system import start_csms_server
from src.controller.meter_reader import create_meter_client
from src.controller.site_control import SiteController
from src.log_setup import setup_logging
from src.prices import load_prices
from src.scenarios import get_scenario
from src.sim_clock import SimulationClock


def default_limit_for_strategy(strategy_name):
    """
    In: strategy name.
    Out: the current [A] new transactions start with: None for uncontrolled
         (no limit, full power, as without any software), 0 A otherwise
         (a new van waits until the controller gives it a share).
    """
    if strategy_name == "uncontrolled":
        return None
    return 0.0


def fleet_schedule_from_vans(vans):
    """
    In: list of Van. Out: dict van id -> departure time.

    In a real depot this comes from the route planning system: which van leaves when.
    """
    schedule = {}
    for van in vans:
        schedule[van.van_id] = van.departure_time
    return schedule


async def run_controller():
    """Start the server and the control loop with the values from settings.py. Runs forever."""
    scenario = get_scenario(settings.SCENARIO)
    clock = SimulationClock(scenario["start_time"], settings.SIMULATED_SECONDS_PER_REAL_SECOND)
    chargers_by_id = {}
    meter_client = create_meter_client(settings.METER_HOST, settings.METER_PORT)

    await start_csms_server(settings.CSMS_HOST, settings.CSMS_PORT, chargers_by_id, clock,
                            default_limit_for_strategy(settings.STRATEGY))
    events = logging.getLogger("CSMS")
    events.info("listening on ws://%s:%s, strategy: %s, scenario: %s (night of %s), simulation speed: %sx",
                settings.CSMS_HOST, settings.CSMS_PORT, settings.STRATEGY, scenario["name"],
                scenario["night_date"], settings.SIMULATED_SECONDS_PER_REAL_SECOND)
    events.info("reading building meter at %s:%s over Modbus TCP", settings.METER_HOST, settings.METER_PORT)

    controller = SiteController(chargers_by_id, clock, meter_client, settings.STRATEGY,
                                scenario_name=scenario["name"],
                                fleet_schedule=fleet_schedule_from_vans(scenario["vans"]),
                                prices=load_prices())
    await controller.run()


def main():
    setup_logging()
    asyncio.run(run_controller())


if __name__ == "__main__":
    main()
