"""
A simulated OCPP 2.0.1 charging station with one charging point.

OCPP words used here:
- Charging station: the whole wallbox with its network connection.
- EVSE: one power output that can charge one vehicle. Our wallbox has one: EVSE 1.
- Connector: the plug on that EVSE. Ours has one: connector 1.
- Transaction: one charging session, from plug-in to unplug.

Stage 1 runs a fixed script: boot, report "available", a van plugs in,
charges for a while at whatever power the controller allows, then unplugs.

Run it (with the controller already running in another terminal):
    python -m src.chargers.simulated_charger
"""

import asyncio
import logging
import uuid
from datetime import timedelta

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

EVSE_ID = 1
CONNECTOR_ID = 1


def energy_after_step_wh(energy_meter_wh, charge_power_kw, step_minutes):
    """
    Add the energy delivered during one time step to the meter reading.

    In: meter reading [Wh], charging power during the step [kW], step length [simulated minutes].
    Out: new meter reading [Wh].
    """
    # Energy = power x time. kW x hours gives kWh, and x 1000 gives Wh,
    # which is the unit real charger meters report in.
    step_hours = step_minutes / 60
    return energy_meter_wh + charge_power_kw * step_hours * 1000


def build_meter_value(timestamp, charge_power_kw, energy_meter_wh, context):
    """
    Build one OCPP meter reading with the present power and the energy counter.

    In: simulated time (datetime), power [kW], meter reading [Wh], OCPP reading context.
    Out: a MeterValueType as a dict, ready to put in a message.
    """
    # Real meters report power in W and energy in Wh, so we convert kW to W here.
    # The unit is written into each value so the receiver never has to guess.
    return {
        "timestamp": timestamp.isoformat(),
        "sampled_value": [
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
        ],
    }


