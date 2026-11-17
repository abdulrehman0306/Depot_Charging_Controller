**Work in progress.**

# Depot_Charging_Controller

Software that charges a fleet of electric delivery vans on time, without going over the depot's grid connection limit, and as cheaply as possible.

This project is being built in stages. This README will be completed at the end, with results that come from real runs of the code.

## Current state: Stage 6, storage, dashboard and API

### Programs

- `src/meter/modbus_meter.py`: a simulated building energy meter, a Modbus TCP server (pymodbus) on port 5020, unit id 1. It plays the building load of the chosen scenario.
- `src/controller/run_controller.py`: starts the central controller (CSMS) and the REST API.
  - `central_system.py`: the OCPP 2.0.1 side. It accepts the chargers, tracks what each one reports, and sends limits with SetChargingProfile.
  - `site_control.py`: the control loop and the safety layer (see below).
  - `strategies.py`: equal sharing.
  - `smart_planner.py`: the smart controller's optimisation model (Pyomo, solved by HiGHS) and its fallback rule.
  - `meter_reader.py`: the Modbus TCP client for the meter.
  - `storage.py`: writes every control cycle's measurements to InfluxDB.
- `src/api/app.py`: the REST API (FastAPI), served by the controller program.
- `grafana/`: the Grafana dashboard (`dashboards/depot_charging.json`) and the provisioning files that load it and the InfluxDB data source automatically when Grafana starts.
- `src/chargers/run_chargers.py`: starts one simulated 22 kW wallbox per van of the chosen scenario (`simulated_charger.py`), and prints each van's state of charge when it leaves.

### Data

