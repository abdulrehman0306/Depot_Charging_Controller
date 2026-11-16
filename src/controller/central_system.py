"""
The central controller: an OCPP 2.0.1 CSMS (charging station management system).

Chargers connect to it over WebSockets. It answers their messages, keeps track
of what each charger is doing, and on a fixed cycle sends each charging van a
power limit with SetChargingProfile.

Stage 1: the limit is a fixed demo value, to show that the charger obeys it.
The real strategies (equal sharing, smart planning) come in later stages.

Run it:
    python -m src.controller.central_system
"""

import asyncio
import logging
from datetime import datetime, timezone

from ocpp.routing import on
from ocpp.v201 import ChargePoint, call, call_result
from ocpp.v201.enums import (
    Action,
    ChargingProfileKindEnumType,
    ChargingProfilePurposeEnumType,
    ChargingRateUnitEnumType,
    MeasurandEnumType,
    RegistrationStatusEnumType,
    TransactionEventEnumType,
)
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

import settings

# Stage 1 only: a fixed limit, half of the charger's 22 kW, so you can see
# the charger slow down when the controller tells it to.
DEMO_POWER_LIMIT_KW = 11.0

# The controller keeps one profile per charger and replaces it every cycle.
# Sending a profile with the same id replaces the old one on the charger.
CHARGING_PROFILE_ID = 1


def utc_now_iso():
    """Out: the current real time in UTC as an ISO 8601 text, the format OCPP uses."""
    return datetime.now(timezone.utc).isoformat()


def read_power_and_energy(meter_values):
    """
    Pull the charging power and the energy counter out of OCPP meter readings.

    In: list of MeterValueType dicts, as received in MeterValues or TransactionEvent.
    Out: (power [kW], energy meter [Wh]). A value is None if it was not in the readings.
    """
    power_kw = None
    energy_wh = None

    # A message can carry several readings; the last one is the newest.
    for reading in meter_values:
        for sample in reading["sampled_value"]:
            unit = sample.get("unit_of_measure", {}).get("unit")
            measurand = sample.get("measurand")

            # Chargers may send W or kW (and Wh or kWh). Convert to the units we work in.
            if measurand == MeasurandEnumType.power_active_import:
                power_kw = sample["value"] / 1000 if unit == "W" else sample["value"]
            elif measurand == MeasurandEnumType.energy_active_import_register:
                energy_wh = sample["value"] * 1000 if unit == "kWh" else sample["value"]
    return power_kw, energy_wh


def build_charging_profile(transaction_id, limit_kw, start_time_iso):
    """
    Build an OCPP 2.0.1 charging profile that caps the power of one transaction.

    In: transaction id, power limit [kW], schedule start time (ISO text).
    Out: a ChargingProfileType as a dict.
    """
    # TxProfile:  the limit belongs to this one charging session only.
    # Absolute:   the schedule starts at a fixed clock time (start_schedule).
    # One period starting at 0 s: "from now on, at most limit_w".
    # We use watts, not amps, because the whole project reasons in kW.
    return {
        "id": CHARGING_PROFILE_ID,
        "stack_level": 0,
        "charging_profile_purpose": ChargingProfilePurposeEnumType.tx_profile,
        "charging_profile_kind": ChargingProfileKindEnumType.absolute,
        "transaction_id": transaction_id,
        "charging_schedule": [
            {
                "id": 1,
                "start_schedule": start_time_iso,
                "charging_rate_unit": ChargingRateUnitEnumType.watts,
                "charging_schedule_period": [{"start_period": 0, "limit": limit_kw * 1000}],
            }
        ],
    }