class SimulatedCharger(ChargePoint):
    """
    One simulated wallbox. It sends messages to the controller and obeys
    the power limits the controller sends back.

    The class inherits from the ocpp library's ChargePoint, which already knows
    how to pack, check and send OCPP messages. We only add the behaviour of a wallbox.
    """

    def __init__(self, charger_id, connection, max_power_kw=settings.CHARGER_MAX_POWER_KW,
                 simulation_speed=settings.SIMULATED_SECONDS_PER_REAL_SECOND):
        # Each charger gets its own logger name, so the printed conversation
        # shows clearly which side sent which message.
        super().__init__(charger_id, connection, logger=logging.getLogger(f"CHARGER {charger_id}"))
        self.max_power_kw = max_power_kw
        self.simulation_speed = simulation_speed

        # Until the controller sends a limit, the charger may use its full hardware power.
        self.power_limit_kw = max_power_kw
        self.charge_power_kw = 0.0
        self.energy_meter_wh = 0.0
        self.sim_time = settings.SIMULATION_START_TIME

        # Transaction state. seq_no counts the TransactionEvent messages of one
        # transaction, so the controller can notice a lost or repeated message.
        self.transaction_id = None
        self.seq_no = 0

        # One entry per simulated minute of charging: (time, power in kW).
        # Used by the tests to check what power was really drawn.
        self.power_log = []

    # -----------------------------------------------------------------------
    # Messages the charger sends to the controller
    # -----------------------------------------------------------------------

    async def boot(self):
        """
        Introduce the charger to the controller with a BootNotification.

        Out: heartbeat interval the controller asked for [real seconds].
        """
        request = call.BootNotification(
            charging_station={"vendor_name": "DepotSim", "model": "Wallbox-22kW"},
            reason=BootReasonEnumType.power_up,
        )
        response = await self.call(request)
        return response.interval

    async def heartbeat_loop(self, interval_s):
        """Send a Heartbeat every interval_s real seconds, forever, so the controller knows the link is alive."""
        while True:
            await self.call(call.Heartbeat())
            await asyncio.sleep(interval_s)

    async def send_status(self, connector_status):
        """Tell the controller if the connector is free (Available) or has a van (Occupied)."""
        await self.call(call.StatusNotification(
            timestamp=self.sim_time.isoformat(),
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
        reading = build_meter_value(self.sim_time, 0.0, self.energy_meter_wh, ReadingContextEnumType.sample_periodic)
        await self.call(call.MeterValues(evse_id=EVSE_ID, meter_value=[reading]))

    async def send_transaction_event(self, event_type, trigger_reason, charging_state, context, stopped_reason=None):
        """
        Send one TransactionEvent (Started, Updated or Ended) with a meter reading.

        In: OCPP event type, why it was sent, charging state, reading context,
            and for Ended only, why the session stopped.
        """
        transaction_info = {"transaction_id": self.transaction_id, "charging_state": charging_state}
        if stopped_reason is not None:
            transaction_info["stopped_reason"] = stopped_reason

        reading = build_meter_value(self.sim_time, self.charge_power_kw, self.energy_meter_wh, context)
        await self.call(call.TransactionEvent(
            event_type=event_type,
            timestamp=self.sim_time.isoformat(),
            trigger_reason=trigger_reason,
            seq_no=self.seq_no,
            transaction_info=transaction_info,
            meter_value=[reading],
            evse={"id": EVSE_ID, "connector_id": CONNECTOR_ID},
        ))
        self.seq_no += 1

    # -----------------------------------------------------------------------
    # A van arriving, charging and leaving
    # -----------------------------------------------------------------------

    async def plug_in_van(self):
        """A van plugs in: the connector becomes Occupied and a new transaction starts."""
        self.transaction_id = str(uuid.uuid4())
        self.seq_no = 0
        await self.send_status(ConnectorStatusEnumType.occupied)

        # The depot vans charge without an RFID card, so the session starts
        # as soon as the cable is plugged in.
        await self.send_transaction_event(
            TransactionEventEnumType.started,
            TriggerReasonEnumType.cable_plugged_in,
            ChargingStateEnumType.ev_connected,
            ReadingContextEnumType.transaction_begin,
        )

    async def charge_for(self, duration_minutes):
        """
        Charge the plugged-in van for a number of simulated minutes, one minute per step.

        Each step uses the newest limit from the controller, so a new
        SetChargingProfile takes effect within one simulated minute.
        """
        for minute in range(1, duration_minutes + 1):
            await self.apply_power_limit()
            await self.wait_one_simulated_minute()

            # Count the energy of the minute that just passed, then move the clock.
            self.energy_meter_wh = energy_after_step_wh(self.energy_meter_wh, self.charge_power_kw, 1)
            self.sim_time += timedelta(minutes=1)
            self.power_log.append((self.sim_time, self.charge_power_kw))

            # Regular meter report, so the controller always knows the real power.
            if minute % settings.METER_VALUE_INTERVAL_MINUTES == 0:
                await self.send_transaction_event(
                    TransactionEventEnumType.updated,
                    TriggerReasonEnumType.meter_value_periodic,
                    ChargingStateEnumType.charging,
                    ReadingContextEnumType.sample_periodic,
                )

    async def apply_power_limit(self):
        """
        Set the charging power to the latest allowed value and report it if it changed.

        The power is the controller's limit, but never more than the charger hardware can deliver.
        """
        new_power_kw = min(self.power_limit_kw, self.max_power_kw)
        if new_power_kw == self.charge_power_kw:
            return

        # Going from 0 to some power means charging has just begun.
        # Otherwise only the charging rate changed.
        if self.charge_power_kw == 0:
            trigger_reason = TriggerReasonEnumType.charging_state_changed
        else:
            trigger_reason = TriggerReasonEnumType.charging_rate_changed

        self.logger.info("%s: charging power %.1f kW -> %.1f kW", self.id, self.charge_power_kw, new_power_kw)
        self.charge_power_kw = new_power_kw
        await self.send_transaction_event(
            TransactionEventEnumType.updated,
            trigger_reason,
            ChargingStateEnumType.charging,
            ReadingContextEnumType.sample_periodic,
        )

    async def wait_one_simulated_minute(self):
        """Sleep for the real time that equals one simulated minute at the chosen simulation speed."""
        await asyncio.sleep(60 / self.simulation_speed)

    async def unplug_van(self):
        """The van leaves: the transaction ends and the connector is free again."""
        self.charge_power_kw = 0.0
        await self.send_transaction_event(
            TransactionEventEnumType.ended,
            TriggerReasonEnumType.ev_departed,
            ChargingStateEnumType.idle,
            ReadingContextEnumType.transaction_end,
            stopped_reason=ReasonEnumType.ev_disconnected,
        )
        self.transaction_id = None

        # The controller's limit belonged to that transaction, so it no longer applies.
        self.power_limit_kw = self.max_power_kw
        await self.send_status(ConnectorStatusEnumType.available)

    # -----------------------------------------------------------------------
    # Messages the controller sends to the charger
    # -----------------------------------------------------------------------

    @on(Action.set_charging_profile)
    def on_set_charging_profile(self, evse_id, charging_profile, **kwargs):
        """
        Accept a new power limit from the controller.

        In: EVSE number and the OCPP charging profile.
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
        if not is_for_this_transaction or schedule["charging_rate_unit"] != ChargingRateUnitEnumType.watts:
            return call_result.SetChargingProfile(status=ChargingProfileStatusEnumType.rejected)

        # Our controller always sends one period that starts now, so this simple
        # charger only reads the first period and ignores the schedule start time.
        limit_w = schedule["charging_schedule_period"][0]["limit"]
        self.power_limit_kw = limit_w / 1000
        self.logger.info("%s: new limit from controller: %.1f kW", self.id, self.power_limit_kw)
        return call_result.SetChargingProfile(status=ChargingProfileStatusEnumType.accepted)


async def run_demo_session(charger_id, host, port, charge_minutes,
                           simulation_speed=settings.SIMULATED_SECONDS_PER_REAL_SECOND):
    """
    Connect one charger to the controller and run one complete charging session.

    In: charger name, controller address and port, how long the van charges
        [simulated minutes], simulation speed.
    Out: the SimulatedCharger object, so its final state can be checked.
    """
    # The charger name is part of the address. This is how the controller
    # knows which charger is on the other end of the connection.
    address = f"ws://{host}:{port}/{charger_id}"
    async with connect(address, subprotocols=[settings.OCPP_SUBPROTOCOL]) as websocket:
        charger = SimulatedCharger(charger_id, websocket, simulation_speed=simulation_speed)

        # Task 1: keep listening for messages from the controller (like SetChargingProfile).
        # It runs in the background while the session below goes on.
        listener_task = asyncio.create_task(charger.start())

        heartbeat_interval_s = await charger.boot()
        # Task 2: heartbeats in the background, also at the same time.
        heartbeat_task = asyncio.create_task(charger.heartbeat_loop(heartbeat_interval_s))

        await charger.send_status(ConnectorStatusEnumType.available)
        await charger.send_idle_meter_values()

        await charger.plug_in_van()
        await charger.charge_for(charge_minutes)
        await charger.unplug_van()

        # The session is over: stop the background tasks before the connection closes.
        heartbeat_task.cancel()
        listener_task.cancel()
    return charger


def main():
    """Run one charger with a 30 minute charging session against the controller from settings.py."""
    logging.basicConfig(level=logging.INFO, format="%(name)-14s | %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    asyncio.run(run_demo_session("CP01", settings.CSMS_HOST, settings.CSMS_PORT, charge_minutes=30))


if __name__ == "__main__":
    main()
