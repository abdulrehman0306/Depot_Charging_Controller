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

import math
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


# ---------------------------------------------------------------------------
# Tables and charts of the week results (python -m src.results)
# ---------------------------------------------------------------------------
# These read results/nights.csv, written by src/run_week.py. pandas and matplotlib
# are imported inside the functions, so the controller, which uses the scoring
# functions above, does not have to load them.

STRATEGIES = ["uncontrolled", "equal_sharing", "smart"]
STRATEGY_LABELS = {"uncontrolled": "Uncontrolled", "equal_sharing": "Equal sharing", "smart": "Smart"}
SCENARIO_LABELS = {"normal_evening": "Normal evening", "rush": "Rush", "late_van": "Late van",
                   "charger_failure": "Charger failure", "load_spike": "Load spike",
                   "negative_prices": "Negative prices", "impossible": "Impossible"}

# Chart colours: categorical slots 1 to 3 of the reference palette, in fixed order,
# one per strategy. Reference lines (limit, target) are grey ink, not a series colour.
STRATEGY_COLORS = {"uncontrolled": "#2a78d6", "equal_sharing": "#eb6834", "smart": "#1baf7a"}
SURFACE_COLOR = "#fcfcfb"
TEXT_COLOR = "#0b0b0b"
SECONDARY_TEXT_COLOR = "#52514e"
GRID_COLOR = "#e4e3df"


def summarize_week(nights):
    """
    One row per scenario and strategy, over its 7 nights.

    In: pandas DataFrame of results/nights.csv.
    Out: pandas DataFrame with the week's numbers per scenario and strategy.
    """
    import pandas as pd

    rows = []
    for (scenario, strategy), group in nights.groupby(["scenario", "strategy"], sort=False):
        energy_kwh = group["energy_kwh"].sum()
        reactions = group["spike_reaction_minutes"].dropna()
        rows.append({
            "scenario": scenario,
            "strategy": strategy,
            "nights": len(group),
            "peak_site_kw": group["peak_site_kw"].max(),
            "minutes_over_limit": group["minutes_over_limit"].sum(),
            "van_visits": int(group["vans"].sum()),
            "vans_reached_target": int(group["vans_reached_target"].sum()),
            "total_shortfall_kwh": group["total_shortfall_kwh"].sum(),
            "largest_shortfall_kwh": group["largest_shortfall_kwh"].max(),
            "energy_kwh": energy_kwh,
            "cost_eur": group["cost_eur"].sum(),
            "cost_ct_per_kwh": 100 * group["cost_eur"].sum() / energy_kwh,
            "spike_reaction_mean_minutes": reactions.mean() if len(reactions) else None,
            "spike_reaction_max_minutes": reactions.max() if len(reactions) else None,
            "fallback_plans": int(group["fallback_plans"].sum()),
        })
    return pd.DataFrame(rows)


def markdown_table(summary):
    """In: the week summary. Out: a Markdown table for the README, one line per scenario and strategy."""
    lines = ["| Scenario | Strategy | Peak site power | Minutes over 150 kW | Vans at 90 % | Shortfall (largest van) "
             "| Energy | Cost | Average price |",
             "|---|---|---|---|---|---|---|---|---|"]
    for row in summary.itertuples():
        if row.total_shortfall_kwh < 0.05:
            shortfall = "0"
        else:
            shortfall = f"{row.total_shortfall_kwh:.1f} kWh ({row.largest_shortfall_kwh:.1f})"
        # Rounded down, so that a peak of 149.99 kW does not show as 150.0 kW.
        peak_kw = math.floor(row.peak_site_kw * 10) / 10
        lines.append(f"| {SCENARIO_LABELS[row.scenario]} | {STRATEGY_LABELS[row.strategy]} | {peak_kw:.1f} kW "
                     f"| {row.minutes_over_limit:.0f} | {row.vans_reached_target} of {row.van_visits} | {shortfall} "
                     f"| {row.energy_kwh:,.0f} kWh | {row.cost_eur:,.2f} EUR | {row.cost_ct_per_kwh:.2f} ct/kWh |")
    return "\n".join(lines)


def style_axes(axes, title, y_label):
    """Recessive grid and axes, plain text in ink colours."""
    axes.set_facecolor(SURFACE_COLOR)
    axes.set_title(title, loc="left", color=TEXT_COLOR, fontsize=12)
    axes.set_ylabel(y_label, color=SECONDARY_TEXT_COLOR)
    axes.grid(axis="y", color=GRID_COLOR, linewidth=0.8)
    axes.set_axisbelow(True)
    for side in ["top", "right"]:
        axes.spines[side].set_visible(False)
    for side in ["left", "bottom"]:
        axes.spines[side].set_color(GRID_COLOR)
    axes.tick_params(colors=SECONDARY_TEXT_COLOR)


