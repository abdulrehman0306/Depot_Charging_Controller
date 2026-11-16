"""
A simulated OCPP 2.0.1 charging station with one charging point.

OCPP words used here:
- Charging station: the whole wallbox with its network connection.
- EVSE: one power output that can charge one vehicle. Our wallbox has one: EVSE 1.
- Connector: the plug on that EVSE. Ours has one: connector 1.
- Transaction: one charging session, from plug-in to unplug.

Each charger waits for its van, charges it at whatever power the controller
allows (full power if the controller sends nothing), and lets it go at its
departure time. Time comes from the shared simulated clock (src/sim_clock.py).

All ten chargers are started together by src/chargers/run_chargers.py.
"""

import asyncio
import logging
import uuid

from ocpp.routing import on
from ocpp.v201 import ChargePoint, call, call_result
from ocpp.v201.enums import (
    Action,
    BootReasonEnumType,
    ChargingProfilePurposeEnumType,
    ChargingProfileStatusEnumType,
    ChargingRateUnitEnumType,
    ChargingStateEnumType,
    ConnectorStatusEnumType,
    MeasurandEnumType,
    ReadingContextEnumType,
    ReasonEnumType,
    TransactionEventEnumType,
    TriggerReasonEnumType,
)
from websockets.asyncio.client import connect

import settings
from src.chargers.van import charge_van
from src.electrical import current_a_to_power_kw
from src.sim_clock import SimulationClock

EVSE_ID = 1
CONNECTOR_ID = 1

# Above this state of charge the van counts as full and stops taking power.
# Not exactly 100 because of small rounding errors in floating point maths.
FULL_STATE_OF_CHARGE_PERCENT = 99.999


def build_meter_value(timestamp, charge_power_kw, energy_meter_wh, context, state_of_charge_percent=None):
    """
    Build one OCPP meter reading with the present power, the energy counter and,
    if a van is plugged in, its state of charge.

    In: simulated time (datetime), power [kW], meter reading [Wh], OCPP reading context,
        state of charge [%] or None.
    Out: a MeterValueType as a dict, ready to put in a message.
    """
    # Real meters report power in W and energy in Wh, so we convert kW to W here.
    # The unit is written into each value so the receiver never has to guess.
    sampled_values = [
        {
            "value": round(charge_power_kw * 1000, 1),
            "measurand": MeasurandEnumType.power_active_import,
            "context": context,
            "unit_of_measure": {"unit": "W"},
        },
        {
            "value": round(energy_meter_wh, 1),
            "measurand": MeasurandEnumType.energy_active_import_register,
            "context": context,
            "unit_of_measure": {"unit": "Wh"},
        },
    ]

    # Simplification: a real AC wallbox usually does NOT know the van's state of
    # charge (that needs ISO 15118). Real depots get it from the vans' telematics.
    # Our simulated charger simply reports it, to keep the project small.
    if state_of_charge_percent is not None:
        sampled_values.append({
            "value": round(state_of_charge_percent, 1),
            "measurand": MeasurandEnumType.soc,
            "context": context,
            "unit_of_measure": {"unit": "Percent"},
        })
    return {"timestamp": timestamp.isoformat(), "sampled_value": sampled_values}


