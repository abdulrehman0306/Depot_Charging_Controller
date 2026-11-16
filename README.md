# depot-charging-controller

Software that charges a fleet of electric delivery vans on time, without going over the depot's grid connection limit, and as cheaply as possible.

This project is being built in stages. This README will be completed at the end, with results that come from real runs of the code.

## Current state: Stage 1, one charger talking OCPP 2.0.1

- `src/controller/central_system.py`: the central controller (CSMS). It accepts charger connections, answers their messages, and every 15 simulated minutes sends each charging van a power limit with `SetChargingProfile`. In Stage 1 the limit is a fixed demo value of 11 kW.
- `src/chargers/simulated_charger.py`: one simulated 22 kW wallbox. It boots, reports its status and meter, a van plugs in, charges at the power the controller allows, and unplugs.
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

Terminal 2, start one charger:

```
python -m src.chargers.simulated_charger
```

Every OCPP message is printed in both terminals. One simulated minute passes per real second, so the 30 minute charging session takes about 30 seconds.

## Tests

```
python -m pytest -v
```