def plot_example_night(example, path):
    """
    True site power of one night for the three strategies, with the grid limit and planning target.

    In: DataFrame of results/example_night.csv, output path.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import pandas as pd

    figure, axes = plt.subplots(figsize=(10, 4.8), facecolor=SURFACE_COLOR)
    for strategy in STRATEGIES:
        night = example[example["strategy"] == strategy]
        times = pd.to_datetime(night["time"], utc=True).dt.tz_convert("Europe/Berlin")
        axes.plot(times, night["site_kw"], color=STRATEGY_COLORS[strategy], linewidth=2,
                  label=STRATEGY_LABELS[strategy], drawstyle="steps-post")
    limit_kw = settings.SITE_LIMIT_KW
    target_kw = settings.SITE_LIMIT_KW - settings.SAFETY_MARGIN_KW
    axes.axhline(limit_kw, color=SECONDARY_TEXT_COLOR, linewidth=1.5, linestyle="--")
    axes.axhline(target_kw, color=SECONDARY_TEXT_COLOR, linewidth=1, linestyle=":")
    axes.text(axes.get_xlim()[0], limit_kw + 3, "  grid limit 150 kW", color=SECONDARY_TEXT_COLOR, fontsize=9)
    axes.text(axes.get_xlim()[0], target_kw - 9, "  planning target 135 kW", color=SECONDARY_TEXT_COLOR, fontsize=9)
    axes.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz="Europe/Berlin"))
    axes.set_ylim(0, None)
    style_axes(axes, "True site power on the night of 12 to 13 January 2026 (normal evening)", "kW")
    axes.legend(frameon=False, loc="upper left", bbox_to_anchor=(0, -0.08), ncol=3)
    figure.tight_layout()
    figure.savefig(path, dpi=150, facecolor=SURFACE_COLOR)
    plt.close(figure)


def plot_week_bars(summary, column, title, y_label, path):
    """
    One group of bars per scenario, one bar per strategy.

    In: the week summary, the column to show, chart title, y axis label, output path.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    scenarios = list(dict.fromkeys(summary["scenario"]))
    bar_width = 0.26
    figure, axes = plt.subplots(figsize=(11, 4.6), facecolor=SURFACE_COLOR)
    for position, strategy in enumerate(STRATEGIES):
        values = []
        for scenario in scenarios:
            match = summary[(summary["scenario"] == scenario) & (summary["strategy"] == strategy)]
            values.append(match[column].iloc[0])
        bar_centres = [index + (position - 1) * bar_width for index in range(len(scenarios))]
        # A thin gap between neighbouring bars: an edge in the surface colour.
        axes.bar(bar_centres, values, bar_width, color=STRATEGY_COLORS[strategy], label=STRATEGY_LABELS[strategy],
                 edgecolor=SURFACE_COLOR, linewidth=2)
    axes.set_xticks(range(len(scenarios)))
    axes.set_xticklabels([SCENARIO_LABELS[scenario] for scenario in scenarios])
    axes.axhline(0, color=SECONDARY_TEXT_COLOR, linewidth=0.8)
    style_axes(axes, title, y_label)
    axes.legend(frameon=False, loc="upper left", bbox_to_anchor=(0, -0.1), ncol=3)
    figure.tight_layout()
    figure.savefig(path, dpi=150, facecolor=SURFACE_COLOR)
    plt.close(figure)


def plot_smart_and_price(example, prices, path):
    """
    Two panels with one shared time axis: the day-ahead price, and the smart controller's
    charging power, on the example night. Two measures, two panels, never two y axes.

    In: DataFrame of results/example_night.csv, price Series, output path.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import pandas as pd

    night = example[example["strategy"] == "smart"]
    times = pd.to_datetime(night["time"], utc=True).dt.tz_convert("Europe/Berlin")
    night_prices = prices[times.min():times.max()]

    figure, (price_axes, power_axes) = plt.subplots(2, 1, figsize=(10, 5.6), sharex=True, facecolor=SURFACE_COLOR)
    # The price is not one of the strategies, so it is drawn in ink, not in a strategy colour.
    price_axes.plot(night_prices.index, night_prices.values, color=SECONDARY_TEXT_COLOR, linewidth=2,
                    drawstyle="steps-post")
    style_axes(price_axes, "Day-ahead price (SMARD, DE-LU), 12 to 13 January 2026", "EUR/MWh")
    power_axes.plot(times, night["charging_kw"], color=STRATEGY_COLORS["smart"], linewidth=2, drawstyle="steps-post")
    style_axes(power_axes, "Smart controller: charging power of all vans together", "kW")
    power_axes.set_ylim(0, None)
    power_axes.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz="Europe/Berlin"))
    figure.tight_layout()
    figure.savefig(path, dpi=150, facecolor=SURFACE_COLOR)
    plt.close(figure)


def main():
    """Make the summary tables and the charts from results/nights.csv and results/example_night.csv."""
    from pathlib import Path

    import pandas as pd

    from src.prices import load_prices

    folder = Path(__file__).resolve().parent.parent / "results"
    nights = pd.read_csv(folder / "nights.csv")
    example = pd.read_csv(folder / "example_night.csv")

    summary = summarize_week(nights)
    summary.round(2).to_csv(folder / "week_summary.csv", index=False)
    (folder / "week_summary.md").write_text(markdown_table(summary) + "\n", encoding="utf-8")

    plot_example_night(example, folder / "example_night_site_power.png")
    # Uncontrolled and equal sharing fill every van to 100 %, smart stops near 90 %, so the
    # fair comparison is the price paid per kWh, not the total in EUR.
    plot_week_bars(summary, "cost_ct_per_kwh", "Average day-ahead price paid per kWh charged, over the week",
                   "ct/kWh", folder / "week_price_per_kwh.png")
    plot_week_bars(summary, "minutes_over_limit", "Minutes over the 150 kW grid limit in the week (true site power)",
                   "minutes", folder / "week_minutes_over_limit.png")
    plot_smart_and_price(example, load_prices(), folder / "smart_charging_and_price.png")
    print(markdown_table(summary))


if __name__ == "__main__":
    main()
