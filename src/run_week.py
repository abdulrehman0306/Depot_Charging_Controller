"""
The results: every scenario, with every strategy, on 7 nights in a row.

Each night runs the whole system (meter, controller, chargers over real OCPP and
Modbus connections, see src/simulation.py) with the real day-ahead prices of that
night and its own random seed for the vans. Every night is scored with the same
functions as the tests (src/results.py), and written as one row to results/nights.csv
as soon as it has finished. A run that is stopped can be started again: nights that
are already in the file are skipped.

    python -m src.run_week          (about 1.5 hours)
    python -m src.results           (tables and charts from results/nights.csv)
"""

import asyncio
import csv
import logging
import time
from datetime import timedelta
from pathlib import Path

import settings
from src.log_setup import setup_logging
from src.prices import load_prices
from src.results import (
    charging_cost_eur,
    minutes_over_limit,
    peak_site_power,
    spike_reaction_minutes,
    van_results,
)
from src.scenarios import SCENARIO_NAMES, get_scenario
from src.simulation import run_scenario_night

STRATEGIES = ["uncontrolled", "equal_sharing", "smart"]
RESULTS_FOLDER = Path(__file__).resolve().parent.parent / "results"
NIGHTS_FILE = RESULTS_FOLDER / "nights.csv"
EXAMPLE_NIGHT_FILE = RESULTS_FOLDER / "example_night.csv"

# The night whose minute by minute site power is kept for the charts.
EXAMPLE_SCENARIO = "normal_evening"
EXAMPLE_NIGHT_NUMBER = 0

COLUMNS = ["scenario", "strategy", "night_number", "night_date", "peak_site_kw", "minutes_over_limit",
           "vans", "vans_reached_target", "total_shortfall_kwh", "largest_shortfall_kwh", "energy_kwh",
           "cost_eur", "spike_reaction_minutes", "safety_actions", "smart_plans", "fallback_plans"]


def week_of_nights(scenario_name):
    """In: scenario name. Out: list of (night number, scenario dict) for the 7 nights."""
    if scenario_name == "negative_prices":
        first_night = settings.NEGATIVE_PRICE_WEEK_FIRST_NIGHT
    else:
        first_night = settings.WEEK_FIRST_NIGHT
    nights = []
    for night_number in range(settings.NIGHTS_PER_WEEK):
        night_date = first_night + timedelta(days=night_number)
        nights.append((night_number, get_scenario(scenario_name, night_date, settings.RANDOM_SEED + night_number)))
    return nights


def score_night(night, scenario, strategy_name, prices):
    """
    Score one finished night with the shared scoring functions.

    In: the dict from run_scenario_night, the scenario dict, strategy name, prices.
    Out: one row for results/nights.csv (dict with the COLUMNS).
    """
    visits = []
    for charger in night["chargers"]:
        visits.extend(charger.completed_visits)
    results = van_results(scenario["vans"], visits)
    shortfalls = [result["shortfall_kwh"] for result in results]

    # The safety layer's reaction to a load spike, measured on the controller's own site log.
    # Uncontrolled has no safety layer, so there is nothing to measure.
    reaction = None
    if scenario["load_spikes"] and strategy_name != "uncontrolled":
        reaction = spike_reaction_minutes(night["controller"].site_log, scenario["load_spikes"][0],
                                          settings.SITE_LIMIT_KW)

    controller = night["controller"]
    return {
        "scenario": scenario["name"],
        "strategy": strategy_name,
        "night_number": None,
        "night_date": scenario["night_date"].isoformat(),
        "peak_site_kw": round(peak_site_power(night["true_site_log"])[0], 2),
        "minutes_over_limit": round(minutes_over_limit(night["true_site_log"], settings.SITE_LIMIT_KW), 1),
        "vans": len(results),
        "vans_reached_target": sum(1 for result in results if result["reached_target"]),
        "total_shortfall_kwh": round(sum(shortfalls), 2),
        "largest_shortfall_kwh": round(max(shortfalls), 2),
        "energy_kwh": round(sum(charger.energy_meter_wh for charger in night["chargers"]) / 1000, 2),
        "cost_eur": round(charging_cost_eur([charger.power_log for charger in night["chargers"]], prices), 2),
        "spike_reaction_minutes": None if reaction is None else round(reaction, 2),
        "safety_actions": len(controller.safety_actions),
        "smart_plans": controller.smart_plans,
        "fallback_plans": controller.fallback_plans,
    }


def finished_nights():
    """Out: the set of (scenario, strategy, night date) already in results/nights.csv."""
    if not NIGHTS_FILE.exists():
        return set()
    with open(NIGHTS_FILE, newline="", encoding="utf-8") as handle:
        return {(row["scenario"], row["strategy"], row["night_date"]) for row in csv.DictReader(handle)}


def append_row(path, columns, row):
    """Add one row to a CSV file, writing the header first if the file is new."""
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def save_example_night(night, strategy_name):
    """Keep the minute by minute true site power of the example night, for the charts."""
    for sample in night["true_site_log"]:
        append_row(EXAMPLE_NIGHT_FILE, ["strategy", "time", "building_kw", "charging_kw", "site_kw"], {
            "strategy": strategy_name, "time": sample["time"].isoformat(),
            "building_kw": round(sample["building_kw"], 3), "charging_kw": round(sample["charging_kw"], 3),
            "site_kw": round(sample["site_kw"], 3)})


async def run_week():
    """Run every night that is not in results/nights.csv yet."""
    RESULTS_FOLDER.mkdir(exist_ok=True)
    prices = load_prices()
    done = finished_nights()
    events = logging.getLogger("WEEK")

    for scenario_name in SCENARIO_NAMES:
        for strategy_name in STRATEGIES:
            for night_number, scenario in week_of_nights(scenario_name):
                key = (scenario_name, strategy_name, scenario["night_date"].isoformat())
                if key in done:
                    continue
                started = time.perf_counter()
                night = await run_scenario_night(scenario, strategy_name, prices)
                row = score_night(night, scenario, strategy_name, prices)
                row["night_number"] = night_number
                append_row(NIGHTS_FILE, COLUMNS, row)
                if scenario_name == EXAMPLE_SCENARIO and night_number == EXAMPLE_NIGHT_NUMBER:
                    save_example_night(night, strategy_name)
                events.warning("%s %s night %d (%s): peak %.1f kW, %.0f min over, %d/%d vans, %.2f EUR, %.0f s",
                               scenario_name, strategy_name, night_number, row["night_date"], row["peak_site_kw"],
                               row["minutes_over_limit"], row["vans_reached_target"], row["vans"], row["cost_eur"],
                               time.perf_counter() - started)


def main():
    setup_logging()
    # The many lines of each night's programs would hide the progress lines; keep only warnings.
    logging.getLogger().setLevel(logging.WARNING)
    asyncio.run(run_week())


if __name__ == "__main__":
    main()
