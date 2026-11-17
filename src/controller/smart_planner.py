"""
The smart controller's planner: a small optimisation model (Pyomo, solved by HiGHS).

The question it answers every quarter hour:
    "How much power should each van get in each quarter hour from now until it
     leaves, so that every van reaches 90 % before departure, the site stays
     under its limit, and the electricity is as cheap as possible?"

Only the first quarter hour of the plan is sent to the chargers. A quarter hour
later the controller plans again with the newest state of charge, building load
and vans (a "rolling horizon"), so the plan never runs on old information.

If the optimiser fails (no answer, too slow, missing prices), a simple rule is
used instead: least laxity first (see fallback_least_laxity_first).
"""

import math
from datetime import timedelta

import pyomo.environ as pyo
from pyomo.opt import TerminationCondition

import settings
from src.chargers.van import grid_energy_to_reach_kwh
from src.controller.strategies import MAX_CHARGING_POWER_KW, MIN_CHARGING_POWER_KW

STEP_HOURS = 0.25

# Solver results like 1e-9 kW are rounding noise of the solver, not a real plan.
SOLVER_NOISE_KW = 1e-6

# A tiny cost per quarter hour of waiting. When two quarter hours have exactly the
# same price, the earlier one is chosen (less risk if something goes wrong later).
# It is far smaller than the smallest price step (0.01 EUR/MWh = 0.00001 EUR/kWh).
EARLIER_IS_BETTER_EUR_PER_KWH_PER_STEP = 1e-7


# ---------------------------------------------------------------------------
# Inputs the model needs, per van and per step
# ---------------------------------------------------------------------------

def planning_target_percent():
    """Out: the state of charge the plan aims for [%]: the 90 % target plus a small reserve."""
    return settings.TARGET_STATE_OF_CHARGE_PERCENT + settings.PLANNING_RESERVE_PERCENT


def reserve_kwh():
    """Out: the planning reserve as battery energy [kWh] (2 % of 75 kWh = 1.5 kWh)."""
    return settings.PLANNING_RESERVE_PERCENT / 100 * settings.VAN_BATTERY_USABLE_KWH


def hours_available(departure_time, step_start):
    """
    In: van departure time, start of a quarter hour step.
    Out: how many hours of that step the van is still there (0 to 0.25 h).
    """
    hours_left = (departure_time - step_start).total_seconds() / 3600
    return min(max(hours_left, 0.0), STEP_HOURS)


def laxity_hours(van, now):
    """
    How long a van could still wait and still reach the target, charging at full power after that.

    In: van dict (state_of_charge_percent, departure_time), the present time.
    Out: laxity [h]. Small or negative = urgent.
    """
    hours_until_departure = (van["departure_time"] - now).total_seconds() / 3600
    needed_kwh = grid_energy_to_reach_kwh(van["state_of_charge_percent"], settings.TARGET_STATE_OF_CHARGE_PERCENT)
    return hours_until_departure - needed_kwh / MAX_CHARGING_POWER_KW


def priority_order(vans):
    """
    The order in which vans are served when there is not enough energy for all of them.

    First the van that leaves first. Among vans that leave at the same time, the one that
    came back emptiest (lowest state of charge at plug-in). Last, the charger number, so
    the order is always the same.

    This order uses only what is known when the van arrives, so it does not change during
    the night. (An order that changed with every plan would keep swapping which vans charge,
    and the plan's forecast of who will be short would not hold.)

    Out: list of indexes into vans, most important first.
    """
    return sorted(range(len(vans)), key=lambda index: (vans[index]["departure_time"],
                                                       vans[index]["arrival_state_of_charge_percent"],
                                                       vans[index]["charger_id"]))


def shortfall_penalties(vans):
    """
    The cost of one missing kWh for each van, so the vans first in priority_order are served
    first when energy is short. The first van gets about twice the base penalty, the last one
    the base penalty.

    Out: list of penalties [EUR per kWh missing], in the same order as vans.
    """
    order = priority_order(vans)
    penalties = [0.0] * len(vans)
    for position, van_index in enumerate(order):
        penalties[van_index] = settings.SHORTFALL_PENALTY_EUR_PER_KWH * (2 - position / len(vans))
    return penalties


# ---------------------------------------------------------------------------
# The optimisation model
# ---------------------------------------------------------------------------

