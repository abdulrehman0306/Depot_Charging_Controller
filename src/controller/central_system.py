"""
The central controller: an OCPP 2.0.1 CSMS (charging station management system).

Chargers connect to it over WebSockets. It answers their messages and keeps
track of what each charger is doing. Three jobs run side by side:

1. One connection handler per charger, answering its OCPP messages.
2. The planning loop: every 15 simulated minutes, ask the charging strategy
   for each charger's power limit and send it with SetChargingProfile.
3. The site monitor: every simulated minute, read the building load from the
   energy meter over Modbus TCP, add the charging power, and compare the
   total with the grid connection limit.

The controller owns the simulated clock. Chargers set their clocks from it
over OCPP, and the meter through its clock registers.

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
from src.controller import strategies
from src.electrical import charging_current_limit_a
from src.controller.meter_reader import MeterReadError, create_meter_client, read_meter, write_meter_clock
from src.log_setup import setup_logging
from src.results import minutes_over_limit, peak_site_power
from src.sim_clock import SimulationClock

# The controller keeps one profile per charger and replaces it every cycle.
# Sending a profile with the same id replaces the old one on the charger.
CHARGING_PROFILE_ID = 1

site_events = logging.getLogger("CSMS")


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
    Build an OCPP 2.0.1 charging profile that caps the power of one transaction.

    In: transaction id, power limit [kW], schedule start time (ISO text).
    Out: a ChargingProfileType as a dict, with the limit in amps per phase.
    """
    # TxProfile:  the limit belongs to this one charging session only.
    # Absolute:   the schedule starts at a fixed clock time (start_schedule).
    # One period starting at 0 s: "from now on, at most limit_a on each of 3 phases".
    # The plan is in kW; real AC chargers are controlled in amps, so we convert here, at the edge.
    # The limit is always 0 A (pause) or at least 6 A, see charging_current_limit_a.
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
                "charging_rate_unit": ChargingRateUnitEnumType.amps,
                "charging_schedule_period": [{
                    "start_period": 0,
                    "limit": charging_current_limit_a(limit_kw),
                    "number_phases": settings.CHARGER_NUMBER_OF_PHASES,
                }],
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

    def __init__(self, charger_id, connection, clock):
        super().__init__(charger_id, connection, logger=logging.getLogger(f"ocpp.CSMS.{charger_id}"))
        self.clock = clock
        self.connector_status = "Unknown"
        self.transaction_id = None
        self.charge_power_kw = 0.0
        self.energy_meter_wh = 0.0
        self.state_of_charge_percent = None
        self.last_heartbeat_time = None

        # Every current limit sent to this charger: (simulated time, limit [A]).
        # Lets the tests check that no limit between 0 and 6 A was ever sent.
        self.sent_limits_a = []

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
        A charging session started, changed or ended. Keep the transaction id,
        because a power limit must name the transaction it is for.
        """
        if "meter_value" in kwargs:
            self.update_meter(kwargs["meter_value"])

        if event_type == TransactionEventEnumType.ended:
            # The van has gone: no transaction, no power, no battery to track.
            self.transaction_id = None
            self.charge_power_kw = 0.0
            self.state_of_charge_percent = None
        else:
            self.transaction_id = transaction_info["transaction_id"]
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


# ---------------------------------------------------------------------------
# Planning: sending power limits
# ---------------------------------------------------------------------------

async def send_power_limit(charger, limit_kw):
    """
    Send one charger a new power limit for its running transaction.

    In: ChargerConnection, power limit [kW].
    Out: the charger's answer, "Accepted" or "Rejected".
    """
    now = charger.clock.now()
    profile = build_charging_profile(charger.transaction_id, limit_kw, now.isoformat())
    response = await charger.call(call.SetChargingProfile(evse_id=1, charging_profile=profile))

    limit_a = profile["charging_schedule"][0]["charging_schedule_period"][0]["limit"]
    charger.sent_limits_a.append((now, limit_a))
    return response.status


async def planning_loop(connected_chargers, strategy, clock):
    """
    Every planning interval, ask the strategy for power limits and send them.

    In: dict of charger id -> ChargerConnection, a strategy function
        (see strategies.py), the simulated clock. Runs forever.
    """
    while True:
        await asyncio.sleep(clock.real_seconds(settings.PLANNING_INTERVAL_MINUTES))
        limits_kw = strategy(connected_chargers)

        for charger_id, limit_kw in limits_kw.items():
            # A charger may have disconnected, or its van left, since the plan was made.
            charger = connected_chargers.get(charger_id)
            if charger is not None and charger.transaction_id is not None:
                await send_power_limit(charger, limit_kw)


# ---------------------------------------------------------------------------
# Site monitor: total power against the grid limit
# ---------------------------------------------------------------------------

def take_site_sample(connected_chargers, sim_time, building_kw):
    """
    Add up the site's power right now.

    In: dict of connected chargers, simulated time, building load from the meter [kW].
    Out: one site log sample (dict with time, building_kw, charging_kw, site_kw).
    """
    charging_kw = sum(charger.charge_power_kw for charger in connected_chargers.values())
    return {
        "time": sim_time,
        "building_kw": building_kw,
        "charging_kw": charging_kw,
        "site_kw": building_kw + charging_kw,
    }


def log_site_sample(sample, number_charging):
    """Print one line about the site power, with a warning if it is over the limit."""
    line = (f"{sample['time'].strftime('%H:%M')} | {number_charging:2d} vans charging | "
            f"chargers {sample['charging_kw']:6.1f} kW | building {sample['building_kw']:5.1f} kW | "
            f"site {sample['site_kw']:6.1f} kW of {settings.SITE_LIMIT_KW:.0f} kW")
    if sample["site_kw"] > settings.SITE_LIMIT_KW:
        line += f" | OVER LIMIT by {sample['site_kw'] - settings.SITE_LIMIT_KW:.1f} kW"
    site_events.info(line)


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
        site_events.warning("meter not readable (%s), using %s", error,
                            "last reading" if last_building_kw is not None else "safe guess")
        if last_building_kw is not None:
            return last_building_kw
        return settings.BUILDING_LOAD_IF_METER_UNKNOWN_KW


async def site_monitor_loop(connected_chargers, clock, site_log, meter_client):
    """
    Every simulated minute, read the building meter and add a site sample to site_log.
    Print a line once per quarter hour so the output stays readable. Runs forever.
    """
    last_printed_quarter = None
    building_kw = None
    while True:
        now = clock.now()
        building_kw = await read_building_load_kw(meter_client, now, building_kw)
        sample = take_site_sample(connected_chargers, now, building_kw)
        site_log.append(sample)

        quarter = (sample["time"].hour, sample["time"].minute // 15)
        if quarter != last_printed_quarter:
            number_charging = sum(1 for charger in connected_chargers.values() if charger.charge_power_kw > 0)
            log_site_sample(sample, number_charging)
            last_printed_quarter = quarter

        await asyncio.sleep(clock.real_seconds(1))


def log_night_summary(site_log):
    """Print the peak site power and the time spent over the limit."""
    peak_kw, peak_time = peak_site_power(site_log)
    over_minutes = minutes_over_limit(site_log, settings.SITE_LIMIT_KW)
    site_events.info("summary: peak site power %.1f kW at %s, %.0f minutes over the %.0f kW limit",
                     peak_kw, peak_time.strftime("%H:%M"), over_minutes, settings.SITE_LIMIT_KW)


# ---------------------------------------------------------------------------
# Accepting chargers
# ---------------------------------------------------------------------------

async def handle_charger_connection(websocket, connected_chargers, clock, site_log):
    """
    Look after one charger from the moment it connects until it disconnects.

    In: the open WebSocket, the shared dict of connected chargers to add it to,
        the simulated clock, and the site log (for the summary when all chargers are gone).
    """
    # Both sides must have agreed on OCPP 2.0.1 when the connection opened.
    if websocket.subprotocol != settings.OCPP_SUBPROTOCOL:
        await websocket.close()
        return

    # The charger's name is the last part of the address it connected to, e.g. /CP01.
    charger_id = websocket.request.path.strip("/")
    charger = ChargerConnection(charger_id, websocket, clock)
    connected_chargers[charger_id] = charger

    # start() keeps reading messages and answering them until the connection closes.
    try:
        await charger.start()
    except ConnectionClosed:
        pass
    finally:
        del connected_chargers[charger_id]

    # When the last charger has gone, the night is over: print how it went.
    if not connected_chargers and site_log:
        log_night_summary(site_log)


async def start_csms_server(host, port, connected_chargers, clock, site_log):
    """
    Start listening for chargers.

    In: address and port to listen on (port 0 picks a free one, used in tests),
        the dict that will hold the connected chargers, the simulated clock, the site log.
    Out: the running server.
    """
    async def on_new_connection(websocket):
        await handle_charger_connection(websocket, connected_chargers, clock, site_log)

    return await serve(on_new_connection, host, port, subprotocols=[settings.OCPP_SUBPROTOCOL])


async def run_csms(strategy):
    """Start the server, the planning loop and the site monitor with the values from settings.py. Runs forever."""
    clock = SimulationClock(settings.SIMULATION_START_TIME, settings.SIMULATED_SECONDS_PER_REAL_SECOND)
    connected_chargers = {}
    site_log = []

    meter_client = create_meter_client(settings.METER_HOST, settings.METER_PORT)

    await start_csms_server(settings.CSMS_HOST, settings.CSMS_PORT, connected_chargers, clock, site_log)
    site_events.info("listening on ws://%s:%s, strategy: %s, simulation speed: %sx",
                     settings.CSMS_HOST, settings.CSMS_PORT, strategy.__name__,
                     settings.SIMULATED_SECONDS_PER_REAL_SECOND)
    site_events.info("reading building meter at %s:%s over Modbus TCP", settings.METER_HOST, settings.METER_PORT)

    # Run planning and monitoring side by side, forever.
    await asyncio.gather(
        planning_loop(connected_chargers, strategy, clock),
        site_monitor_loop(connected_chargers, clock, site_log, meter_client),
    )


def main():
    setup_logging()
    asyncio.run(run_csms(strategies.uncontrolled))


if __name__ == "__main__":
    main()
