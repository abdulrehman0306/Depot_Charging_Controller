"""
The safety layer's reaction to the load spike, measured at a slow simulation speed.

The week run (src/run_week.py) runs equal sharing at 3000x: one simulated minute is
only 20 real milliseconds, so the few milliseconds the programs need for each control
cycle already add up to many simulated seconds, and the measured reaction comes out
too long. Here, as in the test test_safety_layer_brings_the_site_back_under_the_limit_in_time,
each of the 7 load spike nights runs only around the spike (19:20 to 19:45, spike at
19:30) at 300x, where one simulated minute is 200 real milliseconds.

    python -m src.measure_spike_reaction        (about 1 minute)

Writes results/spike_reaction.csv.
"""

import asyncio
import logging

import settings
from src.chargers.van import Van
from src.log_setup import setup_logging
from src.prices import load_prices
from src.results import minutes_over_limit, peak_site_power, spike_reaction_minutes
from src.run_week import RESULTS_FOLDER, week_of_nights
from src.scenarios import clock_time_on_day
from src.simulation import run_night

SLOW_SIMULATION_SPEED = 300
OUTPUT_FILE = RESULTS_FOLDER / "spike_reaction.csv"


async def measure():
    """Out: one row per load spike night with the reaction and the true minutes over the limit."""
    prices = load_prices()
    rows = []
    for night_number, scenario in week_of_nights("load_spike"):
        day = scenario["night_date"]
        spike = scenario["load_spikes"][0]
        leave_time = clock_time_on_day(day, "19:45")
        vans = [Van(van.van_id, van.arrival_time, leave_time, van.arrival_state_of_charge_percent)
                for van in scenario["vans"] if van.arrival_time < leave_time]
        night = await run_night(vans, [spike], "equal_sharing", prices, settings.SITE_LIMIT_KW,
                                SLOW_SIMULATION_SPEED, clock_time_on_day(day, "19:20"))
        true_log_from_spike = [sample for sample in night["true_site_log"] if sample["time"] >= spike.start_time]
        rows.append({
            "night_date": day.isoformat(),
            "vans_charging": len(vans),
            "peak_site_kw": round(peak_site_power(night["true_site_log"])[0], 2),
            "true_minutes_over_limit": round(minutes_over_limit(true_log_from_spike, settings.SITE_LIMIT_KW), 2),
            "reaction_minutes": round(spike_reaction_minutes(night["controller"].site_log, spike, settings.SITE_LIMIT_KW), 2),
        })
        logging.getLogger("SPIKE").warning("%s", rows[-1])
    return rows


def main():
    import pandas as pd

    setup_logging()
    logging.getLogger().setLevel(logging.WARNING)
    rows = asyncio.run(measure())
    RESULTS_FOLDER.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(OUTPUT_FILE, index=False)
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
