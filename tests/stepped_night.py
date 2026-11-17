"""
A night with the smart controller on a stepped clock: the same result on every run.

The full nights in conftest.py run the real programs over real WebSockets and Modbus
TCP, on a clock that follows real time (times the simulation speed). How long a solve
takes, or in which order Python runs the chargers, then changes when things happen in
simulated time, so two runs never give exactly the same numbers.

Here the clock only moves when this loop moves it, one simulated minute at a time.
The controller's own planning code is used (SiteController.smart_limits, the Pyomo/HiGHS
planner), and the vans charge with the same battery model, 0.1 A rounding and 6 A rule
as the simulated chargers. What is left out are the OCPP and Modbus connections; those
are tested by the full nights.
"""

from datetime import timedelta
from types import SimpleNamespace

import settings
from src.chargers.van import charge_van
from src.controller.run_controller import fleet_schedule_from_vans
from src.controller.site_control import SiteController
from src.electrical import charging_current_limit_a, current_a_to_power_kw
from src.meter.building_load import building_load_kw
from src.scenarios import get_scenario

FULL_STATE_OF_CHARGE_PERCENT = 99.999


class SteppedClock:
    """A simulated clock that only moves when the test moves it. No real time is involved."""

    def __init__(self, start_time):
        self.time = start_time

    def now(self):
        return self.time

    def advance_one_minute(self):
        self.time += timedelta(minutes=1)


def charger_power_kw(limit_kw):
    """In: the limit the controller sent [kW]. Out: what a charger really draws [kW] (0.1 A rounding, 6 A rule)."""
    return min(current_a_to_power_kw(charging_current_limit_a(limit_kw)), settings.CHARGER_MAX_POWER_KW)


async def run_stepped_smart_night(scenario_name, prices, plan_delays_minutes):
    """
    Run one night of a scenario with the smart controller, one simulated minute per step.

    In: scenario name, price Series, and how many minutes each plan's limits take to reach
        the chargers, as a list that is used in turn (plan 1 gets the first value, and so on,
        starting again at the end of the list). The list is fixed, so every run is the same.
        The old limits keep running until the new ones arrive.
    Out: dict with controller (the SiteController, with its shortfall forecasts) and
         departure_state_of_charge_percent (van id -> state of charge when it left).
    """
    scenario = get_scenario(scenario_name)
    clock = SteppedClock(scenario["start_time"])
    chargers = {}
    controller = SiteController(chargers, clock, None, "smart",
                                fleet_schedule=fleet_schedule_from_vans(scenario["vans"]), prices=prices)
    number_of_plans = 0

    van_by_charger = {f"CP{number:02d}": van for number, van in enumerate(scenario["vans"], start=1)}
    limits_kw = {}            # limits the chargers are using now
    waiting_limits = []       # (time they arrive at the chargers, limits)
    departure_soc = {}
    last_quarter = None
    planned_ids = set()
    last_departure = max(van.departure_time for van in scenario["vans"])

    while clock.now() <= last_departure:
        now = clock.now()
        plug_in_and_leave(van_by_charger, chargers, limits_kw, departure_soc, now)

        # The controller: plan at every new quarter hour, and when the vans change.
        controller.building_kw = building_load_kw(now, scenario["load_spikes"])
        wanting_ids = sorted(charger_id for charger_id, charger in chargers.items()
                             if charger.state_of_charge_percent < FULL_STATE_OF_CHARGE_PERCENT)
        quarter = (now.date(), now.hour, now.minute // 15)
        if quarter != last_quarter or set(wanting_ids) != planned_ids:
            new_limits = await controller.smart_limits(now, wanting_ids, 0.0, regular=(quarter != last_quarter))
            delay = timedelta(minutes=plan_delays_minutes[number_of_plans % len(plan_delays_minutes)])
            number_of_plans += 1
            waiting_limits.append((now + delay, new_limits))
            # The controller measures its own plan delays, as it does in the real program.
            controller.recent_plan_delays = (controller.recent_plan_delays + [delay])[-8:]
            last_quarter = quarter
            planned_ids = set(wanting_ids)

        # Limits reach the chargers after the fixed delay.
        for arrival_time, new_limits in list(waiting_limits):
            if arrival_time <= now:
                limits_kw.update(new_limits)
                waiting_limits.remove((arrival_time, new_limits))

        # One minute of charging. A new van starts at 0 A (the TxDefaultProfile).
        for charger_id, charger in chargers.items():
            power_kw = charger_power_kw(limits_kw.get(charger_id, 0.0))
            charger.state_of_charge_percent, _ = charge_van(charger.state_of_charge_percent, power_kw, 1 / 60)
        clock.advance_one_minute()

    return {"controller": controller, "departure_state_of_charge_percent": departure_soc}


def plug_in_and_leave(van_by_charger, chargers, limits_kw, departure_soc, now):
    """Plug in the vans that have arrived and let go the ones whose departure time has come."""
    for charger_id, van in van_by_charger.items():
        has_left = van.van_id in departure_soc
        if charger_id not in chargers and not has_left and van.arrival_time <= now:
            chargers[charger_id] = SimpleNamespace(
                van_id=van.van_id, transaction_id="tx-" + van.van_id, is_connected=True, charging_state="Charging",
                state_of_charge_percent=van.arrival_state_of_charge_percent,
                arrival_state_of_charge_percent=van.arrival_state_of_charge_percent)
        if charger_id in chargers and van.departure_time <= now:
            departure_soc[van.van_id] = chargers[charger_id].state_of_charge_percent
            del chargers[charger_id]
            limits_kw.pop(charger_id, None)