- **Day-ahead prices: real.** German day-ahead prices for the DE-LU bidding zone, in quarter hours, from SMARD.de (series 4169). The download and loading code in `src/prices.py` is taken from my earlier project [Battery_Revenue_Study](https://github.com/abdulrehman0306/Battery_Revenue_Study). The two weekly files the scenarios need are kept in `data/prices/`.
  Source: Bundesnetzagentur | SMARD.de, licence CC BY 4.0.
- **Van schedules: synthetic.** `src/scenarios.py`: random arrival times, departure times and arrival state of charge from made-up ranges in `settings.py` with a fixed seed, or set by hand for a test situation.
- **Building load: synthetic.** `src/meter/building_load.py`: a made-up daily profile for a small depot in winter.

The scenarios play real nights from the price data:

| Scenario | Night | What happens |
|---|---|---|
| `normal_evening` | 14 to 15 January 2026 | vans arrive between 16:00 and 20:00 |
| `rush` | 14 to 15 January 2026 | all vans arrive between 19:00 and 19:30 |
| `late_van` | 14 to 15 January 2026 | one van arrives at 22:30 with 10 % and must leave at 05:00 |
| `charger_failure` | 14 to 15 January 2026 | charger CP02 loses its connection to the controller from 18:00 to 18:30 |
| `load_spike` | 14 to 15 January 2026 | the building draws 40 kW extra from 19:30 to 19:50 |
| `negative_prices` | 4 to 5 October 2025 | the night with the most negative prices in the data |
| `impossible` | 14 to 15 January 2026 | ten nearly empty vans, 20:00 to 01:00: not enough energy for all of them |

The negative prices at night are real but mild: 35 negative quarter hours between 20:00 and 06:00, the lowest -5.09 EUR/MWh. In this data, strongly negative prices happen at midday in spring and summer, when the vans are out on their routes.

### Supporting pieces

- `src/meter/registers.py`: the meter's register map, and how values are packed into 16 bit registers (32 bit floats and scaled 32 bit integers over two registers, high word first). It is written for this project in the style of common industrial meters; it is not a copy of a real meter's map.
- `src/sim_clock.py`: the simulated clock. The controller owns it. Chargers set their clocks from the `currentTime` in the controller's BootNotification and Heartbeat answers; the meter follows the time the controller writes into its clock registers (20-21).
- `src/electrical.py`: kW to amps and back. Planning is in kW; limits are sent in amps per phase (3 x 230 V), rounded down to 0.1 A. Following IEC 61851-1, a charger is never given a limit between 0 and 6 A: below 6 A the van is paused (0 A).
- `src/results.py`: scoring, the same functions for every strategy, including the energy cost at the day-ahead price.
- `settings.py`: all settings in one place.

OCPP messages used: BootNotification, Heartbeat, StatusNotification, TransactionEvent (with the van's RFID card as idToken), MeterValues, SetChargingProfile (TxProfile and TxDefaultProfile). They are built with the `ocpp` Python library by The Mobility House.

## Charging strategies

- **Uncontrolled**: no software. Every van charges at 22 kW from plug-in until full. No limit is enforced.
- **Equal sharing**: the power available for charging is split equally between the vans that still want power, at most 22 kW each:

  available for charging = site limit (150 kW) - safety margin (15 kW) - building load (from the meter)

  Because of the 6 A rule, a van gets either 0 or at least 4.14 kW. If there is not enough for every van, the vans at the front of a queue charge and the others pause. Every 15 minutes the queue rotates: vans that had to wait move to the front, so no van waits all night.
- **Smart controller**: plans every van's charging for the rest of the night with an optimisation model, see below.

## The smart controller

Every quarter hour (and when a van arrives, fills up or leaves, or the safety layer asks), the controller asks one question:

> How much power should each van get in each quarter hour from now until it leaves, so that every van reaches 90 % before departure, the site stays under its limit, and the electricity is as cheap as possible?

It answers it with a small linear optimisation model, written with Pyomo and solved by HiGHS (`src/controller/smart_planner.py`). In plain words:

- **What it decides**: the charging power of each van in each quarter hour until the last van leaves (up to about 60 quarter hours, 10 vans).
- **What it must respect**:
  - all vans together stay under the power available in each quarter hour: 135 kW minus the expected building load;
  - no van gets more than a charger can give (21.94 kW, which is 31.8 A after rounding);
  - no van is filled above 100 %;
  - in the first quarter hour (the one that is sent to the chargers), each van gets 0 or at least 4.14 kW (the 6 A rule; this is the only yes/no decision in the model).
- **What it tries to make small**, in this order of importance:
  1. energy missing at departure below 90 %, at 110 to 200 EUR per kWh depending on the van's priority (far more than any electricity price, so the model charges every van it can before it looks at prices);
  2. energy still missing 30 minutes before departure, at 1.10 to 2 EUR per kWh: the model wants every van finished early, so a short delay at the end cannot leave a van short;
  3. the cost of the electricity at the day-ahead price of each quarter hour.

Only the first quarter hour of the plan is sent to the chargers. The next quarter hour the controller plans again with the newest state of charge, meter reading and vans (a "rolling horizon").

Details that matter:

- **Departure times** come from the fleet schedule (in a real depot: the route planning system). The controller knows which van is on which charger from the van's RFID card, which the charger sends in the TransactionEvent that starts the session.
- **Building load ahead**: the controller is given the typical daily profile as its forecast. This is a best case: it knows the normal pattern but not unplanned events like the load spike. Those are handled by the safety layer. A real depot would forecast from its own meter history.
- **Reserve**: the plan aims for 92 %, not 90 %. Limits are rounded down to 0.1 A, and a planned power below 4.14 kW becomes 0 because of the 6 A rule; the 2 % (1.5 kWh) reserve absorbs these small losses.
- **Plan delay**: a plan's limits only work once the plan is solved and sent; until then the old limits keep running. The controller measures this delay on its recent plans (median of the last 8) and starts each new plan's first quarter hour that much later.
- **When not every van can make it** (the `impossible` scenario), the model decides who is served first: the van that leaves first, then the one that came back emptiest, then the charger number. These are known when the van arrives, so the order does not change during the night. The vans first in this order also get their energy earliest. The controller prints which vans will be short and by how much at every regular plan, hours before they leave.
- **Fallback**: if the optimiser fails (no answer within 10 seconds, an error, or missing prices), the controller uses a simple rule for that quarter hour: the most urgent van first (least laxity: the van that can least afford to wait), up to 21.94 kW each, until the power is used up.

## How the controller keeps the limit

The controller runs a control cycle once per simulated minute, like a PLC with a cyclic interrupt:

1. Read the building load from the meter.
2. Estimate the site power: the meter value, plus for every charging van the higher of the power it last reported over OCPP and the power the controller has allowed it. The controller does not wait for the meter to see a change it caused itself. A van it has just allowed to charge is counted at once. A van it has told to slow down is counted at its old power until the charger reports the change.
3. Make a new plan if it is a new quarter hour, if a van arrived, filled up or left, or (the safety layer) if the estimated site power is above the planning target of 135 kW.

More rules keep the limit while limits change:

- **A new van starts at 0 A.** At boot the controller sends every charger a TxDefaultProfile of 0 A. A van that plugs in between plans waits until the controller gives it a share, instead of starting at 22 kW.
- **Make room first, then use it.** Reductions are sent first, to all chargers at the same time. Increases are sent only after the reduced chargers have reported their lower power.
- **Never between 0 and 6 A**, as above.
- **A charger that loses its connection** keeps charging with its last limit (OCPP 2.0.1 behaviour) and keeps its TransactionEvents in a queue, marked `offline`. The controller keeps counting it at that power and leaves that power out of every plan. When the charger reconnects, it sends its queued events and the controller carries on with what it knew about it.

### The safety margin: 15 kW

The controller sees the building load only once per meter reading (once per simulated minute). If the building load jumps just after a reading, the controller cannot react until the next one, so the charging power planned before the jump is still flowing. To keep the site under 150 kW anyway, the controller plans only up to 150 - 15 = 135 kW, so a jump of up to 15 kW fits in the free margin.

Why 15 kW: it is the biggest step of the (synthetic) building profile between two meter readings, +15 kW at 05:00 and at 07:00. A test checks that the margin still covers the biggest step if the profile is changed. 5 % of the limit (7.5 kW) would not cover it. A real depot would choose the margin from measured meter data, for example the biggest one-interval rise seen over a year.

A bigger, abnormal jump, like the 40 kW load spike, is not covered. Covering it with the margin alone would block 40 kW of charging power all night. Instead, the safety layer cuts charging at the next meter reading.

### Reaction to the 40 kW load spike (equal sharing)

Measured in Stage 4 in the `load_spike` scenario. Spike at 19:30, while about 135 kW was flowing:

| Run | True time over the limit | Measured: spike start until the controller's site log is back under the limit |
|---|---|---|
| Test, 10 runs, simulation speed 300 | 2 minutes in every run (the true log has one-minute resolution) | 2.05 to 2.08 minutes |
| Live demo, simulation speed 600 | not measured | 2.6 minutes |

Where the time goes: up to 1 minute until the meter shows the spike (it updates once per simulated minute), and up to 1 minute until the controller reads it (once per simulated minute). The chargers then follow the lower limit within seconds. The controller's own log takes one sample per cycle, which can add up to one more minute to the measured value. A real system would read the meter every second or faster, so the same chain would take seconds, not minutes.

With the smart controller the same spike did not take the site over the limit. In the live demo (simulation speed 600) no van was charging at 19:30, because prices were high, so the site went from 22 kW to 62 kW and the safety layer had nothing to do.

## First results (Stage 5)

One run of each night, all ten vans, the real day-ahead prices of that night. The full comparison over all scenarios comes in Stage 8.

| Night | Strategy | Energy charged | Energy cost | Average price | Vans at 90 % or more | Minutes over 150 kW |
|---|---|---|---|---|---|---|
| 14 to 15 Jan 2026 | equal sharing | 561.1 kWh | 85.67 EUR | 15.27 ct/kWh | 10 of 10 | 0 |
| 14 to 15 Jan 2026 | smart | 517.3 kWh | 44.85 EUR | 8.67 ct/kWh | 10 of 10 | 0 |
| 4 to 5 Oct 2025 | equal sharing | 561.1 kWh | 0.49 EUR | 0.09 ct/kWh | 10 of 10 | 0 |
| 4 to 5 Oct 2025 | smart | 560.9 kWh | -2.29 EUR | -0.41 ct/kWh | 10 of 10 | 0 |

The energy cost is the day-ahead price only. Grid fees, taxes and levies are the same for every strategy and are left out. Equal sharing fills every van to 100 %, the smart controller stops at about 92 % unless prices are negative, so the fair comparison is the average price per kWh.

In the live demo of the January night (smart, simulation speed 600) no van charged before midnight, while prices were high (up to 224 EUR/MWh at 17:30). From 00:00 to 05:00 the vans charged with the full 120 kW, in the cheapest hours of the night (down to 82 EUR/MWh). All ten vans left with 92.0 to 95.0 %, the site peaked at 135.0 kW, and none of the 78 plans needed the fallback rule.

## Storage, dashboard and API

### InfluxDB

Every control cycle (one simulated minute) the controller writes three measurements to InfluxDB:

| Measurement | Tags | Fields |
|---|---|---|
| `site` | run | building_kw, charging_kw, site_kw, estimated_site_kw, site_limit_kw, target_kw |
| `charger` | run, charger_id, van_id | power_kw, allowed_power_kw, state_of_charge_percent, connected |
| `price` | run | price_eur_per_mwh |

The time of each point is the simulated time, so the dashboard shows the night's clock times and prices. Each run of the controller gets its own `run` tag (start time, strategy, scenario), because two runs of the same night have the same simulated times.

Writing never holds up the control loop: each write is a background task. If InfluxDB is down, the measurements are lost and a warning is printed (at the first failure and then every 60th), but charging control carries on. This was checked by running a whole night with InfluxDB switched off: 841 writes failed, and the night ran normally.

### Grafana dashboard

`grafana/dashboards/depot_charging.json` has five panels: site power against the grid limit and the planning target, building load, day-ahead price, charging power per charger, and state of charge per van. A drop-down at the top chooses the run; the newest run is shown first. The default time range is the normal night (14 to 15 January 2026); for the `negative_prices` scenario choose 4 to 5 October 2025.

Grafana loads the dashboard and the InfluxDB data source by itself when it starts, from `grafana/provisioning/`. The data source file holds no secrets: the token is filled in from the environment.

Checked live (InfluxDB 2.7.11 and Grafana 11.5.2 in Docker, started from `docker-compose.yml` with the values from `.env`): InfluxDB set itself up with the `depot` bucket and accepted the token; Grafana's own health check of the provisioned data source answered "datasource is working. 3 buckets found"; the "Depot charging" dashboard was loaded from the JSON file with its 5 panels and is the home page. One smart night of the normal scenario (simulation speed 600) then stored 816 control cycles: 4,896 site values, 30,797 charger values and 816 prices, with no failed writes. The highest site power stored in InfluxDB, 135.0 kW at 01:31, is the same as the controller's own end-of-night summary.

### REST API

The controller program also serves a REST API (FastAPI) on http://127.0.0.1:8000. FastAPI makes an interactive page with all endpoints at http://127.0.0.1:8000/docs.

| Endpoint | What it does |
|---|---|
| `GET /status` | the site now: simulated time, strategy, limit, planning target, building, charging and site power |
| `GET /chargers` | every charger: connected or not, van, power, allowed power, state of charge |
| `GET /vans` | every plugged-in van: charger, state of charge, departure time, the smart controller's planned shortfall |
| `PUT /site-limit` | change the grid limit, e.g. `{"site_limit_kw": 120}`, between 20 and 400 kW |

A new limit is used from the next control cycle. If the site is then above the new planning target (limit minus 15 kW), the safety layer cuts charging at once. In a live run (equal sharing, normal night, simulation speed 600) the limit was lowered from 150 to 100 kW at 19:36 while 6 vans were charging and the site drew 134.6 kW. The controller's next cycle, at 19:37:36, measured 34.6 kW over the new limit and cut charging; at 19:38:41 the site was back under 100 kW, and at 19:45 it drew 84.8 kW, just under the new 85 kW target. A request for 5 kW was refused with HTTP 422 and changed nothing. Time over the limit is always counted against the limit in force at that moment.

### Secrets

The InfluxDB token and the passwords are kept out of git. They live in a file called `.env`, which is listed in `.gitignore`. The repository only has `.env.example` with placeholder values. Before the first start:

```
copy .env.example .env
```

and replace every `change-me` value in `.env` with your own (for the token, a long random text). `settings.py` reads `.env` when the programs start. Without a token, the controller runs without storing measurements.

## How to run

Set up once (Windows, from the repository folder):

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Create `.env` as described under "Secrets". Start InfluxDB and Grafana with Docker (Docker Desktop must be running):

```
docker compose up -d
```

Grafana is then at http://localhost:3000 (log in with `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD` from your `.env`), InfluxDB at http://localhost:8086.

Then start three programs in three terminals, in this order:

```
python -m src.meter.modbus_meter
python -m src.controller.run_controller
python -m src.chargers.run_chargers
```

Settings that can be changed without editing `settings.py`. Set them the same way in all three terminals before starting, for example:

```
set SIM_SPEED=600
set SCENARIO=load_spike
set STRATEGY=smart
```

- `SIM_SPEED`: simulated seconds per real second. The default 60 means one simulated minute per real second; 600 runs one night in about 100 seconds.
- `SCENARIO`: `normal_evening`, `rush`, `late_van`, `charger_failure`, `load_spike`, `negative_prices` or `impossible`.
- `STRATEGY`: `uncontrolled`, `equal_sharing` or `smart` (the default).
- `PRINT_OCPP_MESSAGES=1`: print every raw OCPP message.

To download the price files again (they are already in `data/prices/`):

```
python -m src.prices
```

## Tests

Quick tests (about 10 seconds), without the whole simulated nights:

```
python -m pytest -m "not night"
```

All tests (about 10 minutes):

```
python -m pytest
```

The smart controller's shortfall forecast is tested on a stepped clock (`tests/stepped_night.py`): the controller's own planning code and the same van battery model, but the simulated time only moves when the test moves it, one minute at a time, and each plan's limits arrive after a delay from a fixed list. So it gives the same result on every run. The test checks what matters to a dispatcher: every van that really leaves short was warned about at 21:00, four hours before; each van's forecast is within 3 kWh; the total is within 20 %.

The night tests run complete nights of every scenario with the real OCPP and Modbus connections. They check the true site power, rebuilt from what every charger really drew, not only what the controller measured. Nights with equal sharing run at 3000 times real time. Nights with the smart controller run at 1000 times real time, because each plan takes the optimiser about 80 real milliseconds; at 3000 times real time that would be 4 simulated minutes, far from reality (at the normal 60 times real time it is 5 simulated seconds).
