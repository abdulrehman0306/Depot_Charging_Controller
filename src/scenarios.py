"""
The test scenarios: which vans come to the depot and when, extra building load,
and charger outages.

The vans, the building load and the outages are SYNTHETIC: drawn at random from
the made-up ranges in settings.py with a fixed random seed, or set by hand to create
a test situation. The day-ahead prices of each night are REAL (SMARD.de, see src/prices.py).

  1 normal_evening   vans arrive at staggered times between 16:00 and 20:00
  2 rush             all vans arrive within 30 minutes
  3 late_van         like 1, but one van arrives at 22:30 with 10 % and must leave at 05:00
  4 charger_failure  like 1, but charger CP02 loses its connection to the controller
                     at 18:00 and reconnects 30 minutes later (it keeps charging meanwhile)
  5 load_spike       like 1, plus 40 kW extra building load for 20 minutes at 19:30
  6 negative_prices  like 1, but on a real night with negative prices (4 to 5 October 2025)
  7 impossible       the vans need more energy than the connection can give before they leave
"""

import random
from datetime import datetime, timedelta

import settings
from src.chargers.van import Van
from src.meter.building_load import LoadSpike


def clock_time_on_day(day, hh_mm):
    """
    In: a date, and a clock time as text like "16:30".
    Out: that date and time as a datetime in German local time.
    """
    hours, minutes = hh_mm.split(":")
    return datetime(day.year, day.month, day.day, int(hours), int(minutes), tzinfo=settings.TIMEZONE)


def random_time_between(rng, earliest, latest):
    """
    In: random generator, earliest and latest datetime.
    Out: a random datetime between them, on a whole minute.
    """
    window_minutes = int((latest - earliest).total_seconds() // 60)
    return earliest + timedelta(minutes=rng.randint(0, window_minutes))


def generate_vans(arrival_day, number_of_vans, seed, arrival_earliest, arrival_latest):
    """
    Draw random vans: arrival in the given window, departure the next morning.

    In: day of arrival, number of vans, random seed, arrival window as "HH:MM" texts.
    Out: list of Van.
    """
    # Our own random generator with a fixed seed: same seed, same vans, every run.
    rng = random.Random(seed)
    departure_day = arrival_day + timedelta(days=1)

    vans = []
    for van_number in range(1, number_of_vans + 1):
        arrival_time = random_time_between(rng, clock_time_on_day(arrival_day, arrival_earliest),
                                           clock_time_on_day(arrival_day, arrival_latest))

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


def generate_normal_evening(arrival_day=settings.NORMAL_NIGHT_DATE, number_of_vans=settings.NUMBER_OF_CHARGERS,
                            seed=settings.RANDOM_SEED):
    """Scenario 1 vans: staggered arrivals in the normal window. Out: list of Van."""
    return generate_vans(arrival_day, number_of_vans, seed, settings.ARRIVAL_EARLIEST, settings.ARRIVAL_LATEST)


def rush_vans(day):
    """Scenario 2 vans: all arrive between 19:00 and 19:30. Out: list of Van."""
    return generate_vans(day, settings.NUMBER_OF_CHARGERS, settings.RANDOM_SEED, "19:00", "19:30")


def late_van_vans(day):
    """Scenario 3 vans: normal evening, but the last van comes at 22:30 with 10 % and leaves at 05:00."""
    vans = generate_normal_evening(day)
    vans[-1] = Van(vans[-1].van_id, clock_time_on_day(day, "22:30"),
                   clock_time_on_day(day + timedelta(days=1), "05:00"), 10.0)
    return vans


def impossible_vans(day):
    """
    Scenario 7 vans: deliberately extreme. All ten arrive at 20:00 nearly empty (5 %)
    and must leave at 01:00. They need 10 x 85 % x 75 kWh / 0.92 = 693 kWh from the grid,
    but in 5 hours the connection leaves only 2 h x 113 kW + 3 h x 120 kW = 586 kWh
    for charging (150 kW limit, minus 15 kW margin, minus 22 or 15 kW building load).
    """
    vans = []
    for van_number in range(1, settings.NUMBER_OF_CHARGERS + 1):
        vans.append(Van(f"V{van_number:02d}", clock_time_on_day(day, "20:00"),
                        clock_time_on_day(day + timedelta(days=1), "01:00"), 5.0))
    return vans


def get_scenario(name):
    """
    In: scenario name.
    Out: dict with
         name, night_date, start_time (when the simulation starts),
         vans (list of Van), load_spikes (list of LoadSpike),
         charger_outages (list of dicts with charger_id, start_time, duration_minutes).
    """
    day = settings.NORMAL_NIGHT_DATE
    load_spikes = []
    charger_outages = []

    if name == "normal_evening":
        vans = generate_normal_evening(day)
    elif name == "rush":
        vans = rush_vans(day)
    elif name == "late_van":
        vans = late_van_vans(day)
    elif name == "charger_failure":
        vans = generate_normal_evening(day)
        # CP02's van (V02) arrives at 17:10, so it is plugged in at 18:00.
        charger_outages = [{"charger_id": "CP02", "start_time": clock_time_on_day(day, "18:00"),
                            "duration_minutes": 30}]
    elif name == "load_spike":
        vans = generate_normal_evening(day)
        # A late workshop job (e.g. compressor and welding) while many vans are charging.
        load_spikes = [LoadSpike(clock_time_on_day(day, "19:30"), 20, 40.0)]
    elif name == "negative_prices":
        day = settings.NEGATIVE_PRICE_NIGHT_DATE
        vans = generate_normal_evening(day)
    elif name == "impossible":
        vans = impossible_vans(day)
    else:
        raise ValueError(f"unknown scenario: {name}")

    start_time = clock_time_on_day(day, settings.SIMULATION_START_CLOCK)
    check_no_clock_change(start_time, max(van.departure_time for van in vans))
    return {"name": name, "night_date": day, "start_time": start_time, "vans": vans,
            "load_spikes": load_spikes, "charger_outages": charger_outages}


def check_no_clock_change(start_time, end_time):
    """
    Stop with an error if summer or winter time starts during the night.

    Python adds hours to a local time "on the clock", so a night with a clock change
    would be one hour too short or too long. The scenarios simply avoid those nights.
    """
    if start_time.utcoffset() != end_time.utcoffset():
        raise ValueError(f"the night from {start_time} to {end_time} has a clock change; pick another date")


SCENARIO_NAMES = ["normal_evening", "rush", "late_van", "charger_failure", "load_spike", "negative_prices",
                  "impossible"]
