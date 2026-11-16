"""
The test scenarios: which vans come to the depot, and when.

All van data here is SYNTHETIC: drawn at random from the made-up ranges in
settings.py, with a fixed random seed so every run gets the same vans.
It is not measured data from a real fleet.

Stage 2 has the first scenario only: a normal evening with staggered arrivals.
"""

import random
from datetime import datetime, timedelta

import settings
from src.chargers.van import Van


def clock_time_on_day(day, hh_mm):
    """
    In: a date, and a clock time as text like "16:30".
    Out: that date and time as a datetime in the simulation's time zone.
    """
    hours, minutes = hh_mm.split(":")
    return datetime(day.year, day.month, day.day, int(hours), int(minutes),
                    tzinfo=settings.SIMULATION_START_TIME.tzinfo)


def random_time_between(rng, earliest, latest):
    """
    In: random generator, earliest and latest datetime.
    Out: a random datetime between them, on a whole minute.
    """
    window_minutes = int((latest - earliest).total_seconds() // 60)
    return earliest + timedelta(minutes=rng.randint(0, window_minutes))


def generate_normal_evening(number_of_vans=settings.NUMBER_OF_CHARGERS, seed=settings.RANDOM_SEED,
                            arrival_day=settings.SIMULATION_START_TIME.date()):
    """
    Scenario 1, normal evening: vans arrive at staggered times and leave the next morning.

    In: number of vans, random seed, the day the vans arrive.
    Out: list of Van, sorted by van id.
    """
    # Our own random generator with a fixed seed: same seed, same vans, every run.
    rng = random.Random(seed)
    departure_day = arrival_day + timedelta(days=1)

    vans = []
    for van_number in range(1, number_of_vans + 1):
        arrival_time = random_time_between(
            rng,
            clock_time_on_day(arrival_day, settings.ARRIVAL_EARLIEST),
            clock_time_on_day(arrival_day, settings.ARRIVAL_LATEST),
        )

        # The departure must be inside the morning window AND give a parking
        # time between the minimum and maximum hours. Take the overlap of both.
        departure_earliest = max(clock_time_on_day(departure_day, settings.DEPARTURE_EARLIEST),
                                 arrival_time + timedelta(hours=settings.PARKING_HOURS_MIN))
        departure_latest = min(clock_time_on_day(departure_day, settings.DEPARTURE_LATEST),
                               arrival_time + timedelta(hours=settings.PARKING_HOURS_MAX))
        departure_time = random_time_between(rng, departure_earliest, departure_latest)

        arrival_state_of_charge_percent = round(rng.uniform(settings.ARRIVAL_STATE_OF_CHARGE_MIN_PERCENT,
                                                            settings.ARRIVAL_STATE_OF_CHARGE_MAX_PERCENT), 1)
        vans.append(Van(f"V{van_number:02d}", arrival_time, departure_time, arrival_state_of_charge_percent))
    return vans
