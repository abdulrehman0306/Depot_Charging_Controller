# depot-charging-controller

Software that charges a fleet of electric delivery vans on time, without going over the depot's grid connection limit, and as cheaply as possible.

This project is being built in stages. This README will be completed at the end, with results that come from real runs of the code.

## Current state: Stage 3, building meter over Modbus TCP

- `src/controller/central_system.py`: the central controller (CSMS). It accepts charger connections over OCPP 2.0.1, tracks every charger, runs a planning loop every 15 simulated minutes, and every simulated minute reads the building meter and adds up the site power (building + chargers).
- `src/controller/meter_reader.py`: the controller's Modbus TCP client for the building meter.
- `src/meter/modbus_meter.py`: a simulated building energy meter, a Modbus TCP server (pymodbus) on port 5020, unit id 1.
- `src/meter/registers.py`: the meter's register map and the packing of values into 16 bit registers (32 bit floats and scaled 32 bit integers over two registers, high word first). The map is written for this project in the style of common industrial meters; it is not a copy of a real meter's map.
- `src/controller/strategies.py`: charging strategies. Only "uncontrolled" so far: no limits are sent and every van charges at full power.
- `src/chargers/simulated_charger.py`: one simulated 22 kW wallbox. It waits for its van, charges it at the power the controller allows, and lets it go at its departure time.
- `src/chargers/run_chargers.py`: starts all ten chargers at once.
- `src/chargers/van.py`: van battery model (75 kWh usable, 92 % charging efficiency, no charging curve).
- `src/scenarios.py`: the van schedule. **Synthetic**: random arrival times, departure times and arrival state of charge from made-up ranges in `settings.py`, with a fixed seed.
- `src/meter/building_load.py`: the building's own load. **Synthetic**: a made-up daily profile for a small depot in winter.
- `src/sim_clock.py`: the simulated clock. The controller owns it. Chargers set their clocks from the `currentTime` in the controller's BootNotification and Heartbeat answers; the meter follows the time the controller writes into its clock registers (20-21).
- `src/electrical.py`: kW to amps and back. Planning is in kW; the SetChargingProfile limit is in amps per phase (3 x 230 V), rounded down to 0.1 A. Following IEC 61851-1, a charger is never given a limit between 0 and 6 A: below 6 A the van is paused (0 A).
- `settings.py`: all settings in one place.

OCPP messages used: BootNotification, Heartbeat, StatusNotification, TransactionEvent, MeterValues, SetChargingProfile. They are built with the `ocpp` Python library by The Mobility House.

## How to run

Set up once (Windows, from the repository folder):

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Terminal 1, start the building meter:

```
python -m src.meter.modbus_meter
```

Terminal 2, start the controller:

```
python -m src.controller.central_system
```

Terminal 3, start the ten chargers:

```
python -m src.chargers.run_chargers
```

By default one simulated minute passes per real second, so one night (15:30 to 07:30) takes about 16 minutes. For a faster run, set the same speed in all terminals before starting, for example `set SIM_SPEED=600` (about 100 seconds per night). To see every raw OCPP message, also set `set PRINT_OCPP_MESSAGES=1`.

## Tests

```
python -m pytest -v
```

## Price data

Day-ahead prices will come from SMARD.de, using the loading code from my earlier project [battery-revenue-study](https://github.com/abdulrehman0306/battery-revenue-study).
