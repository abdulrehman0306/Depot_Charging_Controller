"""
Scoring of a run. Every charging strategy is scored by these same functions,
so the comparison between strategies is fair.

A site log is a list of samples, oldest first. Each sample is a dict:
    {"time": datetime, "building_kw": float, "charging_kw": float, "site_kw": float}
"""


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
        if sample["site_kw"] > site_limit_kw:
            total_minutes += (next_sample["time"] - sample["time"]).total_seconds() / 60
    return total_minutes
