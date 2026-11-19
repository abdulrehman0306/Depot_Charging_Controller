"""
Start the central controller: the OCPP server for the chargers, the control loop
that reads the meter and decides the power limits, the REST API, and (if an
InfluxDB token is set in .env) the writing of measurements to InfluxDB.

The controller owns the simulated clock. Chargers set their clocks from it
over OCPP, and the meter through its clock registers (or over MQTT, when the
building load is read over MQTT).

Start the meter first, then run:
    python -m src.controller.run_controller
"""

import asyncio
import logging
from datetime import datetime

import uvicorn

import settings
from src.api.app import create_app
from src.controller.central_system import start_csms_server
from src.controller.meter_reader import create_meter_client
from src.controller.mqtt_meter_reader import MqttMeterReader
from src.controller.site_control import SiteController
from src.controller.smart_planner import warm_up_solver
from src.controller.storage import MeasurementStore
from src.log_setup import setup_logging
from src.prices import load_prices
from src.scenarios import get_scenario
from src.sim_clock import SimulationClock


def default_limit_for_strategy(strategy_name):
    """
    In: strategy name.
    Out: the current [A] new transactions start with: None for uncontrolled
         (no limit, full power, as without any software), 0 A otherwise
         (a new van waits until the controller gives it a share).
    """
    if strategy_name == "uncontrolled":
        return None
    return 0.0


def fleet_schedule_from_vans(vans):
    """
    In: list of Van. Out: dict van id -> departure time.

    In a real depot this comes from the route planning system: which van leaves when.
    """
    schedule = {}
    for van in vans:
        schedule[van.van_id] = van.departure_time
    return schedule


async def run_controller():
    """Start the server and the control loop with the values from settings.py. Runs forever."""
    scenario = get_scenario(settings.SCENARIO)

    # Slow start-up work first (reading the price files, the solver's first solve), so it
    # does not eat simulated time: the simulated clock only starts after it.
    prices = load_prices()
    if settings.STRATEGY == "smart":
        warm_up_solver()
    clock = SimulationClock(scenario["start_time"], settings.SIMULATED_SECONDS_PER_REAL_SECOND)
    chargers_by_id = {}
    meter_client = create_building_load_source()

    await start_csms_server(settings.CSMS_HOST, settings.CSMS_PORT, chargers_by_id, clock,
                            default_limit_for_strategy(settings.STRATEGY))
    events = logging.getLogger("CSMS")
    events.info("listening on ws://%s:%s, strategy: %s, scenario: %s (night of %s), simulation speed: %sx",
                settings.CSMS_HOST, settings.CSMS_PORT, settings.STRATEGY, scenario["name"],
                scenario["night_date"], settings.SIMULATED_SECONDS_PER_REAL_SECOND)
    if settings.BUILDING_LOAD_SOURCE == "mqtt":
        events.info("reading the building load over MQTT, topic %s on %s:%s", meter_client.power_topic,
                    settings.MQTT_HOST, settings.MQTT_PORT)
    else:
        events.info("reading building meter at %s:%s over Modbus TCP", settings.METER_HOST, settings.METER_PORT)

    storage = create_storage()
    run_name = f"{datetime.now():%Y-%m-%d %H:%M:%S} {settings.STRATEGY} {scenario['name']}"
    controller = SiteController(chargers_by_id, clock, meter_client, settings.STRATEGY,
                                scenario_name=scenario["name"],
                                fleet_schedule=fleet_schedule_from_vans(scenario["vans"]),
                                prices=prices, storage=storage, run_name=run_name)

    # The REST API runs in the same program, on the same event loop, so it can read the
    # controller's state directly. uvicorn is the web server that runs the FastAPI app.
    api_server = uvicorn.Server(uvicorn.Config(create_app(controller), host=settings.API_HOST,
                                               port=settings.API_PORT, log_level="warning"))
    events.info("REST API on http://%s:%s (interactive page: /docs)", settings.API_HOST, settings.API_PORT)

    # Control loop and API side by side, forever.
    try:
        await asyncio.gather(controller.run(), api_server.serve())
    finally:
        if storage is not None:
            await storage.close()
        if isinstance(meter_client, MqttMeterReader):
            meter_client.stop()


def create_building_load_source():
    """
    Out: where the controller reads the building load, chosen by settings.BUILDING_LOAD_SOURCE:
         a Modbus TCP client for the meter (the default), or an MQTT reader (already started).
    """
    if settings.BUILDING_LOAD_SOURCE == "mqtt":
        reader = MqttMeterReader(settings.MQTT_HOST, settings.MQTT_PORT)
        reader.start()
        return reader
    return create_meter_client(settings.METER_HOST, settings.METER_PORT)


def create_storage():
    """
    Out: a MeasurementStore for InfluxDB, or None if no token is set (then nothing is stored).
    """
    events = logging.getLogger("STORAGE")
    if not settings.INFLUXDB_TOKEN:
        events.warning("no INFLUXDB_TOKEN in .env: measurements are not stored")
        return None
    events.info("writing measurements to InfluxDB at %s, bucket %s", settings.INFLUXDB_URL, settings.INFLUXDB_BUCKET)
    return MeasurementStore(settings.INFLUXDB_URL, settings.INFLUXDB_TOKEN, settings.INFLUXDB_ORG,
                            settings.INFLUXDB_BUCKET)


def main():
    setup_logging()
    asyncio.run(run_controller())


if __name__ == "__main__":
    main()
