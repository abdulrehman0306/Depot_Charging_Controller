**Work in progress.**

# Depot_Charging_Controller

Software that charges a fleet of electric delivery vans on time, without going over the depot's grid connection limit, and as cheaply as possible.

This project is being built in stages. This README will be completed at the end, with results that come from real runs of the code.

## Current state: Stage 4, equal sharing and the safety layer

### Programs

- `src/meter/modbus_meter.py`: a simulated building energy meter, a Modbus TCP server (pymodbus) on port 5020, unit id 1. It plays the building load of the chosen scenario.
- `src/controller/run_controller.py`: starts the central controller (CSMS).
  - `central_system.py`: the OCPP 2.0.1 side. It accepts the chargers, tracks what each one reports, and sends limits with SetChargingProfile.
  - `site_control.py`: the control loop and the safety layer (see below).
  - `strategies.py`: how the available power is split (uncontrolled, equal sharing).
  - `meter_reader.py`: the Modbus TCP client for the meter.
- `src/chargers/run_chargers.py`: starts one simulated 22 kW wallbox per van of the chosen scenario (`simulated_charger.py`), and prints each van's state of charge when it leaves.

### Data (all synthetic)

- `src/scenarios.py`: the van schedules. **Synthetic**: random arrival times, departure times and arrival state of charge from made-up ranges in `settings.py` with a fixed seed, or set by hand for a test situation. Scenarios so far: `normal_evening`, `rush`, `late_van`, `load_spike` (40 kW extra building load for 20 minutes at 19:30) and `impossible` (the vans need more energy than the connection can give before they leave). Charger failure and negative prices come in Stage 5.
- `src/meter/building_load.py`: the building's own load. **Synthetic**: a made-up daily profile for a small depot in winter.

### Supporting pieces

- `src/meter/registers.py`: the meter's register map, and how values are packed into 16 bit registers (32 bit floats and scaled 32 bit integers over two registers, high word first). It is written for this project in the style of common industrial meters; it is not a copy of a real meter's map.
- `src/sim_clock.py`: the simulated clock. The controller owns it. Chargers set their clocks from the `currentTime` in the controller's BootNotification and Heartbeat answers; the meter follows the time the controller writes into its clock registers (20-21).
- `src/electrical.py`: kW to amps and back. Planning is in kW; limits are sent in amps per phase (3 x 230 V), rounded down to 0.1 A. Following IEC 61851-1, a charger is never given a limit between 0 and 6 A: below 6 A the van is paused (0 A).
- `src/results.py`: scoring, the same functions for every strategy.
- `settings.py`: all settings in one place.

OCPP messages used: BootNotification, Heartbeat, StatusNotification, TransactionEvent, MeterValues, SetChargingProfile (TxProfile and TxDefaultProfile). They are built with the `ocpp` Python library by The Mobility House.

## Charging strategies

- **Uncontrolled**: no software. Every van charges at 22 kW from plug-in until full. No limit is enforced.
- **Equal sharing**: the power available for charging is split equally between the vans that still want power, at most 22 kW each:

  available for charging = site limit (150 kW) - safety margin (15 kW) - building load (from the meter)

  Because of the 6 A rule, a van gets either 0 or at least 4.14 kW. If there is not enough for every van, the vans at the front of a queue charge and the others pause. Every 15 minutes the queue rotates: vans that had to wait move to the front, so no van waits all night.

## How the controller keeps the limit

The controller runs a control cycle once per simulated minute, like a PLC with a cyclic interrupt:

1. Read the building load from the meter.
2. Estimate the site power: the meter value, plus for every charging van the higher of the power it last reported over OCPP and the power the controller has allowed it. The controller does not wait for the meter to see a change it caused itself. A van it has just allowed to charge is counted at once. A van it has told to slow down is counted at its old power until the charger reports the change.
3. Make a new plan if it is a new quarter hour, if a van arrived, filled up or left, or (the safety layer) if the estimated site power is above the planning target of 135 kW.

Three more rules keep the limit while limits change:

- **A new van starts at 0 A.** At boot the controller sends every charger a TxDefaultProfile of 0 A. A van that plugs in between plans waits until the controller gives it a share (at the next cycle), instead of starting at 22 kW.
- **Make room first, then use it.** Reductions are sent first, to all chargers at the same time. Increases are sent only after the reduced chargers have reported their lower power.
- **Never between 0 and 6 A**, as above.

### The safety margin: 15 kW

The controller sees the building load only once per meter reading (once per simulated minute). If the building load jumps just after a reading, the controller cannot react until the next one, so the charging power planned before the jump is still flowing. To keep the site under 150 kW anyway, the controller plans only up to 150 - 15 = 135 kW, so a jump of up to 15 kW fits in the free margin.

Why 15 kW: it is the biggest step of the (synthetic) building profile between two meter readings, +15 kW at 05:00 and at 07:00. A test checks that the margin still covers the biggest step if the profile is changed. 5 % of the limit (7.5 kW) would not cover it. A real depot would choose the margin from measured meter data, for example the biggest one-interval rise seen over a year.

A bigger, abnormal jump, like the 40 kW load spike, is not covered. Covering it with the margin alone would block 40 kW of charging power all night. Instead, the safety layer cuts charging at the next meter reading.

### Reaction to the 40 kW load spike

Measured in the `load_spike` scenario. Spike at 19:30, while about 135 kW was flowing:

| Run | True time over the limit | Measured: spike start until the controller's site log is back under the limit |
|---|---|---|
| Test, 10 runs, simulation speed 300 | 2 minutes in every run (the true log has one-minute resolution) | 2.05 to 2.08 minutes |
| Live demo, simulation speed 600 | not measured | 2.6 minutes |

In the live demo, the meter showed the spike at 19:30:26, the controller read it and cut charging at 19:31:32, and the measured site power was back under the limit at 19:32:38. Every van still left with 100 %.

Where the time goes: up to 1 minute until the meter shows the spike (it updates once per simulated minute), and up to 1 minute until the controller reads it (once per simulated minute). The chargers then follow the lower limit within seconds. The controller's own log takes one sample per cycle, which can add up to one more minute to the measured value. A real system would read the meter every second or faster, so the same chain would take seconds, not minutes. The one-minute steps here are a choice to keep the simulation simple.

## How to run

Set up once (Windows, from the repository folder):

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Start three programs in three terminals, in this order:

```
python -m src.meter.modbus_meter
python -m src.controller.run_controller
python -m src.chargers.run_chargers
```

Settings that can be changed without editing `settings.py`. Set them the same way in all three terminals before starting, for example:

```
set SIM_SPEED=600
set SCENARIO=load_spike
set STRATEGY=equal_sharing
```

- `SIM_SPEED`: simulated seconds per real second. The default 60 means one simulated minute per real second; 600 runs one night in about 100 seconds.
- `SCENARIO`: `normal_evening`, `rush`, `late_van`, `load_spike` or `impossible`.
- `STRATEGY`: `uncontrolled` or `equal_sharing`.
- `PRINT_OCPP_MESSAGES=1`: print every raw OCPP message.

## Tests

```
python -m pytest -v
```

The full suite takes about three minutes. It runs complete nights of every scenario with the real OCPP and Modbus connections, at a simulation speed where one night takes about 20 seconds. The scenario tests check the true site power, rebuilt from what every charger really drew, not only what the controller measured.

## Price data

Day-ahead prices will come from SMARD.de, using the loading code from my earlier project [battery-revenue-study](https://github.com/abdulrehman0306/battery-revenue-study).
