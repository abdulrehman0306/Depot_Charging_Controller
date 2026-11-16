# depot-charging-controller

Software that charges a fleet of electric delivery vans on time, without going over the depot's grid connection limit, and as cheaply as possible.

This project is being built in stages. This README will be completed at the end, with results that come from real runs of the code.

## Current state: Stage 2, ten chargers and uncontrolled charging

- `src/controller/central_system.py`: the central controller (CSMS). It accepts charger connections over OCPP 2.0.1, tracks every charger, runs a planning loop every 15 simulated minutes, and adds up the site power (building + chargers) every simulated minute.
- `src/controller/strategies.py`: charging strategies. Only "uncontrolled" so far: no limits are sent and every van charges at full power.
- `src/chargers/simulated_charger.py`: one simulated 22 kW wallbox. It waits for its van, charges it at the power the controller allows, and lets it go at its departure time.
- `src/chargers/run_chargers.py`: starts all ten chargers at once.
- `src/chargers/van.py`: van battery model (75 kWh usable, 92 % charging efficiency, no charging curve).
- `src/scenarios.py`: the van schedule. **Synthetic**: random arrival times, departure times and arrival state of charge from made-up ranges in `settings.py`, with a fixed seed.
- `src/meter/building_load.py`: the building's own load. **Synthetic**: a made-up daily profile for a small depot in winter.
- `src/sim_clock.py`: the simulated clock. The controller owns it; chargers set their clocks from the `currentTime` in the controller's BootNotification and Heartbeat answers.
- `src/electrical.py`: kW to amps and back. Planning is in kW; the SetChargingProfile limit is in amps per phase (3 x 230 V), rounded down to 0.1 A.
- `settings.py`: all settings in one place.

OCPP messages used: BootNotification, Heartbeat, StatusNotification, TransactionEvent, MeterValues, SetChargingProfile. They are built with the `ocpp` Python library by The Mobility House.

## How to run

Set up once (Windows, from the repository folder):

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Terminal 1, start the controller:

```
python -m src.controller.central_system
```

Terminal 2, start the ten chargers:

```
python -m src.chargers.run_chargers
```

By default one simulated minute passes per real second, so one night (15:30 to 07:30) takes about 16 minutes. For a faster run, set the same speed in both terminals before starting, for example `set SIM_SPEED=600` (about 100 seconds per night). To see every raw OCPP message, also set `set PRINT_OCPP_MESSAGES=1`.

## Tests

```
python -m pytest -v
```

## Price data

Day-ahead prices will come from SMARD.de, using the loading code from my earlier project [battery-revenue-study](https://github.com/abdulrehman0306/battery-revenue-study).
