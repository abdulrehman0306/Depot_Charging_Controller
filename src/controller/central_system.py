"""
The central controller: an OCPP 2.0.1 CSMS (charging station management system).

This file is the OCPP side: chargers connect over WebSockets, the controller
answers their messages, keeps track of what each charger is doing, and sends
power limits with SetChargingProfile.

The decisions (how much power each van gets, the safety layer) are made in
site_control.py, once per simulated minute. The program is started from
run_controller.py.
"""

import logging

from ocpp.routing import after, on
from ocpp.v201 import ChargePoint, call, call_result
from ocpp.v201.enums import (
    Action,
    AuthorizationStatusEnumType,
    ChargingProfileKindEnumType,
    ChargingProfilePurposeEnumType,
    ChargingProfileStatusEnumType,
    ChargingRateUnitEnumType,
    MeasurandEnumType,
    RegistrationStatusEnumType,
    TransactionEventEnumType,
)
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

import settings
from src.electrical import charging_current_limit_a, current_a_to_power_kw

EVSE_ID = 1

# The controller keeps one TxProfile per charger and replaces it on every new plan.
# Sending a profile with the same id replaces the old one on the charger.
TX_PROFILE_ID = 1
TX_DEFAULT_PROFILE_ID = 2


def read_meter_values(meter_values):
    """
    Pull charging power, energy counter and state of charge out of OCPP meter readings.

    In: list of MeterValueType dicts, as received in MeterValues or TransactionEvent.
    Out: dict with "power_kw", "energy_wh" and "state_of_charge_percent".
         A value is None if it was not in the readings.
    """
    values = {"power_kw": None, "energy_wh": None, "state_of_charge_percent": None}

    # A message can carry several readings; the last one is the newest.
    for reading in meter_values:
        for sample in reading["sampled_value"]:
            unit = sample.get("unit_of_measure", {}).get("unit")
            measurand = sample.get("measurand")

            # Chargers may send W or kW (and Wh or kWh). Convert to the units we work in.
            if measurand == MeasurandEnumType.power_active_import:
                values["power_kw"] = sample["value"] / 1000 if unit == "W" else sample["value"]
            elif measurand == MeasurandEnumType.energy_active_import_register:
                values["energy_wh"] = sample["value"] * 1000 if unit == "kWh" else sample["value"]
            elif measurand == MeasurandEnumType.soc:
                values["state_of_charge_percent"] = sample["value"]
    return values


def build_charging_profile(transaction_id, limit_kw, start_time_iso):
    """
    Build an OCPP 2.0.1 TxProfile that caps the power of one transaction.

    In: transaction id, power limit [kW], schedule start time (ISO text).
    Out: a ChargingProfileType as a dict, with the limit in amps per phase.
    """
    # TxProfile:  the limit belongs to this one charging session only.
    # Absolute:   the schedule starts at a fixed clock time (start_schedule).
    # One period starting at 0 s: "from now on, at most limit_a on each of 3 phases".
    # The plan is in kW; real AC chargers are controlled in amps, so we convert here, at the edge.
    # The limit is always 0 A (pause) or at least 6 A, see charging_current_limit_a.
    return {
        "id": TX_PROFILE_ID,
        "stack_level": 0,
        "charging_profile_purpose": ChargingProfilePurposeEnumType.tx_profile,
        "charging_profile_kind": ChargingProfileKindEnumType.absolute,
        "transaction_id": transaction_id,
        "charging_schedule": [{
            "id": 1,
            "start_schedule": start_time_iso,
            "charging_rate_unit": ChargingRateUnitEnumType.amps,
            "charging_schedule_period": [{
                "start_period": 0,
                "limit": charging_current_limit_a(limit_kw),
                "number_phases": settings.CHARGER_NUMBER_OF_PHASES,
            }],
        }],
    }


def build_default_profile(limit_a):
    """
    Build an OCPP 2.0.1 TxDefaultProfile: the limit every new transaction starts with.

    In: current limit per phase [A].
    Out: a ChargingProfileType as a dict.
    """
    # Relative: the schedule starts when the transaction starts, so no clock time is needed.
    return {
        "id": TX_DEFAULT_PROFILE_ID,
        "stack_level": 0,
        "charging_profile_purpose": ChargingProfilePurposeEnumType.tx_default_profile,
        "charging_profile_kind": ChargingProfileKindEnumType.relative,
        "charging_schedule": [{
            "id": 1,
            "charging_rate_unit": ChargingRateUnitEnumType.amps,
            "charging_schedule_period": [{
                "start_period": 0,
                "limit": limit_a,
                "number_phases": settings.CHARGER_NUMBER_OF_PHASES,
            }],
        }],
    }


