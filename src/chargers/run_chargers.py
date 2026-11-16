"""
Start all simulated chargers at once, each with one van from the scenario.

Start the controller first, then run:
    python -m src.chargers.run_chargers
"""

import asyncio
import logging

import settings
from src.chargers.simulated_charger import run_charger
from src.log_setup import setup_logging
from src.scenarios import generate_normal_evening


def log_van_schedule(vans):
    """Print the (synthetic) van schedule, one line per van."""
    schedule_log = logging.getLogger("SCHEDULE")
    schedule_log.info("synthetic van schedule (seed %s):", settings.RANDOM_SEED)
    for van in vans:
        schedule_log.info("%s arrives %s with %.1f %%, leaves %s",
                          van.van_id, van.arrival_time.strftime("%H:%M"),
                          van.arrival_state_of_charge_percent, van.departure_time.strftime("%a %H:%M"))


async def run_all_chargers(vans, host, port, simulation_speed=settings.SIMULATED_SECONDS_PER_REAL_SECOND):
    """
    Run one charger per van, all at the same time.

    In: list of Van, controller address and port, simulation speed.
    Out: list of SimulatedCharger, after every van has left.
    """
    # Charger CP01 gets van V01, CP02 gets V02, and so on.
    charger_tasks = []
    for number, van in enumerate(vans, start=1):
        charger_tasks.append(run_charger(f"CP{number:02d}", van, host, port, simulation_speed))

    # gather() runs all chargers side by side and waits until every one is done.
    return await asyncio.gather(*charger_tasks)


def main():
    setup_logging()
    vans = generate_normal_evening()
    log_van_schedule(vans)
    asyncio.run(run_all_chargers(vans, settings.CSMS_HOST, settings.CSMS_PORT))


if __name__ == "__main__":
    main()
