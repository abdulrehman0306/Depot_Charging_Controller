"""
Start all simulated chargers at once, each with one van from the scenario.

Start the meter and the controller first, then run:
    python -m src.chargers.run_chargers
The scenario is chosen in settings.py (SCENARIO).
"""

import asyncio
import logging

import settings
from src.chargers.simulated_charger import run_charger
from src.log_setup import setup_logging
from src.results import van_results
from src.scenarios import get_scenario


def log_van_schedule(scenario):
    """Print the (synthetic) van schedule, one line per van."""
    schedule_log = logging.getLogger("SCHEDULE")
    schedule_log.info("scenario %s, synthetic van schedule:", scenario["name"])
    for van in scenario["vans"]:
        schedule_log.info("%s arrives %s with %.1f %%, leaves %s",
                          van.van_id, van.arrival_time.strftime("%H:%M"),
                          van.arrival_state_of_charge_percent, van.departure_time.strftime("%a %H:%M"))


def log_van_results(vans, chargers):
    """Print each van's departure state of charge and any shortfall below the target."""
    completed_visits = []
    for charger in chargers:
        completed_visits.extend(charger.completed_visits)

    results_log = logging.getLogger("RESULTS")
    for result in van_results(vans, completed_visits):
        status = "ok" if result["reached_target"] else f"SHORT by {result['shortfall_kwh']:.1f} kWh"
        results_log.info("%s arrived %.1f %%, left %.1f %%, target %.0f %%: %s", result["van_id"],
                         result["arrival_state_of_charge_percent"], result["departure_state_of_charge_percent"],
                         settings.TARGET_STATE_OF_CHARGE_PERCENT, status)


async def run_all_chargers(vans, host, port, simulation_speed=settings.SIMULATED_SECONDS_PER_REAL_SECOND,
                           charger_outages=()):
    """
    Run one charger per van, all at the same time.

    In: list of Van, controller address and port, simulation speed, and the scenario's
        charger outages (dicts with charger_id, start_time, duration_minutes).
    Out: list of SimulatedCharger, after every van has left.
    """
    outage_by_charger = {}
    for outage in charger_outages:
        outage_by_charger[outage["charger_id"]] = outage

    # Charger CP01 gets van V01, CP02 gets V02, and so on.
    charger_tasks = []
    for number, van in enumerate(vans, start=1):
        charger_id = f"CP{number:02d}"
        charger_tasks.append(run_charger(charger_id, van, host, port, simulation_speed,
                                         outage=outage_by_charger.get(charger_id)))

    # gather() runs all chargers side by side and waits until every one is done.
    return await asyncio.gather(*charger_tasks)


def main():
    setup_logging()
    scenario = get_scenario(settings.SCENARIO)
    log_van_schedule(scenario)
    chargers = asyncio.run(run_all_chargers(scenario["vans"], settings.CSMS_HOST, settings.CSMS_PORT,
                                            charger_outages=scenario["charger_outages"]))
    log_van_results(scenario["vans"], chargers)


if __name__ == "__main__":
    main()