class ChargerConnection(ChargePoint):
    """
    The controller's side of the connection to one charger, plus what the
    controller knows about that charger.

    The ocpp library's ChargePoint class is used on both ends of the link.
    Here it plays the controller role: the @on methods answer the charger.
    """

    def __init__(self, charger_id, connection, clock, default_limit_a=None):
        """
        In: charger id, WebSocket, simulated clock, and the current limit [A] new
            transactions start with (None = send no default, the charger uses full power).
        """
        super().__init__(charger_id, connection, logger=logging.getLogger(f"ocpp.CSMS.{charger_id}"))
        self.clock = clock
        self.default_limit_a = default_limit_a

        # What the charger has told us.
        self.connector_status = "Unknown"
        self.transaction_id = None
        self.charging_state = None
        self.charge_power_kw = 0.0
        self.energy_meter_wh = 0.0
        self.state_of_charge_percent = None
        self.last_heartbeat_time = None

        # What the controller itself allows the charger right now.
        self.allowed_current_a = 0.0
        self.allowed_power_kw = 0.0

        # Every current limit sent to this charger: (simulated time, limit [A]).
        # Lets the tests check that no limit between 0 and 6 A was ever sent.
        self.sent_limits_a = []

        # Which van is plugged in (from its RFID card), its state of charge when it plugged in,
        # and whether the charger is connected now.
        self.van_id = None
        self.arrival_state_of_charge_percent = None
        self.is_connected = True

    def take_over_state_from(self, old_connection):
        """
        A charger has reconnected: carry over everything we knew from its old connection.

        Without this the controller would forget that a van is charging there, and would
        give that power to other vans while this one still draws it.
        """
        self.connector_status = old_connection.connector_status
        self.transaction_id = old_connection.transaction_id
        self.charging_state = old_connection.charging_state
        self.charge_power_kw = old_connection.charge_power_kw
        self.energy_meter_wh = old_connection.energy_meter_wh
        self.state_of_charge_percent = old_connection.state_of_charge_percent
        self.allowed_current_a = old_connection.allowed_current_a
        self.allowed_power_kw = old_connection.allowed_power_kw
        self.sent_limits_a = old_connection.sent_limits_a
        self.van_id = old_connection.van_id
        self.arrival_state_of_charge_percent = old_connection.arrival_state_of_charge_percent

    @on(Action.boot_notification)
    def on_boot_notification(self, charging_station, reason, **kwargs):
        """
        A charger has just started and introduces itself. Accept it, give it a
        heartbeat interval, and tell it the (simulated) time so its clock matches ours.
        """
        return call_result.BootNotification(
            current_time=self.clock.now().isoformat(),
            interval=settings.HEARTBEAT_INTERVAL_S,
            status=RegistrationStatusEnumType.accepted,
        )

    @after(Action.boot_notification)
    async def after_boot_notification(self, **kwargs):
        """
        Right after accepting a charger, tell it the limit new transactions must start with.

        An @after method runs once the answer has been sent, as its own task, so it
        may send a request of its own (an @on method may not: it would wait for itself).
        """
        if self.default_limit_a is None:
            return
        await self.call(call.SetChargingProfile(evse_id=EVSE_ID,
                                                charging_profile=build_default_profile(self.default_limit_a)))

    @on(Action.heartbeat)
    def on_heartbeat(self, **kwargs):
        """The charger says it is still alive. Remember when, and reply with the time."""
        self.last_heartbeat_time = self.clock.now()
        return call_result.Heartbeat(current_time=self.last_heartbeat_time.isoformat())

    @on(Action.status_notification)
    def on_status_notification(self, timestamp, connector_status, evse_id, connector_id, **kwargs):
        """The connector changed state, for example Available (free) or Occupied (van plugged in)."""
        self.connector_status = connector_status
        return call_result.StatusNotification()

    @on(Action.meter_values)
    def on_meter_values(self, evse_id, meter_value, **kwargs):
        """Meter readings sent outside a charging session."""
        self.update_meter(meter_value)
        return call_result.MeterValues()

    @on(Action.transaction_event)
    def on_transaction_event(self, event_type, timestamp, trigger_reason, seq_no, transaction_info, **kwargs):
        """
        A charging session started, changed or ended. Keep the transaction id
        (a power limit must name the transaction it is for) and the charging state.
        """
        if "meter_value" in kwargs:
            self.update_meter(kwargs["meter_value"])
        self.charging_state = transaction_info.get("charging_state")

        if event_type == TransactionEventEnumType.started:
            self.transaction_id = transaction_info["transaction_id"]
            # The van's RFID card tells us which van it is (see the fleet schedule).
            if kwargs.get("id_token") is not None:
                self.van_id = kwargs["id_token"]["id_token"]
            # The state of charge in the Started event is the one the van came back with.
            self.arrival_state_of_charge_percent = self.state_of_charge_percent
            # The new transaction starts with the default limit we gave the charger.
            if self.default_limit_a is None:
                self.set_allowed_current(settings.CHARGER_MAX_CURRENT_A)
            else:
                self.set_allowed_current(self.default_limit_a)
        elif event_type == TransactionEventEnumType.ended:
            # The van has gone: no transaction, no power, no battery to track.
            self.transaction_id = None
            self.charge_power_kw = 0.0
            self.state_of_charge_percent = None
            self.van_id = None
            self.arrival_state_of_charge_percent = None
            self.set_allowed_current(0.0)

        # OCPP 2.0.1: if the charger sent an idToken, the answer must say whether it is accepted.
        # Every van card of the depot is accepted.
        if kwargs.get("id_token") is not None:
            return call_result.TransactionEvent(id_token_info={"status": AuthorizationStatusEnumType.accepted})
        return call_result.TransactionEvent()

    def update_meter(self, meter_values):
        """Store the newest power [kW], energy [Wh] and state of charge [%] the charger reported."""
        values = read_meter_values(meter_values)
        if values["power_kw"] is not None:
            self.charge_power_kw = values["power_kw"]
        if values["energy_wh"] is not None:
            self.energy_meter_wh = values["energy_wh"]
        if values["state_of_charge_percent"] is not None:
            self.state_of_charge_percent = values["state_of_charge_percent"]

    def set_allowed_current(self, limit_a):
        """Remember the current [A] this charger is allowed, and the power [kW] that means."""
        self.allowed_current_a = limit_a
        self.allowed_power_kw = min(current_a_to_power_kw(limit_a), settings.CHARGER_MAX_POWER_KW)


