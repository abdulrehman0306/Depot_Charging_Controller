"""
Scoring of a run. Every charging strategy is scored by these same functions,
so the comparison between strategies is fair.

A site log is a list of samples, oldest first. Each sample is a dict with at least:
    {"time": datetime, "building_kw": float, "charging_kw": float, "site_kw": float}
A sample may also hold "site_limit_kw", the limit in force at that moment (it can be
changed through the API during a run). Then that limit is used for the sample instead
of the one passed to the scoring function.

Two kinds of site log are used:
- measured: what the controller saw (meter reading + charger reports). This is
  what a real depot would have. It sees a building jump only at the next meter reading.
- true: built afterwards from what the chargers really drew and the real building
  load (true_site_log below). Only a simulation can have this; the tests use it
  to prove the limit holds.
"""

from datetime import timedelta

import settings
from src.meter.building_load import building_load_kw
from src.prices import price_at

# Power values within this much of the limit count as "at the limit", not over it.
POWER_TOLERANCE_KW = 0.01


def is_over_limit(sample, site_limit_kw):
    """
    In: one site log sample, the site limit [kW] to use if the sample does not hold its own.
    Out: True if the site power was over the limit in force at that moment.
    """
    limit_kw = sample.get("site_limit_kw", site_limit_kw)
    return sample["site_kw"] > limit_kw + POWER_TOLERANCE_KW


def peak_site_power(site_log):
    """
    In: site log.
    Out: (highest site power [kW], time it happened). (0.0, None) for an empty log.
    """
    peak_kw = 0.0
    peak_time = None
    for sample in site_log:
        if sample["site_kw"] > peak_kw:
            peak_kw = sample["site_kw"]
            peak_time = sample["time"]
    return peak_kw, peak_time


def minutes_over_limit(site_log, site_limit_kw):
    """
    Total simulated time the site drew more than the limit.

    In: site log, site limit [kW].
    Out: minutes over the limit.

    Each sample is taken to hold until the next sample, so a gap between
    samples counts fully if the earlier sample was over the limit.
    """
    total_minutes = 0.0
    for sample, next_sample in zip(site_log, site_log[1:]):
        if is_over_limit(sample, site_limit_kw):
            total_minutes += (next_sample["time"] - sample["time"]).total_seconds() / 60
    return total_minutes


def over_limit_episodes(site_log, site_limit_kw):
    """
    Find each stretch of time the site was over the limit.

    In: site log, site limit [kW].
    Out: list of (start time, end time). End is the first sample back at or under
         the limit, or None if the log ends while still over.
    """
    episodes = []
    episode_start = None
    for sample in site_log:
        is_over = is_over_limit(sample, site_limit_kw)
        if is_over and episode_start is None:
            episode_start = sample["time"]
        elif not is_over and episode_start is not None:
            episodes.append((episode_start, sample["time"]))
            episode_start = None
    if episode_start is not None:
        episodes.append((episode_start, None))
    return episodes


def spike_reaction_minutes(site_log, spike, site_limit_kw):
    """
    How long it took from the start of a load spike until the site was back under the limit.

    In: site log, the LoadSpike, site limit [kW].
    Out: minutes from the spike start to the first sample back at or under the limit.
         0.0 if the site never went over the limit during the spike (the margin was enough).
         None if it never came back under.
    """
    spike_end = spike.start_time + timedelta(minutes=spike.duration_minutes)
    over_seen = False
    for sample in site_log:
        if sample["time"] < spike.start_time:
            continue
        is_over = is_over_limit(sample, site_limit_kw)
        if is_over:
            over_seen = True
        elif over_seen:
            return (sample["time"] - spike.start_time).total_seconds() / 60
        elif sample["time"] >= spike_end:
            # The whole spike passed without the site going over the limit.
            return 0.0
    if over_seen:
        return None
    return 0.0


def charger_power_at(power_log, sim_time):
    """
    In: one charger's power log (list of steps with start, end, power_kw), a time.
    Out: the power the charger really drew at that time [kW], 0 if it was not charging.
    """
    for step in power_log:
        if step["start"] <= sim_time < step["end"]:
            return step["power_kw"]
    return 0.0


def true_site_log(power_logs, load_spikes, start_time, end_time):
    """
    Rebuild the real site power minute by minute, from what the chargers really drew
    and the real building load (profile + spikes).

    In: list of charger power logs, the scenario's load spikes, first and last time.
    Out: site log with one sample per simulated minute.
    """
    site_log = []
    sim_time = start_time
    while sim_time <= end_time:
        building_kw = building_load_kw(sim_time, load_spikes)
        charging_kw = sum(charger_power_at(power_log, sim_time) for power_log in power_logs)
        site_log.append({"time": sim_time, "building_kw": building_kw, "charging_kw": charging_kw,
                         "site_kw": building_kw + charging_kw})
        sim_time += timedelta(minutes=1)
    return site_log


def next_quarter_hour(sim_time):
    """In: a time. Out: the start of the next quarter hour after it."""
    minutes_into_quarter = sim_time.minute % 15
    quarter_start = sim_time.replace(minute=sim_time.minute - minutes_into_quarter, second=0, microsecond=0)
    return quarter_start + timedelta(minutes=15)


def charging_cost_eur(power_logs, prices):
    """
    What the charging energy cost at the day-ahead price.

    Only the wholesale energy price is counted (what the depot's supplier would pass
    on with a dynamic tariff). Grid fees, taxes and levies are the same for every
    strategy, so they are left out.

    In: list of charger power logs (steps with start, end, power_kw), price Series from src/prices.py.
    Out: cost [EUR]. Negative if the vans were paid to charge (negative prices).
    """
    total_eur = 0.0
    for power_log in power_logs:
        for step in power_log:
            # A step may cross a quarter hour boundary. Split it there, so each part
            # is paid at the price of its own quarter hour.
            piece_start = step["start"]
            while piece_start < step["end"]:
                piece_end = min(step["end"], next_quarter_hour(piece_start))
                energy_kwh = step["power_kw"] * (piece_end - piece_start).total_seconds() / 3600
                total_eur += energy_kwh * price_at(prices, piece_start) / 1000
                piece_start = piece_end
    return total_eur


def van_results(vans, completed_visits):
    """
    Did each van leave with enough charge, and if not, how much was missing?

    In: list of Van, list of completed visits (dicts with van_id and departure_state_of_charge_percent).
    Out: list of dicts, one per van: van_id, arrival and departure state of charge [%],
         reached_target (True/False) and shortfall_kwh (energy missing in the battery to reach the target).
    """
    departure_by_van = {}
    for visit in completed_visits:
        departure_by_van[visit["van_id"]] = visit["departure_state_of_charge_percent"]

    results = []
    for van in vans:
        departure_percent = departure_by_van.get(van.van_id, van.arrival_state_of_charge_percent)
        missing_percent = max(0.0, settings.TARGET_STATE_OF_CHARGE_PERCENT - departure_percent)
        results.append({
            "van_id": van.van_id,
            "arrival_state_of_charge_percent": van.arrival_state_of_charge_percent,
            "departure_state_of_charge_percent": departure_percent,
            "reached_target": missing_percent < 1e-6,
            "shortfall_kwh": missing_percent / 100 * settings.VAN_BATTERY_USABLE_KWH,
        })
    return results
