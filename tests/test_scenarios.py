"""
Checks every controlled strategy must pass in every scenario, on whole simulated nights.

"True" site power means the power that really flowed, rebuilt from what every charger
really drew plus the real building load. The controller never sees it; it only sees
the meter and the charger reports.
"""

from datetime import timedelta

import pytest

import settings
from src.results import minutes_over_limit, over_limit_episodes, van_results
from tests.conftest import run_scenario_night

STRATEGIES = ["equal_sharing", "smart"]
SCENARIOS_WITHOUT_SPIKE = ["normal_evening", "rush", "late_van", "charger_failure", "negative_prices", "impossible"]
SCENARIOS_WHERE_EVERY_VAN_CAN_MAKE_IT = ["normal_evening", "rush", "late_van", "charger_failure",
                                         "negative_prices", "load_spike"]


def all_completed_visits(night):
    """In: a finished night. Out: the completed van visits of all chargers in one list."""
    visits = []
    for charger in night["chargers"]:
        visits.extend(charger.completed_visits)
    return visits


@pytest.mark.night
@pytest.mark.parametrize("strategy_name", STRATEGIES)
@pytest.mark.parametrize("scenario_name", SCENARIOS_WITHOUT_SPIKE)
async def test_site_power_never_goes_over_the_limit(scenario_name, strategy_name):
    night = await run_scenario_night(scenario_name, strategy_name)
    assert minutes_over_limit(night["true_site_log"], settings.SITE_LIMIT_KW) == 0.0


@pytest.mark.night
@pytest.mark.parametrize("strategy_name", STRATEGIES)
async def test_load_spike_night_is_over_the_limit_only_during_the_spike(strategy_name):
    # The 40 kW spike is bigger than the 15 kW margin, so the site is over the limit until
    # the safety layer reacts (see test_equal_sharing_and_safety.py for the reaction time).
    night = await run_scenario_night("load_spike", strategy_name)
    spike = night["load_spikes"][0]
    episodes = over_limit_episodes(night["true_site_log"], settings.SITE_LIMIT_KW)
    for start, end in episodes:
        assert spike.start_time <= start < spike.start_time + timedelta(minutes=spike.duration_minutes)

    # If the site did go over the limit, the safety layer must have acted. (The smart
    # controller may have so little charging running at 19:30, when prices are high,
    # that the spike fits under the limit and no safety action is needed.)
    if episodes:
        assert len(night["controller"].safety_actions) > 0


@pytest.mark.night
@pytest.mark.parametrize("strategy_name", STRATEGIES)
@pytest.mark.parametrize("scenario_name", SCENARIOS_WITHOUT_SPIKE + ["load_spike"])
async def test_no_limit_between_0_and_6_amps_and_none_above_22_kw(scenario_name, strategy_name):
    night = await run_scenario_night(scenario_name, strategy_name)
    for sent in night["controller"].sent_limits:
        assert sent["limit_a"] == 0.0 or sent["limit_a"] >= settings.CHARGER_MIN_CURRENT_A
    for charger in night["chargers"]:
        assert max(step["power_kw"] for step in charger.power_log) <= settings.CHARGER_MAX_POWER_KW


@pytest.mark.night
@pytest.mark.parametrize("strategy_name", STRATEGIES)
@pytest.mark.parametrize("scenario_name", SCENARIOS_WHERE_EVERY_VAN_CAN_MAKE_IT)
async def test_every_van_reaches_90_percent_when_possible(scenario_name, strategy_name):
    night = await run_scenario_night(scenario_name, strategy_name)
    for result in van_results(night["vans"], all_completed_visits(night)):
        assert result["reached_target"], result


@pytest.mark.night
@pytest.mark.parametrize("strategy_name", STRATEGIES)
async def test_controller_recovers_after_a_charger_reconnects(strategy_name):
    night = await run_scenario_night("charger_failure", strategy_name)
    charger = night["chargers"][1]
    controller_view = night["controller"].chargers_by_id["CP02"]
    assert charger.id == "CP02"

    # The charger came back online (it lost its connection from 18:00 to 18:30).
    assert charger.is_online

    # The controller kept following it after the reconnect: it received the end of the
    # transaction, and its last meter reading matches the charger's own energy meter.
    assert controller_view.transaction_id is None
    assert controller_view.energy_meter_wh == pytest.approx(charger.energy_meter_wh, abs=0.1)

    # Its van still left with at least 90 %.
    result = van_results(night["vans"], all_completed_visits(night))[1]
    assert result["van_id"] == "V02"
    assert result["reached_target"]
