"""
All settings for the depot charging controller, in one place.

Change a value here and every part of the project uses the new value.
Units are written in the names: _kw = kilowatt, _kwh = kilowatt hour,
_s = real seconds, _minutes = simulated minutes.
"""

import os
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Chargers
# ---------------------------------------------------------------------------
NUMBER_OF_CHARGERS = 10

# A standard AC wallbox: three phases x 230 V x 32 A = 22.08 kW, rounded to 22 kW.
# Planning uses kW. Only the OCPP messages use amps (see src/electrical.py).
CHARGER_MAX_POWER_KW = 22.0
CHARGER_MAX_CURRENT_A = 32
CHARGER_NUMBER_OF_PHASES = 3

# IEC 61851-1: an AC charger cannot offer a car less than 6 A per phase.
# A van gets either 0 A (paused) or at least 6 A (about 4.1 kW on three phases).
CHARGER_MIN_CURRENT_A = 6

# Phase-to-neutral voltage of the German low voltage grid (400 V between phases).
PHASE_VOLTAGE_V = 230

# ---------------------------------------------------------------------------
# Site grid connection
# ---------------------------------------------------------------------------
# Total power the depot may draw from the grid (building + chargers together).
SITE_LIMIT_KW = 150.0

# The safety layer starts reducing charging power when the site reaches
# this share of the limit, so there is room before the real limit is hit.
SAFETY_MARGIN_FRACTION = 0.95

# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
# Same length as one day-ahead price slot, so each plan step has one price.
PLANNING_INTERVAL_MINUTES = 15

# ---------------------------------------------------------------------------
# Vans
# ---------------------------------------------------------------------------
VAN_BATTERY_USABLE_KWH = 75.0

# Share of the energy from the grid that actually ends up in the battery.
# The rest is lost as heat in the on-board charger and the battery.
CHARGING_EFFICIENCY = 0.92

# Every van must have at least this state of charge when it leaves.
TARGET_STATE_OF_CHARGE_PERCENT = 90.0

# ---------------------------------------------------------------------------
# Synthetic van schedule (made-up ranges, not measured data)
# ---------------------------------------------------------------------------
# The same seed always gives the same vans, so every run can be repeated.
RANDOM_SEED = 42

ARRIVAL_EARLIEST = "16:00"
ARRIVAL_LATEST = "20:00"
DEPARTURE_EARLIEST = "05:00"
DEPARTURE_LATEST = "08:00"
PARKING_HOURS_MIN = 9
PARKING_HOURS_MAX = 14

# State of charge when a van comes back from its route.
ARRIVAL_STATE_OF_CHARGE_MIN_PERCENT = 20.0
ARRIVAL_STATE_OF_CHARGE_MAX_PERCENT = 50.0

# ---------------------------------------------------------------------------
# Simulation clock
# ---------------------------------------------------------------------------
# 60 means one simulated minute passes every real second,
# so a full simulated day takes 24 real minutes.
# It can be changed without editing this file, for example for a faster demo:
#     set SIM_SPEED=600        (Windows)
#     export SIM_SPEED=600     (Linux, Docker)
# The controller and the chargers must use the same speed.
SIMULATED_SECONDS_PER_REAL_SECOND = int(os.environ.get("SIM_SPEED", "60"))

# Simulated time when a run starts: just before the first van can arrive.
# German winter time (CET, UTC+1). Summer time is not handled yet.
SIMULATION_START_TIME = datetime(2025, 1, 15, 15, 30, tzinfo=timezone(timedelta(hours=1)))

# ---------------------------------------------------------------------------
# OCPP connection between chargers and the controller (CSMS)
# ---------------------------------------------------------------------------
CSMS_HOST = "localhost"
CSMS_PORT = 9000

# Name both sides agree on when the WebSocket opens, so they know
# they are speaking OCPP 2.0.1 and not, for example, OCPP 1.6.
OCPP_SUBPROTOCOL = "ocpp2.0.1"

# How often a charger says "I am still alive", in real seconds.
# Real chargers often use 300 s or more; it is short here so you see it in a demo.
HEARTBEAT_INTERVAL_S = 10

# ---------------------------------------------------------------------------
# Building energy meter (Modbus TCP)
# ---------------------------------------------------------------------------
METER_HOST = "localhost"
# The standard Modbus TCP port is 502, but ports below 1024 need admin rights
# on Linux, so the simulated meter uses 5020.
METER_PORT = 5020
# Modbus "unit id" (also called slave id or device id) of the meter.
METER_DEVICE_ID = 1

# If the meter cannot be read and there is no earlier reading, assume the
# building draws this much. A high guess is the safe side: it leaves less
# room for charging, never more.
BUILDING_LOAD_IF_METER_UNKNOWN_KW = 60.0

# How often a charging charger reports its meter, in simulated minutes.
METER_VALUE_INTERVAL_MINUTES = 5

# Print every raw OCPP message. Useful with one charger; with ten it is a flood,
# so it is off unless you set PRINT_OCPP_MESSAGES=1.
PRINT_OCPP_MESSAGES = os.environ.get("PRINT_OCPP_MESSAGES", "0") == "1"