class SimulatedCharger(ChargePoint):
    """
    One simulated wallbox. It sends messages to the controller and obeys
    the power limits the controller sends back.

    The class inherits from the ocpp library's ChargePoint, which already knows
    how to pack, check and send OCPP messages. We only add the behaviour of a wallbox.
    """

    def __init__(self, charger_id, connection, clock, max_power_kw=settings.CHARGER_MAX_POWER_KW):
        # The ocpp library prints every raw message on this logger (if switched on in settings).
        super().__init__(charger_id, connection, logger=logging.getLogger(f"ocpp.CHARGER.{charger_id}"))
        # Short, readable event lines go to this one.
        self.events = logging.getLogger(f"CHARGER {charger_id}")
        self.clock = clock
        self.max_power_kw = max_power_kw

        # Until the controller sends a limit, the charger may use its full hardware power.
        self.power_limit_kw = max_power_kw
        self.charge_power_kw = 0.0
        self.energy_meter_wh = 0.0

        # The van that is plugged in, and its battery level.
        self.van = None
        self.state_of_charge_percent = None

        # Transaction state. seq_no counts the TransactionEvent messages of one
        # transaction, so the controller can notice a lost or repeated message.
        self.transaction_id = None
        self.seq_no = 0

        # Bookkeeping for the charging loop.
        self.last_energy_update_time = None
        self.last_meter_report_time = None

        # One entry per charging step: (time, average power really drawn in that step [kW]).
        # Used by the tests to check what power was really drawn.
        self.power_log = []

    # -----------------------------------------------------------------------
    # Messages the charger sends to the controller
    # -----------------------------------------------------------------------

    async def boot(self):
        """
        Introduce the charger to the controller with a BootNotification,
        and set our clock to the controller's clock.

        Out: heartbeat interval the controller asked for [real seconds].
        """
        request = call.BootNotification(
            charging_station={"vendor_name": "DepotSim", "model": "Wallbox-22kW"},
            reason=BootReasonEnumType.power_up,
        )
        response = await self.call(request)
        self.clock.set_time_from_iso(response.current_time)
        return response.interval

    async def heartbeat_loop(self, interval_s):
        """
        Send a Heartbeat every interval_s real seconds, forever, so the controller
        knows the link is alive. Each answer also keeps our clock in step.
        """
        while True:
            response = await self.call(call.Heartbeat())
            self.clock.set_time_from_iso(response.current_time)
            await asyncio.sleep(interval_s)

    async def send_status(self, connector_status):
        """Tell the controller if the connector is free (Available) or has a van (Occupied)."""
        await self.call(call.StatusNotification(
            timestamp=self.clock.now().isoformat(),
            connector_status=connector_status,
            evse_id=EVSE_ID,
            connector_id=CONNECTOR_ID,
        ))

    async def send_idle_meter_values(self):
        """
        Report the meter while no van is charging.

        In OCPP 2.0.1, readings that belong to a charging session travel inside
        TransactionEvent messages. MeterValues is for readings outside a session,
        like this one.
        """
        reading = build_meter_value(self.clock.now(), 0.0, self.energy_meter_wh, ReadingContextEnumType.sample_periodic)
        await self.call(call.MeterValues(evse_id=EVSE_ID, meter_value=[reading]))

    async def send_transaction_event(self, event_type, trigger_reason, context, stopped_reason=None):
        """
        Send one TransactionEvent (Started, Updated or Ended) with a meter reading.

        In: OCPP event type, why it was sent, reading context,
            and for Ended only, why the session stopped.
        """
        now = self.clock.now()
        transaction_info = {"transaction_id": self.transaction_id, "charging_state": self.charging_state()}
        if stopped_reason is not None:
            transaction_info["stopped_reason"] = stopped_reason

        reading = build_meter_value(now, self.charge_power_kw, self.energy_meter_wh, context,
                                    self.state_of_charge_percent)
        await self.call(call.TransactionEvent(
            event_type=event_type,
            timestamp=now.isoformat(),
            trigger_reason=trigger_reason,
            seq_no=self.seq_no,
            transaction_info=transaction_info,
            meter_value=[reading],
            evse={"id": EVSE_ID, "connector_id": CONNECTOR_ID},
        ))
        self.seq_no += 1
        self.last_meter_report_time = now

    def charging_state(self):
        """
        Out: the OCPP charging state that describes the charger right now.

        EVConnected:   cable in, not charging yet.
        Charging:      energy is flowing.
        SuspendedEV:   the van does not want more (battery full).
        SuspendedEVSE: the charger holds back power (limit set to zero).
        Idle:          no van.
        """
        if self.van is None:
            return ChargingStateEnumType.idle
        if self.charge_power_kw > 0:
            return ChargingStateEnumType.charging
        if self.state_of_charge_percent >= FULL_STATE_OF_CHARGE_PERCENT:
            return ChargingStateEnumType.suspended_ev
        if self.last_energy_update_time is None:
            return ChargingStateEnumType.ev_connected
        return ChargingStateEnumType.suspended_evse

    # -----------------------------------------------------------------------
    # A van arriving, charging and leaving
    # -----------------------------------------------------------------------

    async def run_van_visit(self, van):
        """Wait for the van, charge it until it must leave, then let it go."""
        await self.wait_until(van.arrival_time)
        await self.plug_in_van(van)
        await self.charge_until(van.departure_time)
        await self.unplug_van()

    async def wait_until(self, sim_time):
        """Do nothing until the simulated clock reaches sim_time, checking once per simulated minute."""
        while self.clock.now() < sim_time:
            await asyncio.sleep(self.clock.real_seconds(1))

    async def plug_in_van(self, van):
        """A van plugs in: the connector becomes Occupied and a new transaction starts."""
        self.van = van
        self.state_of_charge_percent = van.arrival_state_of_charge_percent
        self.transaction_id = str(uuid.uuid4())
        self.seq_no = 0
        self.events.info("%s van %s plugged in, state of charge %.1f %%",
                         self.clock.now().strftime("%H:%M"), van.van_id, self.state_of_charge_percent)
        await self.send_status(ConnectorStatusEnumType.occupied)

        # The depot vans charge without an RFID card, so the session starts
        # as soon as the cable is plugged in.
        await self.send_transaction_event(
            TransactionEventEnumType.started,
            TriggerReasonEnumType.cable_plugged_in,
            ReadingContextEnumType.transaction_begin,
        )

    async def charge_until(self, departure_time):
        """
        Charge the plugged-in van until its departure time.

        Every simulated minute: count the energy of the minute that passed,
        then set the power for the next minute from the newest controller limit.
        """
        self.last_energy_update_time = self.clock.now()
        await self.apply_power_limit()

        while True:
            await asyncio.sleep(self.clock.real_seconds(1))
            # Never count energy past the departure time: the van has left by then.
            now = min(self.clock.now(), departure_time)
            self.update_energy(now)
            if now >= departure_time:
                return

            await self.apply_power_limit()

            # Regular meter report, so the controller always knows the real power.
            minutes_since_report = (now - self.last_meter_report_time).total_seconds() / 60
            if minutes_since_report >= settings.METER_VALUE_INTERVAL_MINUTES:
                await self.send_transaction_event(
                    TransactionEventEnumType.updated,
                    TriggerReasonEnumType.meter_value_periodic,
                    ReadingContextEnumType.sample_periodic,
                )

    def update_energy(self, now):
        """Add the energy delivered since the last update to the van and to the meter."""
        # A clock correction from a Heartbeat can move time back a tiny bit.
        # Never count negative time.
        duration_hours = max(0.0, (now - self.last_energy_update_time).total_seconds() / 3600)
        self.state_of_charge_percent, grid_energy_kwh = charge_van(
            self.state_of_charge_percent, self.charge_power_kw, duration_hours)
        self.energy_meter_wh += grid_energy_kwh * 1000
        self.last_energy_update_time = now

        # Average power over the step. Lower than the set power if the battery filled up during the step.
        # min() with the set power stops tiny floating point errors (22.000000000000004)
        # from showing a power the charger never really drew.
        if duration_hours > 0:
            average_power_kw = min(self.charge_power_kw, grid_energy_kwh / duration_hours)
            self.power_log.append((now, average_power_kw))

    async def apply_power_limit(self):
        """
        Set the charging power for the next step and report it if it changed.

        The power is the controller's limit, but never more than the charger
        hardware can deliver, and zero once the battery is full.
        """
        if self.state_of_charge_percent >= FULL_STATE_OF_CHARGE_PERCENT:
            new_power_kw = 0.0
        else:
            new_power_kw = min(self.power_limit_kw, self.max_power_kw)
        if new_power_kw == self.charge_power_kw:
            return

        # Starting or stopping is a change of charging state; anything else only changes the rate.
        if new_power_kw == 0 or self.charge_power_kw == 0:
            trigger_reason = TriggerReasonEnumType.charging_state_changed
        else:
            trigger_reason = TriggerReasonEnumType.charging_rate_changed

        if new_power_kw == 0 and self.state_of_charge_percent >= FULL_STATE_OF_CHARGE_PERCENT:
            self.events.info("%s van %s full", self.clock.now().strftime("%H:%M"), self.van.van_id)
        self.charge_power_kw = new_power_kw
        await self.send_transaction_event(
            TransactionEventEnumType.updated,
            trigger_reason,
            ReadingContextEnumType.sample_periodic,
        )

    async def unplug_van(self):
        """The van leaves: the transaction ends and the connector is free again."""
        self.charge_power_kw = 0.0
        self.events.info("%s van %s left, state of charge %.1f %%",
                         self.clock.now().strftime("%H:%M"), self.van.van_id, self.state_of_charge_percent)
        await self.send_transaction_event(
            TransactionEventEnumType.ended,
            TriggerReasonEnumType.ev_departed,
            ReadingContextEnumType.transaction_end,
            stopped_reason=ReasonEnumType.ev_disconnected,
        )
        self.transaction_id = None
        self.van = None
        self.state_of_charge_percent = None
        self.last_energy_update_time = None

        # The controller's limit belonged to that transaction, so it no longer applies.
        self.power_limit_kw = self.max_power_kw
        await self.send_status(ConnectorStatusEnumType.available)

    # -----------------------------------------------------------------------
    # Messages the controller sends to the charger
    # -----------------------------------------------------------------------

    @on(Action.set_charging_profile)
    def on_set_charging_profile(self, evse_id, charging_profile, **kwargs):
        """
        Accept a new current limit from the controller.

        In: EVSE number and the OCPP charging profile (limit in amps per phase).
        Out: Accepted, or Rejected if the profile does not fit this charger.
        """
        # A TxProfile is a limit for one running transaction only. If there is
        # no transaction, or it is for another one, the charger must refuse it.
        is_for_this_transaction = (
            evse_id == EVSE_ID
            and self.transaction_id is not None
            and charging_profile.get("transaction_id") == self.transaction_id
            and charging_profile["charging_profile_purpose"] == ChargingProfilePurposeEnumType.tx_profile
        )
        schedule = charging_profile["charging_schedule"][0]
        if not is_for_this_transaction or schedule["charging_rate_unit"] != ChargingRateUnitEnumType.amps:
            return call_result.SetChargingProfile(status=ChargingProfileStatusEnumType.rejected)

        # Our controller always sends one period that starts now, so this simple
        # charger only reads the first period and ignores the schedule start time.
        limit_a = schedule["charging_schedule_period"][0]["limit"]
        self.power_limit_kw = current_a_to_power_kw(limit_a)
        return call_result.SetChargingProfile(status=ChargingProfileStatusEnumType.accepted)