async def send_power_limit(charger, limit_kw):
    """
    Send one charger a new power limit for its running transaction.

    In: ChargerConnection, power limit [kW].
    Out: the charger's answer, "Accepted" or "Rejected".
    """
    now = charger.clock.now()
    profile = build_charging_profile(charger.transaction_id, limit_kw, now.isoformat())
    response = await charger.call(call.SetChargingProfile(evse_id=EVSE_ID, charging_profile=profile))

    limit_a = profile["charging_schedule"][0]["charging_schedule_period"][0]["limit"]
    charger.sent_limits_a.append((now, limit_a))
    if response.status == ChargingProfileStatusEnumType.accepted:
        charger.set_allowed_current(limit_a)
    return response.status


async def handle_charger_connection(websocket, chargers_by_id, clock, default_limit_a):
    """
    Look after one charger from the moment it connects until it disconnects.

    In: the open WebSocket, the shared dict of all chargers to add it to,
        the simulated clock, the default current limit [A] for new transactions (or None).

    A charger that disconnects stays in the dict, marked as not connected: its van may
    still be charging with the last limit. If it reconnects, the new connection takes
    over what the controller knew about it.
    """
    # Both sides must have agreed on OCPP 2.0.1 when the connection opened.
    if websocket.subprotocol != settings.OCPP_SUBPROTOCOL:
        await websocket.close()
        return

    # The charger's name is the last part of the address it connected to, e.g. /CP01.
    charger_id = websocket.request.path.strip("/")
    charger = ChargerConnection(charger_id, websocket, clock, default_limit_a)
    events = logging.getLogger("CSMS")
    if charger_id in chargers_by_id:
        charger.take_over_state_from(chargers_by_id[charger_id])
        events.info("%s %s reconnected", clock.now().strftime("%H:%M:%S"), charger_id)
    chargers_by_id[charger_id] = charger

    # start() keeps reading messages and answering them until the connection closes.
    try:
        await charger.start()
    except ConnectionClosed:
        pass
    finally:
        charger.is_connected = False
        if charger.transaction_id is not None:
            events.warning("%s %s disconnected while a van is plugged in; counting it at %.1f kW until it is back",
                           clock.now().strftime("%H:%M:%S"), charger_id,
                           max(charger.charge_power_kw, charger.allowed_power_kw))


async def start_csms_server(host, port, chargers_by_id, clock, default_limit_a=None):
    """
    Start listening for chargers.

    In: address and port to listen on (port 0 picks a free one, used in tests),
        the dict that will hold all chargers (by id), the simulated clock,
        the default current limit [A] for new transactions (None = no default).
    Out: the running server.
    """
    async def on_new_connection(websocket):
        await handle_charger_connection(websocket, chargers_by_id, clock, default_limit_a)

    return await serve(on_new_connection, host, port, subprotocols=[settings.OCPP_SUBPROTOCOL])
