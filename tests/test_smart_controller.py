"""
The smart controller: the optimisation planner on small hand-made cases, the fallback
rule, whole nights compared with equal sharing on cost, and the shortfall forecast.
"""

from datetime import timedelta

import pytest

import settings
from src.controller import smart_planner
from src.controller.smart_planner import plan_charging, plan_steps
from src.controller.strategies import MAX_CHARGING_POWER_KW, MIN_CHARGING_POWER_KW
from src.prices import price_at
from src.results import charging_cost_eur
from tests.conftest import PRICES, run_scenario_night
from tests.stepped_night import run_stepped_smart_night

NOW = settings.SIMULATION_START_TIME


def make_van(charger_id, state_of_charge_percent, hours_until_departure):
    """A van for the planner: charger id, van id (V + charger number), state of charge (now and
    at plug-in), departure."""
    return {"charger_id": charger_id, "van_id": "V" + charger_id[2:],
            "state_of_charge_percent": state_of_charge_percent,
            "arrival_state_of_charge_percent": state_of_charge_percent,
            "departure_time": NOW + timedelta(hours=hours_until_departure)}


def plan(vans, capacity_kw, prices_by_hour):
    """
    Run the planner with the same capacity in every step and one price per hour.

    In: vans, capacity [kW], list of prices [EUR/MWh], one for each hour from now.
    Out: the planner's result dict.
    """
    step_starts = plan_steps(NOW, vans)
    prices = [prices_by_hour[step // 4] for step in range(len(step_starts))]
    return plan_charging(vans, step_starts, [capacity_kw] * len(step_starts), prices, NOW)


# ---------------------------------------------------------------------------
# The planner on small cases
# ---------------------------------------------------------------------------

def test_planner_waits_for_the_cheap_hours():
    # Needs about 22 kWh, has 4 hours; the last 2 hours are 4 times cheaper.
    van = make_van("CP01", 63.0, 4)
    result = plan([van], 120.0, [200.0, 200.0, 50.0, 50.0])
    assert result["used_fallback"] is False
    assert result["limits_kw"]["CP01"] == 0.0


def test_planner_charges_now_when_now_is_cheap():
    van = make_van("CP01", 63.0, 4)
    result = plan([van], 120.0, [50.0, 50.0, 200.0, 200.0])
    assert result["limits_kw"]["CP01"] == pytest.approx(MAX_CHARGING_POWER_KW)


def test_planner_keeps_the_site_limit_and_the_6_amp_rule():
    # Three urgent vans, but only 30 kW for all of them.
    vans = [make_van("CP01", 20.0, 1), make_van("CP02", 20.0, 1), make_van("CP03", 20.0, 1)]
    result = plan(vans, 30.0, [100.0])
    limits = result["limits_kw"].values()
    assert sum(limits) <= 30.0 + 1e-6
    assert all(limit == 0.0 or limit >= MIN_CHARGING_POWER_KW - 1e-6 for limit in limits)


def test_planner_reports_the_shortfall_of_an_impossible_van():
    # 10 % and leaves in 1 hour. At most 21.94 kW x 1 h x 0.92 = 20.19 kWh fit in.
    # 90 % needs 80 % of 75 kWh = 60 kWh, so 39.81 kWh will be missing.
    van = make_van("CP01", 10.0, 1)
    result = plan([van], 120.0, [100.0])
    assert result["shortfall_kwh"]["V01"] == pytest.approx(60.0 - MAX_CHARGING_POWER_KW * 0.92, abs=0.01)


def test_planner_serves_the_van_that_leaves_first_when_not_both_can_make_it():
    # Room for one van at a time. Both are nearly empty; CP02 leaves 30 minutes before CP01.
    vans = [make_van("CP01", 10.0, 1.5), make_van("CP02", 10.0, 1.0)]
    result = plan(vans, 22.0, [100.0, 100.0])
    assert result["limits_kw"]["CP02"] == pytest.approx(MAX_CHARGING_POWER_KW)
    assert result["limits_kw"]["CP01"] == 0.0


def test_planner_fills_up_a_van_when_it_is_paid_to_charge():
    # Already above 90 %, so no charging is needed, but the price is negative now.
    van = make_van("CP01", 91.0, 3)
    assert plan([van], 120.0, [-5.0, 80.0, 80.0])["limits_kw"]["CP01"] > 0
    assert plan([van], 120.0, [5.0, 80.0, 80.0])["limits_kw"]["CP01"] == 0.0


def test_fallback_is_used_when_prices_are_missing():
    # Room for one full van and one van at 8 kW. CP02 leaves first, so it is served first.
    vans = [make_van("CP01", 20.0, 8), make_van("CP02", 20.0, 4)]
    step_starts = plan_steps(NOW, vans)
    result = plan_charging(vans, step_starts, [30.0] * len(step_starts), None, NOW)
    assert result["used_fallback"] is True
    assert result["limits_kw"]["CP02"] == pytest.approx(MAX_CHARGING_POWER_KW)
    assert result["limits_kw"]["CP01"] == pytest.approx(30.0 - MAX_CHARGING_POWER_KW)


def test_fallback_is_used_when_the_solver_fails(monkeypatch):
    # Pretend HiGHS found no optimal answer.
    monkeypatch.setattr(smart_planner, "solve_model", lambda model: False)
    result = plan([make_van("CP01", 20.0, 4)], 120.0, [100.0, 100.0, 100.0, 100.0])
    assert result["used_fallback"] is True
    assert result["limits_kw"]["CP01"] == pytest.approx(MAX_CHARGING_POWER_KW)


# ---------------------------------------------------------------------------
# Whole nights
# ---------------------------------------------------------------------------

def night_cost_eur(night):
    """Out: the charging energy cost of a finished night [EUR]."""
    return charging_cost_eur([charger.power_log for charger in night["chargers"]], PRICES)


@pytest.mark.night
@pytest.mark.parametrize("scenario_name", ["normal_evening", "rush", "late_van", "charger_failure",
                                           "negative_prices"])
async def test_smart_never_costs_more_than_equal_sharing(scenario_name):
    smart = await run_scenario_night(scenario_name, "smart")
    equal = await run_scenario_night(scenario_name, "equal_sharing")
    assert night_cost_eur(smart) <= night_cost_eur(equal)


@pytest.mark.night
async def test_smart_charges_in_the_negative_price_quarter_hours():
    smart = await run_scenario_night("negative_prices", "smart")
    equal = await run_scenario_night("negative_prices", "equal_sharing")

    def energy_at_negative_prices_kwh(night):
        energy_kwh = 0.0
        for charger in night["chargers"]:
            for step in charger.power_log:
                if price_at(PRICES, step["start"]) < 0:
                    energy_kwh += step["power_kw"] * (step["end"] - step["start"]).total_seconds() / 3600
        return energy_kwh

    assert energy_at_negative_prices_kwh(smart) > energy_at_negative_prices_kwh(equal)
    # Charging was paid for overall: the night's charging cost is negative.
    assert night_cost_eur(smart) < 0


# The forecast is tested on a stepped clock (tests/stepped_night.py), so it gives the same
# result on every run. Each plan's limits reach the chargers after a delay from a fixed list.
# The lists copy what was measured in the full nights at test speed: usually about 2 simulated
# minutes, with single slow plans of 6 to 16 minutes.
MEASURED_LIKE_DELAYS_MINUTES = [2, 1, 2, 6, 2, 3, 2, 10]
HARSH_DELAYS_MINUTES = [2, 2, 16, 2, 4, 2, 10, 2]


def shortfall_forecast_and_result(night, forecast_hour):
    """
    In: a finished stepped night, the hour of the forecast to look at.
    Out: (forecast shortfall per van [kWh], real shortfall per van [kWh]).
    """
    forecast = [entry for entry in night["controller"].shortfall_forecasts
                if entry["time"].hour == forecast_hour][0]["shortfall_kwh"]
    real = {}
    for van_id, departure_percent in night["departure_state_of_charge_percent"].items():
        missing_percent = max(0.0, settings.TARGET_STATE_OF_CHARGE_PERCENT - departure_percent)
        real[van_id] = missing_percent / 100 * settings.VAN_BATTERY_USABLE_KWH
    return forecast, real


async def test_stepped_night_gives_the_same_result_every_time():
    first = await run_stepped_smart_night("impossible", PRICES, MEASURED_LIKE_DELAYS_MINUTES)
    second = await run_stepped_smart_night("impossible", PRICES, MEASURED_LIKE_DELAYS_MINUTES)
    assert first["departure_state_of_charge_percent"] == second["departure_state_of_charge_percent"]
    assert first["controller"].shortfall_forecasts == second["controller"].shortfall_forecasts


@pytest.mark.parametrize("plan_delays_minutes", [MEASURED_LIKE_DELAYS_MINUTES, HARSH_DELAYS_MINUTES])
async def test_smart_warns_in_advance_about_every_van_that_will_be_short(plan_delays_minutes):
    night = await run_stepped_smart_night("impossible", PRICES, plan_delays_minutes)
    # The forecast made at 21:00, four hours before the vans leave at 01:00.
    forecast, real = shortfall_forecast_and_result(night, forecast_hour=21)

    # What matters to a dispatcher: no missed warning. Every van that really left short
    # was forecast as short. (A warning for a borderline van that then just made it is fine.)
    for van_id, real_kwh in real.items():
        if real_kwh > 0.1:
            assert forecast[van_id] > 0.1, f"{van_id} left {real_kwh:.1f} kWh short without a warning"

    # Each van's forecast is within 3 kWh (4 % of the battery) of what really happened.
    for van_id, real_kwh in real.items():
        assert forecast[van_id] == pytest.approx(real_kwh, abs=3.0)

    # And the total is within 20 %.
    assert sum(forecast.values()) == pytest.approx(sum(real.values()), rel=0.2)
