"""
Stage 4 tests: equal sharing, the 6 A rule over whole nights, and the safety layer.

"True" site power below means the power that really flowed, rebuilt from what
every charger really drew plus the real building load. The controller itself
never sees it; it only sees the meter and the charger reports.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest

import settings
from src.chargers.van import Van
from src.controller.site_control import SiteController, expected_charging_power_kw
from src.controller.strategies import equal_sharing
from src.meter.building_load import BUILDING_LOAD_BY_HOUR_KW
from src.results import minutes_over_limit, over_limit_episodes, spike_reaction_minutes, van_results
from src.scenarios import clock_time_on_day, get_scenario
from tests.conftest import run_night, run_scenario_night

DAY = settings.SIMULATION_START_TIME.date()
SCENARIOS_WITHOUT_SPIKE = ["normal_evening", "rush", "late_van", "impossible"]
SCENARIOS_WHERE_EVERY_VAN_CAN_MAKE_IT = ["normal_evening", "rush", "late_van", "load_spike"]


def all_completed_visits(night):
    """In: a finished night. Out: the completed van visits of all chargers in one list."""
    visits = []
    for charger in night["chargers"]:
        visits.extend(charger.completed_visits)
    return visits


# ---------------------------------------------------------------------------
# Equal sharing on its own
# ---------------------------------------------------------------------------

def test_equal_sharing_splits_the_power_equally():
    # 113 kW for 10 vans: 11.3 kW each.
    limits_kw = equal_sharing([f"CP{n:02d}" for n in range(1, 11)], 113.0)
    assert all(limit_kw == pytest.approx(11.3) for limit_kw in limits_kw.values())


def test_equal_sharing_gives_no_more_than_22_kw():
    limits_kw = equal_sharing(["CP01", "CP02"], 120.0)
    assert limits_kw == {"CP01": 22.0, "CP02": 22.0}


def test_equal_sharing_pauses_the_back_of_the_queue_when_power_is_short():
    # 23 kW is not enough for 10 x 4.14 kW. Only 5 vans can get the minimum: 23 / 5 = 4.6 kW each.
    queue = [f"CP{n:02d}" for n in range(1, 11)]
    limits_kw = equal_sharing(queue, 23.0)
    assert [limits_kw[charger_id] for charger_id in queue[:5]] == [pytest.approx(4.6)] * 5
    assert [limits_kw[charger_id] for charger_id in queue[5:]] == [0.0] * 5


def test_equal_sharing_pauses_everyone_below_the_minimum():
    assert equal_sharing(["CP01", "CP02"], 3.0) == {"CP01": 0.0, "CP02": 0.0}


# ---------------------------------------------------------------------------
# Safety margin, rotation and the safety estimate
# ---------------------------------------------------------------------------

def test_margin_covers_the_biggest_building_step_in_one_meter_interval():
    # The profile changes only on the hour, so one meter interval sees at most one step.
    # The list is a day; the step from 23:00 to 00:00 counts too.
    biggest_rise_kw = 0.0
    for hour in range(24):
        rise_kw = BUILDING_LOAD_BY_HOUR_KW[(hour + 1) % 24] - BUILDING_LOAD_BY_HOUR_KW[hour]
        biggest_rise_kw = max(biggest_rise_kw, rise_kw)
    assert biggest_rise_kw == 15
    assert settings.SAFETY_MARGIN_KW >= biggest_rise_kw


def test_rotation_moves_waiting_vans_to_the_front():
    # CP01 and CP02 charged in the last period, CP03 had to wait.
    chargers = {
        "CP01": SimpleNamespace(allowed_power_kw=4.6),
        "CP02": SimpleNamespace(allowed_power_kw=4.6),
        "CP03": SimpleNamespace(allowed_power_kw=0.0),
    }
    controller = SiteController(chargers, clock=None, meter_client=None, strategy_name="equal_sharing")
    controller.rotation_queue = ["CP01", "CP02", "CP03"]

    controller.update_rotation_queue(["CP01", "CP02", "CP03"], rotate=True)
    assert controller.rotation_queue == ["CP03", "CP01", "CP02"]

    # A van that has just arrived goes to the very front.
    chargers["CP04"] = SimpleNamespace(allowed_power_kw=0.0)
    controller.update_rotation_queue(["CP01", "CP02", "CP03", "CP04"], rotate=False)
    assert controller.rotation_queue == ["CP04", "CP03", "CP01", "CP02"]


def test_safety_estimate_counts_the_higher_of_reported_and_allowed_power():
    def charger(reported_kw, allowed_kw, state="Charging"):
        return SimpleNamespace(transaction_id="tx", charging_state=state,
                               charge_power_kw=reported_kw, allowed_power_kw=allowed_kw)

    # Told to go down to 11 kW but still reporting 22 kW: count 22 kW until it reports the change.
    assert expected_charging_power_kw(charger(22.0, 11.0)) == 22.0
    # Just allowed 11 kW but has not started yet: count 11 kW already.
    assert expected_charging_power_kw(charger(0.0, 11.0)) == 11.0
    # Full van: draws nothing.
    assert expected_charging_power_kw(charger(0.0, 11.0, state="SuspendedEV")) == 0.0


# ---------------------------------------------------------------------------
# Whole nights with equal sharing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario_name", SCENARIOS_WITHOUT_SPIKE)
async def test_site_power_never_goes_over_the_limit(scenario_name):
    night = await run_scenario_night(scenario_name, "equal_sharing")
    assert minutes_over_limit(night["true_site_log"], settings.SITE_LIMIT_KW) == 0.0


@pytest.mark.parametrize("scenario_name", SCENARIOS_WITHOUT_SPIKE + ["load_spike"])
async def test_no_limit_between_0_and_6_amps_and_none_above_22_kw(scenario_name):
    night = await run_scenario_night(scenario_name, "equal_sharing")
    for sent in night["controller"].sent_limits:
        assert sent["limit_a"] == 0.0 or sent["limit_a"] >= settings.CHARGER_MIN_CURRENT_A
    for charger in night["chargers"]:
        assert max(step["power_kw"] for step in charger.power_log) <= settings.CHARGER_MAX_POWER_KW


@pytest.mark.parametrize("scenario_name", SCENARIOS_WHERE_EVERY_VAN_CAN_MAKE_IT)
async def test_every_van_reaches_90_percent_when_possible(scenario_name):
    night = await run_scenario_night(scenario_name, "equal_sharing")
    for result in van_results(night["vans"], all_completed_visits(night)):
        assert result["reached_target"], result


async def test_impossible_case_reports_the_shortfall_correctly():
    night = await run_scenario_night("impossible", "equal_sharing")
    results = van_results(night["vans"], all_completed_visits(night))

    # Some vans are short: the grid connection cannot deliver enough before 01:00.
    assert sum(result["shortfall_kwh"] for result in results) > 0

    # Check every reported shortfall against the charger's own energy meter:
    # departure % = 5 % + grid energy x 0.92 / 75 kWh, shortfall = (90 % - departure %) x 75 kWh.
    for charger, result in zip(night["chargers"], results):
        stored_kwh = charger.energy_meter_wh / 1000 * settings.CHARGING_EFFICIENCY
        departure_percent = 5.0 + stored_kwh / settings.VAN_BATTERY_USABLE_KWH * 100
        expected_shortfall_kwh = max(0.0, 90.0 - departure_percent) / 100 * settings.VAN_BATTERY_USABLE_KWH
        assert result["shortfall_kwh"] == pytest.approx(expected_shortfall_kwh, abs=0.01)

    # The vans got at most what the connection can give: 2 h x 113 kW + 3 h x 120 kW = 586 kWh.
    total_grid_kwh = sum(charger.energy_meter_wh for charger in night["chargers"]) / 1000
    assert total_grid_kwh <= 586.0 + 0.5


async def test_no_van_waits_all_night_when_power_is_short():
    # With a 60 kW limit there is not enough power for every van at once (45 kW target minus
    # the building load leaves room for only 2 to 5 vans), so equal sharing has to rotate.
    night = await run_scenario_night("normal_evening", "equal_sharing", site_limit_kw=60.0)

    some_van_had_to_wait = False
    for charger in night["chargers"]:
        # Longest unbroken time this van stood at 0 kW while its battery was not full.
        longest_wait_minutes = 0.0
        current_wait_minutes = 0.0
        for step in charger.power_log:
            if step["power_kw"] == 0.0 and step["state_of_charge_percent"] < 99.9:
                current_wait_minutes += (step["end"] - step["start"]).total_seconds() / 60
                longest_wait_minutes = max(longest_wait_minutes, current_wait_minutes)
                some_van_had_to_wait = True
            else:
                current_wait_minutes = 0.0

        assert charger.energy_meter_wh > 0
        assert longest_wait_minutes <= 90, f"{charger.id} waited {longest_wait_minutes:.0f} minutes"

    # Make sure the test really tested rotation: there was a shortage.
    assert some_van_had_to_wait


# ---------------------------------------------------------------------------
# The load spike and the safety layer
# ---------------------------------------------------------------------------

async def test_load_spike_night_is_over_the_limit_only_during_the_spike():
    night = await run_scenario_night("load_spike", "equal_sharing")
    spike = night["load_spikes"][0]
    for start, end in over_limit_episodes(night["true_site_log"], settings.SITE_LIMIT_KW):
        assert spike.start_time <= start < spike.start_time + timedelta(minutes=spike.duration_minutes)
    assert len(night["controller"].safety_actions) > 0


async def test_safety_layer_brings_the_site_back_under_the_limit_in_time():
    # A short run (19:20 to 19:45, spike at 19:30) at a slow simulation speed: one simulated
    # minute = 200 real ms. Then the computer's own processing time (a few ms) is only
    # a few simulated seconds and does not distort the measured reaction.
    scenario = get_scenario("load_spike")
    spike = scenario["load_spikes"][0]
    leave_time = clock_time_on_day(DAY, "19:45")
    vans = [Van(van.van_id, van.arrival_time, leave_time, van.arrival_state_of_charge_percent)
            for van in scenario["vans"]]

    night = await run_night(vans, [spike], "equal_sharing", simulation_speed=300,
                            start_time=clock_time_on_day(DAY, "19:20"))

    # The true site power, which the controller cannot see. The time over the limit can be
    # at most: up to 1 minute until the meter shows the spike (it updates once a minute),
    # and up to 1 minute until the controller reads it (once a minute). The chargers
    # then follow the lower limit within seconds.
    # Plus a quarter minute for the processing time of the programs themselves.
    true_log_from_spike = [sample for sample in night["true_site_log"] if sample["time"] >= spike.start_time]
    true_minutes_over = minutes_over_limit(true_log_from_spike, settings.SITE_LIMIT_KW)
    assert 0 < true_minutes_over <= 2.25

    # Measured by the controller: from the spike start until its own site log is back under
    # the limit. Its log has one sample per cycle, which can add one more minute.
    reaction_minutes = spike_reaction_minutes(night["controller"].site_log, spike, settings.SITE_LIMIT_KW)
    assert reaction_minutes is not None
    assert reaction_minutes <= 3.25