async def run_charger(charger_id, van, host, port, simulation_speed=settings.SIMULATED_SECONDS_PER_REAL_SECOND):
    """
    Connect one charger to the controller and look after one van visit.

    In: charger name, the van (or None for a charger that only connects),
        controller address and port, simulation speed.
    Out: the SimulatedCharger object, so its final state can be checked.
    """
    # The charger name is part of the address. This is how the controller
    # knows which charger is on the other end of the connection.
    address = f"ws://{host}:{port}/{charger_id}"
    async with connect(address, subprotocols=[settings.OCPP_SUBPROTOCOL]) as websocket:
        # The start time does not matter much: boot() sets the clock to the controller's time.
        clock = SimulationClock(settings.SIMULATION_START_TIME, simulation_speed)
        charger = SimulatedCharger(charger_id, websocket, clock)

        # Task 1: keep listening for messages from the controller (like SetChargingProfile).
        # It runs in the background while the van visit below goes on.
        listener_task = asyncio.create_task(charger.start())

        heartbeat_interval_s = await charger.boot()
        # Task 2: heartbeats in the background, also at the same time.
        heartbeat_task = asyncio.create_task(charger.heartbeat_loop(heartbeat_interval_s))

        await charger.send_status(ConnectorStatusEnumType.available)
        await charger.send_idle_meter_values()
        if van is not None:
            await charger.run_van_visit(van)

        # The visit is over: stop the background tasks before the connection closes.
        heartbeat_task.cancel()
        listener_task.cancel()
    return charger