class ChargerConnection(ChargePoint):
    """
    The controller's side of the connection to one charger, plus what the
    controller knows about that charger.

    The ocpp library's ChargePoint class is used on both ends of the link.
    Here it plays the controller role: the @on methods answer the charger.
    """

    def __init__(self, charger_id, connection):
        super().__init__(charger_id, connection, logger=logging.getLogger(f"CSMS    {charger_id}"))
        self.connector_status = "Unknown"
        self.transaction_id = None
        self.charge_power_kw = 0.0
        self.energy_meter_wh = 0.0
        self.last_heartbeat_utc = None

        # Newest time stamp the charger has sent us. The chargers run on a
        # simulated clock, so the controller uses their time for its schedules.
        self.charger_time_iso = None

    @on(Action.boot_notification)
    def on_boot_notification(self, charging_station, reason, **kwargs):
        """A charger has just started and introduces itself. Accept it and give it a heartbeat interval."""
        self.logger.info("%s: booted, %s %s", self.id, charging_station["vendor_name"], charging_station["model"])
        return call_result.BootNotification(
            current_time=utc_now_iso(),
            interval=settings.HEARTBEAT_INTERVAL_S,
            status=RegistrationStatusEnumType.accepted,
        )

    @on(Action.heartbeat)
    def on_heartbeat(self, **kwargs):
        """The charger says it is still alive. Remember when, and reply with the time."""
        self.last_heartbeat_utc = utc_now_iso()
        return call_result.Heartbeat(current_time=self.last_heartbeat_utc)

    @on(Action.status_notification)
    def on_status_notification(self, timestamp, connector_status, evse_id, connector_id, **kwargs):
        """The connector changed state, for example Available (free) or Occupied (van plugged in)."""
        self.connector_status = connector_status
        self.charger_time_iso = timestamp
        return call_result.StatusNotification()

    @on(Action.meter_values)
    def on_meter_values(self, evse_id, meter_value, **kwargs):
        """Meter readings sent outside a charging session."""
        self.update_meter(meter_value)
        return call_result.MeterValues()

    @on(Action.transaction_event)
    def on_transaction_event(self, event_type, timestamp, trigger_reason, seq_no, transaction_info, **kwargs):
        """
        A charging session started, changed or ended. Keep the transaction id,
        because a power limit must name the transaction it is for.
        """
        self.charger_time_iso = timestamp
        if event_type == TransactionEventEnumType.ended:
            self.transaction_id = None
        else:
            self.transaction_id = transaction_info["transaction_id"]

        if "meter_value" in kwargs:
            self.update_meter(kwargs["meter_value"])
        return call_result.TransactionEvent()

    def update_meter(self, meter_values):
        """Store the newest power [kW] and energy [Wh] the charger reported."""
        power_kw, energy_wh = read_power_and_energy(meter_values)
        if power_kw is not None:
            self.charge_power_kw = power_kw
        if energy_wh is not None:
            self.energy_meter_wh = energy_wh


async def send_power_limit(charger, limit_kw):
    """
    Send one charger a new power limit for its running transaction.

    In: ChargerConnection, power limit [kW].
    Out: the charger's answer, "Accepted" or "Rejected".
    """
    profile = build_charging_profile(charger.transaction_id, limit_kw, charger.charger_time_iso)
    response = await charger.call(call.SetChargingProfile(evse_id=1, charging_profile=profile))
    charger.logger.info("%s: limit %.1f kW -> %s", charger.id, limit_kw, response.status)
    return response.status


async def planning_loop(connected_chargers, limit_kw, interval_s):
    """
    Every interval_s real seconds, send a power limit to every charger that has a van charging.

    In: dict of charger id -> ChargerConnection, limit [kW], cycle time [real seconds].
    Runs forever.
    """
    while True:
        await asyncio.sleep(interval_s)

        # Copy the list first: a charger may connect or disconnect while we
        # are waiting for an answer, and the dict must not change under our feet.
        for charger in list(connected_chargers.values()):
            if charger.transaction_id is not None:
                await send_power_limit(charger, limit_kw)


async def handle_charger_connection(websocket, connected_chargers):
    """
    Look after one charger from the moment it connects until it disconnects.

    In: the open WebSocket, and the shared dict of connected chargers to add it to.
    """
    # Both sides must have agreed on OCPP 2.0.1 when the connection opened.
    if websocket.subprotocol != settings.OCPP_SUBPROTOCOL:
        await websocket.close()
        return

    # The charger's name is the last part of the address it connected to, e.g. /CP01.
    charger_id = websocket.request.path.strip("/")
    charger = ChargerConnection(charger_id, websocket)
    connected_chargers[charger_id] = charger
    logging.getLogger("CSMS").info("%s connected", charger_id)

    # start() keeps reading messages and answering them until the connection closes.
    try:
        await charger.start()
    except ConnectionClosed:
        logging.getLogger("CSMS").info("%s disconnected", charger_id)
    finally:
        del connected_chargers[charger_id]


async def start_csms_server(host, port, connected_chargers):
    """
    Start listening for chargers.

    In: address and port to listen on (port 0 picks a free one, used in tests),
        and the dict that will hold the connected chargers.
    Out: the running server.
    """
    async def on_new_connection(websocket):
        await handle_charger_connection(websocket, connected_chargers)

    return await serve(on_new_connection, host, port, subprotocols=[settings.OCPP_SUBPROTOCOL])


async def run_csms():
    """Start the server and the planning loop, using the values from settings.py. Runs forever."""
    connected_chargers = {}
    await start_csms_server(settings.CSMS_HOST, settings.CSMS_PORT, connected_chargers)
    logging.getLogger("CSMS").info("listening on ws://%s:%s", settings.CSMS_HOST, settings.CSMS_PORT)

    # 15 simulated minutes, converted into real seconds at the chosen simulation speed.
    planning_interval_s = settings.PLANNING_INTERVAL_MINUTES * 60 / settings.SIMULATED_SECONDS_PER_REAL_SECOND
    await planning_loop(connected_chargers, DEMO_POWER_LIMIT_KW, planning_interval_s)


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)-14s | %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)
    asyncio.run(run_csms())


if __name__ == "__main__":
    main()
