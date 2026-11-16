"""
The controller's control loop and safety layer.

Once per simulated minute, like a PLC scan cycle:
1. Read the building load from the meter (Modbus TCP).
2. Log the measured site power: building + what the chargers report over OCPP.
3. Decide whether a new plan is needed, and if so, plan and send the limits:
   - every quarter hour (regular plan, the equal sharing queue rotates);
   - when the set of vans wanting power changes (a van arrived, filled up or left);
   - SAFETY: when the estimated site power is above the planning target.

Estimated site power (what the safety layer watches):
    building load from the meter
  + for every charging van: the higher of what it last reported and what the
    controller has allowed it.
Counting the allowed power means the controller does not have to wait for a
meter reading to know that a van it just allowed to charge will soon draw
that power. Counting the reported power means a van that has not yet obeyed
a lower limit is still counted at its old, higher power.

Planning target = site limit - safety margin (see SAFETY_MARGIN_KW in settings.py).
"""

import asyncio
import logging

from ocpp.v201.enums import ChargingStateEnumType

import settings
from src.controller.central_system import send_power_limit
from src.controller.meter_reader import MeterReadError, read_meter, write_meter_clock
from src.controller.strategies import equal_sharing
from src.electrical import charging_current_limit_a
from src.results import minutes_over_limit, over_limit_episodes, peak_site_power, spike_reaction_minutes
from src.scenarios import get_scenario

# Comparisons of power values allow this much difference, so floating point
# noise (like 135.00000001 kW) does not count as "over".
POWER_TOLERANCE_KW = 0.01


async def read_building_load_kw(meter_client, sim_time, last_building_kw):
    """
    Sync the meter's clock, then read the building load from it.

    In: Modbus client, simulated time, the last good reading [kW] or None.
    Out: building load [kW]. If the meter cannot be read: the last good reading,
         or a safe high guess if there never was one.
    """
    try:
        await write_meter_clock(meter_client, sim_time)
        return (await read_meter(meter_client))["power_kw"]
    except MeterReadError as error:
        logging.getLogger("CSMS").warning("meter not readable (%s), using %s", error,
                                          "last reading" if last_building_kw is not None else "safe guess")
        if last_building_kw is not None:
            return last_building_kw
        return settings.BUILDING_LOAD_IF_METER_UNKNOWN_KW


def wants_power(charger):
    """In: ChargerConnection. Out: True if a van is plugged in and not yet full."""
    # SuspendedEV means the van itself stopped taking power: its battery is full.
    return charger.transaction_id is not None and charger.charging_state != ChargingStateEnumType.suspended_ev


def expected_charging_power_kw(charger):
    """
    The power the safety layer counts for one charger [kW].

    The higher of what the charger last reported and what the controller has allowed:
    an allowed increase counts at once, an ordered decrease only once the charger reports it.
    """
    if not wants_power(charger):
        return 0.0
    return max(charger.charge_power_kw, charger.allowed_power_kw)


