"""
Tests of the small calculation pieces: kW/amps conversion, the van battery,
the synthetic schedule and building load, and the scoring functions.
All are checked against numbers worked out by hand.
"""

from datetime import timedelta

import pytest

import settings
from src.chargers.van import Van, charge_van, grid_energy_to_reach_kwh
from src.electrical import charging_current_limit_a, current_a_to_power_kw, power_kw_to_current_a
from src.meter.building_load import LoadSpike, building_load_kw
from src.results import (
    charger_power_at,
    minutes_over_limit,
    over_limit_episodes,
    peak_site_power,
    spike_reaction_minutes,
    van_results,
)
from src.scenarios import clock_time_on_day, generate_normal_evening

START = settings.SIMULATION_START_TIME


# ---------------------------------------------------------------------------
# kW <-> amps
# ---------------------------------------------------------------------------

def test_32_amps_on_three_phases_is_about_22_kw():
    # 3 x 230 V x 32 A = 22 080 W
    assert current_a_to_power_kw(32) == pytest.approx(22.08)


def test_kw_to_amps_rounds_down():
    # 22 kW / 690 V = 31.88 A, rounded down to 31.8 A
    assert power_kw_to_current_a(22.0) == 31.8
    # An exact value stays exact: 11.04 kW is 16.0 A
    assert power_kw_to_current_a(11.04) == 16.0


def test_rounding_never_allows_more_power_than_asked():
    for power_kw in [0.5, 4.2, 7.0, 11.0, 13.33, 15.0, 22.0]:
        assert current_a_to_power_kw(power_kw_to_current_a(power_kw)) <= power_kw


def test_current_limit_is_never_between_0_and_6_amps():
    for power_kw in [0.0, 0.5, 2.0, 4.0, 4.13, 4.14, 4.2, 7.0, 11.0, 22.0, 30.0]:
        limit_a = charging_current_limit_a(power_kw)
        assert limit_a == 0.0 or settings.CHARGER_MIN_CURRENT_A <= limit_a <= settings.CHARGER_MAX_CURRENT_A


def test_current_limit_pauses_below_6_amps_and_caps_at_32_amps():
    # 4.0 kW = 5.8 A: below the minimum, so the van pauses.
    assert charging_current_limit_a(4.0) == 0.0
    # 4.14 kW = exactly 6.0 A: allowed.
    assert charging_current_limit_a(4.14) == 6.0
    # 30 kW would be 43.4 A: capped at the charger's 32 A.
    assert charging_current_limit_a(30.0) == 32.0


# ---------------------------------------------------------------------------
# Van battery
# ---------------------------------------------------------------------------

def test_grid_energy_includes_charging_losses():
    # 20 % -> 90 % of 75 kWh = 52.5 kWh into the battery, / 0.92 = 57.07 kWh from the grid
    assert grid_energy_to_reach_kwh(20.0, 90.0) == pytest.approx(52.5 / 0.92)
    # Already above the target: nothing needed
    assert grid_energy_to_reach_kwh(95.0, 90.0) == 0.0


def test_one_hour_at_22_kw_raises_state_of_charge():
    # 22 kWh from the grid x 0.92 = 20.24 kWh stored = 26.99 % of 75 kWh
    new_state_of_charge_percent, grid_energy_kwh = charge_van(50.0, 22.0, 1.0)
    assert grid_energy_kwh == pytest.approx(22.0)
    assert new_state_of_charge_percent == pytest.approx(50.0 + 20.24 / 75 * 100)


def test_van_stops_charging_when_full():
    # From 99 % only 0.75 kWh fits in the battery, which is 0.815 kWh from the grid.
    new_state_of_charge_percent, grid_energy_kwh = charge_van(99.0, 22.0, 1.0)
    assert new_state_of_charge_percent == pytest.approx(100.0)
    assert grid_energy_kwh == pytest.approx(0.75 / 0.92)


# ---------------------------------------------------------------------------
# Synthetic schedule and building load
# ---------------------------------------------------------------------------