def build_model(vans, step_starts, capacity_kw, prices_eur_per_mwh, now):
    """
    Build the Pyomo model.

    In: vans (list of dicts with charger_id, van_id, state_of_charge_percent, departure_time),
        start time of each quarter hour step, charging power available in each step [kW],
        price of each step [EUR/MWh], the present time.
    Out: the Pyomo model, not solved yet.
    """
    model = pyo.ConcreteModel()
    model.vans = pyo.Set(initialize=range(len(vans)))
    model.steps = pyo.Set(initialize=range(len(step_starts)))

    # Inputs written as tables, so the constraints below read like the formulas.
    hours = {(v, t): hours_available(vans[v]["departure_time"], step_starts[t])
             for v in model.vans for t in model.steps}
    # The same, but only counting the time until 30 minutes before departure (the wished finish time).
    finish_early = timedelta(minutes=settings.PLANNING_FINISH_EARLY_MINUTES)
    hours_before_finish = {(v, t): hours_available(vans[v]["departure_time"] - finish_early, step_starts[t])
                           for v in model.vans for t in model.steps}
    needed_kwh = {v: grid_energy_to_reach_kwh(vans[v]["state_of_charge_percent"], planning_target_percent())
                  * settings.CHARGING_EFFICIENCY for v in model.vans}
    room_to_full_kwh = {v: grid_energy_to_reach_kwh(vans[v]["state_of_charge_percent"], 100.0)
                        * settings.CHARGING_EFFICIENCY for v in model.vans}
    penalties = shortfall_penalties(vans)

    # Decisions:
    #   power_kw[v, t]     charging power of van v in step t (0 to 21.94 kW, the most a charger can get)
    #   charging_now[v]    1 if van v charges in the first step, else 0 (for the 6 A rule)
    #   shortfall_kwh[v]   battery energy van v will be missing at departure (only if the target is impossible)
    #   late_kwh[v]        battery energy still missing 30 minutes before departure (allowed, but not wanted)
    model.power_kw = pyo.Var(model.vans, model.steps, bounds=(0, MAX_CHARGING_POWER_KW))
    model.charging_now = pyo.Var(model.vans, domain=pyo.Binary)
    model.shortfall_kwh = pyo.Var(model.vans, domain=pyo.NonNegativeReals)
    model.late_kwh = pyo.Var(model.vans, domain=pyo.NonNegativeReals)

    # No charging after the van has left.
    for v in model.vans:
        for t in model.steps:
            if hours[v, t] == 0:
                model.power_kw[v, t].fix(0)

    # 1. Site limit: all vans together get at most the power available in each step.
    def site_limit_rule(model, t):
        return sum(model.power_kw[v, t] for v in model.vans) <= capacity_kw[t]
    model.site_limit = pyo.Constraint(model.steps, rule=site_limit_rule)

    # 2. Target: energy into the battery before departure, plus any shortfall, reaches 90 % + reserve.
    def target_rule(model, v):
        stored_kwh = sum(model.power_kw[v, t] * hours[v, t] for t in model.steps) * settings.CHARGING_EFFICIENCY
        return stored_kwh + model.shortfall_kwh[v] >= needed_kwh[v]
    model.target = pyo.Constraint(model.vans, rule=target_rule)

    # 3. Finish early: energy stored up to 30 minutes before departure, plus late_kwh,
    #    reaches the target as well. late_kwh is allowed, but costs a penalty (see objective).
    def finish_early_rule(model, v):
        stored_early_kwh = sum(model.power_kw[v, t] * hours_before_finish[v, t]
                               for t in model.steps) * settings.CHARGING_EFFICIENCY
        return stored_early_kwh + model.late_kwh[v] >= needed_kwh[v]
    model.finish_early = pyo.Constraint(model.vans, rule=finish_early_rule)

    # 4. A battery cannot be filled above 100 %.
    def full_rule(model, v):
        stored_kwh = sum(model.power_kw[v, t] * hours[v, t] for t in model.steps) * settings.CHARGING_EFFICIENCY
        return stored_kwh <= room_to_full_kwh[v]
    model.full = pyo.Constraint(model.vans, rule=full_rule)

    # 5. The 6 A rule, only for the first step, because only that step is sent to the chargers:
    #    either 0, or between 4.14 kW and 21.94 kW.
    def minimum_power_rule(model, v):
        return model.power_kw[v, 0] >= MIN_CHARGING_POWER_KW * model.charging_now[v]
    model.minimum_power = pyo.Constraint(model.vans, rule=minimum_power_rule)

    def switch_off_rule(model, v):
        return model.power_kw[v, 0] <= MAX_CHARGING_POWER_KW * model.charging_now[v]
    model.switch_off = pyo.Constraint(model.vans, rule=switch_off_rule)

    # Objective: electricity cost + penalty for missing energy + penalty for finishing late
    # + a tiny preference for earlier. Price in EUR/MWh / 1000 = EUR/kWh.
    # Negative prices make charging earn money.
    def total_cost_rule(model):
        electricity_eur = sum(prices_eur_per_mwh[t] / 1000 * model.power_kw[v, t] * hours[v, t]
                              for v in model.vans for t in model.steps)
        shortfall_eur = sum(penalties[v] * model.shortfall_kwh[v] for v in model.vans)
        late_eur = sum(settings.FINISH_LATE_PENALTY_EUR_PER_KWH * model.late_kwh[v] for v in model.vans)
        waiting_eur = sum(EARLIER_IS_BETTER_EUR_PER_KWH_PER_STEP * t * model.power_kw[v, t] * hours[v, t]
                          for v in model.vans for t in model.steps)
        return electricity_eur + shortfall_eur + late_eur + waiting_eur
    model.total_cost = pyo.Objective(rule=total_cost_rule, sense=pyo.minimize)
    return model