class SiteController:
    """
    The control loop for the whole site. A class is used because the loop has to
    remember things between cycles: the last building reading, the rotation queue,
    when it last planned, and the site log.
    """

    def __init__(self, connected_chargers, clock, meter_client, strategy_name,
                 site_limit_kw=settings.SITE_LIMIT_KW, safety_margin_kw=settings.SAFETY_MARGIN_KW,
                 scenario_name=None):
        self.connected_chargers = connected_chargers
        self.clock = clock
        self.meter_client = meter_client
        self.strategy_name = strategy_name
        self.site_limit_kw = site_limit_kw
        self.safety_margin_kw = safety_margin_kw
        # Only used to label the end-of-night report, never for decisions.
        self.scenario_name = scenario_name

        self.building_kw = None
        self.site_log = []
        self.safety_actions = []
        # Every limit sent: dicts with time, charger_id, limit_kw and limit_a.
        self.sent_limits = []

        # Equal sharing queue: front = next to charge when power is short.
        self.rotation_queue = []
        self.last_plan_quarter = None
        self.planned_charger_ids = set()
        # True if the last plan could not send its increases yet (see apply_limits).
        self.plan_is_unfinished = False

        self.last_printed_quarter = None
        self.had_chargers = False
        self.summary_printed = False
        self.events = logging.getLogger("CSMS")

    def target_kw(self):
        """Out: the highest site power the controller plans for [kW]: limit minus margin."""
        return self.site_limit_kw - self.safety_margin_kw

    def estimated_site_kw(self):
        """Out: building load from the meter + expected power of every charger [kW]."""
        charging_kw = sum(expected_charging_power_kw(charger) for charger in self.connected_chargers.values())
        return self.building_kw + charging_kw

    # -----------------------------------------------------------------------
    # The cycle
    # -----------------------------------------------------------------------

    async def run(self):
        """
        Run one control cycle per simulated minute, forever.

        Like a cyclic interrupt in a PLC (OB35) rather than a free cycle (OB1): each cycle
        starts one minute after the previous one started, however long the work took.
        """
        while True:
            cycle_start = self.clock.now()
            await self.control_cycle()
            seconds_left = 60 - (self.clock.now() - cycle_start).total_seconds()
            await asyncio.sleep(self.clock.real_seconds(max(0.0, seconds_left) / 60))

    async def control_cycle(self):
        """One scan: read the meter, log the site, plan if needed, report at the end of the night."""
        now = self.clock.now()
        self.building_kw = await read_building_load_kw(self.meter_client, now, self.building_kw)
        self.record_sample(now)

        # Uncontrolled means no software: no plans and no safety layer.
        if self.strategy_name != "uncontrolled":
            await self.plan_if_needed(now)

        self.report_if_night_is_over()

    async def plan_if_needed(self, now):
        """Make a new plan if it is a new quarter hour, the vans changed, or the safety layer says so."""
        wanting_ids = sorted(charger_id for charger_id, charger in self.connected_chargers.items()
                             if wants_power(charger))
        reason = None
        rotate = False

        quarter = (now.date(), now.hour, now.minute // 15)
        if quarter != self.last_plan_quarter:
            reason = "regular plan"
            rotate = True
            self.last_plan_quarter = quarter

        # The safety layer: do not wait for the next quarter hour if the site is over the target.
        estimated_kw = self.estimated_site_kw()
        if estimated_kw > self.target_kw() + POWER_TOLERANCE_KW:
            self.events.warning("%s SAFETY: estimated site power %.1f kW is above the %.1f kW target, "
                                "reducing charging now", now.strftime("%H:%M:%S"), estimated_kw, self.target_kw())
            self.safety_actions.append({"time": now, "estimated_site_kw": estimated_kw})
            reason = reason or "safety"
        elif set(wanting_ids) != self.planned_charger_ids:
            reason = reason or "vans changed"
        elif self.plan_is_unfinished:
            reason = reason or "finish the last plan"

        if reason is not None:
            await self.plan(wanting_ids, rotate)

    async def plan(self, wanting_ids, rotate):
        """Split the power available for charging between the vans and send the limits."""
        self.update_rotation_queue(wanting_ids, rotate)
        available_kw = self.target_kw() - self.building_kw
        limits_kw = equal_sharing(self.rotation_queue, available_kw)
        all_sent = await self.apply_limits(limits_kw)
        self.planned_charger_ids = set(wanting_ids)
        self.plan_is_unfinished = not all_sent

    def update_rotation_queue(self, wanting_ids, rotate):
        """
        Keep the equal sharing queue up to date.

        In: ids of chargers whose vans want power, and whether to rotate (only at regular plans).
        """
        # Forget vans that left or are full.
        queue = [charger_id for charger_id in self.rotation_queue if charger_id in wanting_ids]

        # Rotation: vans that were allowed to charge in the last period go to the back,
        # vans that had to wait come to the front. With enough power for everyone
        # nobody waits, and the order does not change.
        if rotate:
            waited = [charger_id for charger_id in queue if self.connected_chargers[charger_id].allowed_power_kw == 0]
            charged = [charger_id for charger_id in queue if charger_id not in waited]
            queue = waited + charged

        # Newly arrived vans have not charged yet, so they go to the front.
        new_ids = [charger_id for charger_id in wanting_ids if charger_id not in queue]
        self.rotation_queue = new_ids + queue

    # -----------------------------------------------------------------------
    # Sending limits: make room first, then use it
    # -----------------------------------------------------------------------

    async def apply_limits(self, limits_kw):
        """
        Send the new limits. Reductions go first; increases only once the reduced
        chargers have confirmed, so the site never briefly draws old and new power together.

        Out: True if every limit was sent, False if increases were held back.
        """
        decreases = []
        increases = []
        for charger_id, limit_kw in limits_kw.items():
            charger = self.connected_chargers.get(charger_id)
            if charger is None or charger.transaction_id is None:
                continue
            # Compare in amps, the unit actually sent, so rounding does not cause needless messages.
            new_limit_a = charging_current_limit_a(limit_kw)
            if new_limit_a < charger.allowed_current_a:
                decreases.append((charger, limit_kw))
            elif new_limit_a > charger.allowed_current_a:
                increases.append((charger, limit_kw))

        # Each charger has its own connection, so all reductions are sent at the same
        # time (asyncio.gather) instead of waiting for one charger's answer before
        # telling the next. With ten chargers that is the difference between
        # one round trip and ten in a row.
        await self.send_limits_together(decreases)
        if decreases:
            confirmed = await self.wait_for_reductions([charger for charger, _ in decreases])
            if not confirmed and increases:
                # Not safe to raise anyone yet. Try again at the next cycle, not only at the
                # next quarter hour, so the vans waiting for these increases are not left out.
                self.events.warning("not all chargers confirmed their reduction, holding back increases")
                return False

        await self.send_limits_together(increases)
        return True

    async def send_limits_together(self, chargers_and_limits):
        """In: list of (ChargerConnection, limit [kW]). Sends them all at the same time."""
        sending = [self.send_limit(charger, limit_kw) for charger, limit_kw in chargers_and_limits]
        await asyncio.gather(*sending)

    async def send_limit(self, charger, limit_kw):
        """Send one limit over OCPP and keep a record of it."""
        await send_power_limit(charger, limit_kw)
        self.sent_limits.append({"time": self.clock.now(), "charger_id": charger.id, "limit_kw": limit_kw,
                                 "limit_a": charging_current_limit_a(limit_kw)})

    async def wait_for_reductions(self, chargers):
        """
        Wait until every given charger reports a power at or below its new limit.

        In: list of ChargerConnection. Out: True if all confirmed within the timeout.
        """
        # Check four times per simulated minute. The timeout is counted in checks,
        # not by comparing clock times, so it also works when a test runs the
        # simulation so fast that one real sleep lasts several simulated minutes.
        number_of_checks = settings.REDUCTION_CONFIRM_TIMEOUT_MINUTES * 4
        for _ in range(number_of_checks):
            if self.all_reduced(chargers):
                return True
            await asyncio.sleep(self.clock.real_seconds(0.25))
        return self.all_reduced(chargers)

    def all_reduced(self, chargers):
        """In: list of ChargerConnection. Out: True if each one reports at most its allowed power."""
        for charger in chargers:
            # A charger that disconnected or whose van left or filled up draws nothing more.
            still_connected = self.connected_chargers.get(charger.id) is charger
            if still_connected and wants_power(charger) and \
                    charger.charge_power_kw > charger.allowed_power_kw + POWER_TOLERANCE_KW:
                return False
        return True

    # -----------------------------------------------------------------------
    # Logging and the end-of-night report
    # -----------------------------------------------------------------------

    def record_sample(self, now):
        """Store the measured site power and print a line each quarter hour or while over the limit."""
        charging_kw = sum(charger.charge_power_kw for charger in self.connected_chargers.values())
        sample = {
            "time": now,
            "building_kw": self.building_kw,
            "charging_kw": charging_kw,
            "site_kw": self.building_kw + charging_kw,
            "estimated_site_kw": self.estimated_site_kw(),
        }
        self.site_log.append(sample)

        quarter = (now.hour, now.minute // 15)
        is_over_limit = sample["site_kw"] > self.site_limit_kw + POWER_TOLERANCE_KW
        if quarter != self.last_printed_quarter or is_over_limit:
            number_charging = sum(1 for charger in self.connected_chargers.values() if charger.charge_power_kw > 0)
            line = (f"{now.strftime('%H:%M:%S')} | {number_charging:2d} vans charging | "
                    f"chargers {charging_kw:6.1f} kW | building {self.building_kw:5.1f} kW | "
                    f"site {sample['site_kw']:6.1f} kW of {self.site_limit_kw:.0f} kW")
            if is_over_limit:
                line += f" | OVER LIMIT by {sample['site_kw'] - self.site_limit_kw:.1f} kW"
            self.events.info(line)
            self.last_printed_quarter = quarter

    def report_if_night_is_over(self):
        """When the last charger has disconnected, print how the night went (once)."""
        if self.connected_chargers:
            self.had_chargers = True
            return
        if self.had_chargers and not self.summary_printed:
            self.log_night_summary()
            self.summary_printed = True

    def log_night_summary(self):
        """Print the peak site power, time over the limit, safety actions and spike reaction times."""
        peak_kw, peak_time = peak_site_power(self.site_log)
        self.events.info("summary: strategy %s, peak site power %.1f kW at %s, %.0f minutes over the %.0f kW limit, "
                         "%d safety actions", self.strategy_name, peak_kw, peak_time.strftime("%H:%M"),
                         minutes_over_limit(self.site_log, self.site_limit_kw), self.site_limit_kw,
                         len(self.safety_actions))
        for start, end in over_limit_episodes(self.site_log, self.site_limit_kw):
            end_text = end.strftime("%H:%M:%S") if end is not None else "end of log"
            self.events.info("summary: measured over the limit from %s to %s", start.strftime("%H:%M:%S"), end_text)

        # The spike times come from the scenario. They are used only here, to measure the reaction.
        if self.scenario_name is not None:
            for spike in get_scenario(self.scenario_name)["load_spikes"]:
                reaction = spike_reaction_minutes(self.site_log, spike, self.site_limit_kw)
                self.events.info("summary: load spike +%.0f kW at %s: site back under the limit after %s",
                                 spike.extra_power_kw, spike.start_time.strftime("%H:%M"),
                                 "never" if reaction is None else f"{reaction:.1f} minutes")