def test_van_schedule_stays_inside_the_ranges():
    vans = generate_normal_evening()
    arrival_day = START.date()
    departure_day = arrival_day + timedelta(days=1)

    assert len(vans) == settings.NUMBER_OF_CHARGERS
    for van in vans:
        parking_hours = (van.departure_time - van.arrival_time).total_seconds() / 3600
        assert clock_time_on_day(arrival_day, "16:00") <= van.arrival_time <= clock_time_on_day(arrival_day, "20:00")
        assert clock_time_on_day(departure_day, "05:00") <= van.departure_time <= clock_time_on_day(departure_day, "08:00")
        assert settings.PARKING_HOURS_MIN <= parking_hours <= settings.PARKING_HOURS_MAX
        assert 20.0 <= van.arrival_state_of_charge_percent <= 50.0


def test_same_seed_gives_the_same_vans():
    assert generate_normal_evening(seed=7) == generate_normal_evening(seed=7)
    assert generate_normal_evening(seed=7) != generate_normal_evening(seed=8)


def test_building_load_follows_the_daily_profile():
    day = START.date()
    assert building_load_kw(clock_time_on_day(day, "03:00")) == 15.0
    assert building_load_kw(clock_time_on_day(day, "10:30")) == 45.0
    assert building_load_kw(clock_time_on_day(day, "19:00")) == 22.0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def make_site_log(site_powers_kw):
    """One sample per simulated minute with the given site powers [kW]."""
    return [{"time": START + timedelta(minutes=minute), "building_kw": 0.0,
             "charging_kw": site_kw, "site_kw": site_kw}
            for minute, site_kw in enumerate(site_powers_kw)]


def test_peak_site_power_and_its_time():
    peak_kw, peak_time = peak_site_power(make_site_log([100, 160, 155, 120]))
    assert peak_kw == 160
    assert peak_time == START + timedelta(minutes=1)


def test_minutes_over_limit_counts_each_minute_over():
    # Minutes 1 and 2 are over 150 kW. The last sample has no "next" sample, so it adds no time.
    assert minutes_over_limit(make_site_log([100, 160, 155, 120, 170]), 150) == 2.0


def test_over_limit_episodes_have_start_and_end():
    episodes = over_limit_episodes(make_site_log([100, 160, 155, 120, 170]), 150)
    # Over from minute 1 to minute 3, and again from minute 4 until the log ends.
    assert episodes == [(START + timedelta(minutes=1), START + timedelta(minutes=3)),
                        (START + timedelta(minutes=4), None)]


def test_spike_reaction_is_measured_from_the_spike_start():
    # Spike starts at minute 2. The log shows it over the limit at minutes 3 and 4, back under at 5.
    spike = LoadSpike(START + timedelta(minutes=2), 20, 40.0)
    site_log = make_site_log([130, 130, 130, 175, 160, 140, 140])
    assert spike_reaction_minutes(site_log, spike, 150) == 3.0


def test_spike_reaction_is_zero_when_the_margin_was_enough():
    spike = LoadSpike(START + timedelta(minutes=1), 2, 10.0)
    assert spike_reaction_minutes(make_site_log([130, 140, 140, 130, 130]), spike, 150) == 0.0


def test_van_shortfall_is_the_missing_battery_energy():
    van = Van("V01", START, START + timedelta(hours=5), 5.0)
    # Left with 70 %: 20 % of 75 kWh = 15 kWh missing to reach 90 %.
    results = van_results([van], [{"van_id": "V01", "departure_state_of_charge_percent": 70.0}])
    assert results[0]["reached_target"] is False
    assert results[0]["shortfall_kwh"] == pytest.approx(15.0)


def test_true_site_power_uses_what_the_charger_really_drew():
    power_log = [{"start": START, "end": START + timedelta(minutes=10), "power_kw": 11.0}]
    assert charger_power_at(power_log, START + timedelta(minutes=5)) == 11.0
    assert charger_power_at(power_log, START + timedelta(minutes=10)) == 0.0