def solve_model(model):
    """
    Solve the model with HiGHS.

    Out: True if an optimal answer was found and loaded into the model, else False.
    """
    solver = pyo.SolverFactory("highs")
    solver.options["time_limit"] = settings.SOLVER_TIME_LIMIT_S
    result = solver.solve(model, load_solutions=False, raise_exception_on_nonoptimal_result=False)
    if result.solver.termination_condition != TerminationCondition.optimal:
        return False
    model.solutions.load_from(result)
    return True


# ---------------------------------------------------------------------------
# The fallback rule
# ---------------------------------------------------------------------------

def fallback_least_laxity_first(vans, capacity_now_kw, now):
    """
    Simple rule used when the optimiser fails: the most urgent van first.

    Vans are sorted by laxity (see laxity_hours). In that order each van that still
    needs energy gets up to 21.94 kW until the available power is used up. A van gets
    either 0 or at least 4.14 kW (6 A rule). Prices are ignored.

    Out: dict charger id -> power limit [kW].
    """
    limits_kw = {}
    remaining_kw = capacity_now_kw
    for van in sorted(vans, key=lambda van: (laxity_hours(van, now), van["charger_id"])):
        needs_energy = grid_energy_to_reach_kwh(van["state_of_charge_percent"],
                                                settings.TARGET_STATE_OF_CHARGE_PERCENT) > 0
        if needs_energy and remaining_kw >= MIN_CHARGING_POWER_KW:
            limits_kw[van["charger_id"]] = min(MAX_CHARGING_POWER_KW, remaining_kw)
            remaining_kw -= limits_kw[van["charger_id"]]
        else:
            limits_kw[van["charger_id"]] = 0.0
    return limits_kw


# ---------------------------------------------------------------------------
# One plan
# ---------------------------------------------------------------------------

def warm_up_solver():
    """
    Solve one tiny model at start-up.

    The very first solve is several times slower than later ones, because Pyomo and
    HiGHS load and set themselves up. Doing that before the first van arrives means
    the first real plan is as fast as all the others.
    """
    model = pyo.ConcreteModel()
    model.power_kw = pyo.Var(bounds=(0, MAX_CHARGING_POWER_KW))
    model.charging_now = pyo.Var(domain=pyo.Binary)
    model.minimum_power = pyo.Constraint(expr=model.power_kw >= MIN_CHARGING_POWER_KW * model.charging_now)
    model.total_cost = pyo.Objective(expr=model.power_kw - model.charging_now, sense=pyo.minimize)
    solve_model(model)

def plan_steps(now, vans):
    """
    In: present time, vans. Out: start times of the quarter hour steps until the last van leaves.
    """
    latest_departure = max(van["departure_time"] for van in vans)
    number_of_steps = max(1, math.ceil((latest_departure - now).total_seconds() / (STEP_HOURS * 3600)))
    return [now + timedelta(hours=STEP_HOURS * step) for step in range(number_of_steps)]


def plan_charging(vans, step_starts, capacity_kw, prices_eur_per_mwh, now):
    """
    Make one charging plan.

    In: vans (list of dicts with charger_id, van_id, state_of_charge_percent, departure_time),
        step start times, available charging power per step [kW], price per step [EUR/MWh]
        (or None if prices are missing), the present time.
    Out: dict with
         limits_kw         charger id -> power limit for the first step [kW]
         shortfall_kwh     van id -> energy the plan expects the van to be missing at departure [kWh]
                           (None when the fallback rule was used: it does not look ahead)
         used_fallback     True if the fallback rule was used
         reason            why the fallback was used, or "optimal"
    """
    if not vans:
        return {"limits_kw": {}, "shortfall_kwh": {}, "used_fallback": False, "reason": "no vans"}

    reason = "optimal"
    try:
        if prices_eur_per_mwh is None:
            raise ValueError("prices missing for the planning horizon")
        model = build_model(vans, step_starts, capacity_kw, prices_eur_per_mwh, now)
        if solve_model(model):
            return read_plan(model, vans)
        reason = "the solver found no optimal answer in time"
    except Exception as error:
        # Any problem at all (solver missing, bad input) must never stop the charging.
        reason = f"optimiser error: {error}"

    return {"limits_kw": fallback_least_laxity_first(vans, capacity_kw[0], now),
            "shortfall_kwh": None, "used_fallback": True, "reason": reason}


def read_plan(model, vans):
    """Out: the plan dict (see plan_charging) from a solved model."""
    limits_kw = {}
    shortfall_kwh = {}
    for v in model.vans:
        power_kw = pyo.value(model.power_kw[v, 0])
        limits_kw[vans[v]["charger_id"]] = power_kw if power_kw > SOLVER_NOISE_KW else 0.0
        # The plan aims for 90 % + reserve. Only energy missing below 90 % is a real shortfall.
        shortfall_below_target_kwh = pyo.value(model.shortfall_kwh[v]) - reserve_kwh()
        shortfall_kwh[vans[v]["van_id"]] = max(0.0, shortfall_below_target_kwh)
    return {"limits_kw": limits_kw, "shortfall_kwh": shortfall_kwh, "used_fallback": False, "reason": "optimal"}
