"""
All settings for the depot charging controller, in one place.

Change a value here and every part of the project uses the new value.
Units are written in the names: _kw = kilowatt, _kwh = kilowatt hour,
_s = real seconds, _minutes = simulated minutes.
"""

import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Read the secrets (InfluxDB token, passwords) from the .env file next to this one,
# if it exists. Values already set in the environment (for example by Docker) win.
# .env is in .gitignore; .env.example shows which values it needs.
load_dotenv(Path(__file__).resolve().parent / ".env")

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

# Margin kept free below the limit. The controller only sees the building
# load once per meter reading (one simulated minute). If the building load
# jumps right after a reading, the controller cannot react until the next
# one. The margin must cover the biggest normal jump in that time, so the
# site stays under the limit even before the controller has seen the jump.
# In the synthetic building profile the biggest step is +15 kW (at 05:00
# and 07:00), so the margin is 15 kW: charging is planned up to 135 kW
# total site power. (5 % of the limit, 7.5 kW, would not cover that step.)
# A bigger, abnormal jump (like the 40 kW load spike scenario) is not covered:
# the safety layer then cuts charging at the next meter reading.
SAFETY_MARGIN_KW = 15.0

# ---------------------------------------------------------------------------
# Planning and control
# ---------------------------------------------------------------------------
# Same length as one day-ahead price slot, so each plan step has one price.
PLANNING_INTERVAL_MINUTES = 15

# When a plan lowers some chargers and raises others, the controller waits for
# the lowered chargers to report their new power before raising the others.
# If they have not confirmed within this time, the increases are skipped
# until the next plan (the safe choice).
REDUCTION_CONFIRM_TIMEOUT_MINUTES = 2

# Which charging strategy the controller uses: "uncontrolled", "equal_sharing" or "smart".
# Change without editing this file:  set STRATEGY=uncontrolled
STRATEGY = os.environ.get("STRATEGY", "smart")

# Smart controller: every kWh a van is missing at departure (below the 90 % target)
# counts as this many euros in the optimisation. It is far higher than any
# electricity price, so the optimiser always charges every van it can first,
# and only then looks for the cheapest quarter hours.
SHORTFALL_PENALTY_EUR_PER_KWH = 100.0

# Smart controller: the plan aims this much above the 90 % target. Small real losses
# would otherwise leave a van just under 90 %: limits are rounded down to 0.1 A,
# a plan reaches the charger a moment after it was made, and a planned power below
# 4.14 kW becomes 0 because of the 6 A rule. 2 % of 75 kWh is 1.5 kWh.
PLANNING_RESERVE_PERCENT = 2.0

# Smart controller: the plan wants every van finished this long before it leaves,
# so a delay or a short disturbance in the last minutes does not leave it short.
# Energy still missing at that point costs FINISH_LATE_PENALTY_EUR_PER_KWH in the
# optimisation: about ten times an electricity price, so finishing early wins
# whenever it is possible, but far less than a real shortfall, so the last minutes
# are still used when they are needed.
PLANNING_FINISH_EARLY_MINUTES = 30
FINISH_LATE_PENALTY_EUR_PER_KWH = 1.0

# Smart controller: give up on the optimiser after this many real seconds and
# use the simple fallback rule for this plan instead.
SOLVER_TIME_LIMIT_S = 10

# ---------------------------------------------------------------------------
# Day-ahead prices (real data, SMARD.de)
# ---------------------------------------------------------------------------
# SMARD chart data API: series 4169 = day-ahead price for the DE-LU bidding
# zone, quarter hours, EUR/MWh. Same source and loading code as my
# battery-revenue-study project.
SMARD_PRICE_SERIES_ID = "4169"
SMARD_PRICE_REGION = "DE-LU"
PRICE_DATA_FOLDER = Path(__file__).resolve().parent / "data" / "prices"

# Which test scenario the meter and the chargers play (see src/scenarios.py).
# The controller only uses it to label its report, never to make decisions.
# Change without editing this file:  set SCENARIO=load_spike
SCENARIO = os.environ.get("SCENARIO", "normal_evening")

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

# German local time, with summer and winter time. Nights with a clock change
# (last Sunday of March and of October) are not used: src/scenarios.py checks it.
TIMEZONE = ZoneInfo("Europe/Berlin")

# The nights the scenarios play, chosen from the real SMARD price data:
# - a normal winter weekday night (Wednesday to Thursday),
# - the night in the data with the most negative prices between 20:00 and 06:00
#   (Saturday to Sunday, 35 negative quarter hours, lowest -5.09 EUR/MWh).
NORMAL_NIGHT_DATE = date(2026, 1, 14)
NEGATIVE_PRICE_NIGHT_DATE = date(2025, 10, 4)

# The results (src/run_week.py) play every scenario on 7 nights in a row, each night with
# its own random seed (RANDOM_SEED + night number) and the real prices of that night.
# The negative price scenario uses the week around its night, all other scenarios a
# January week. Both weeks have no change of summer or winter time.
WEEK_FIRST_NIGHT = date(2026, 1, 12)
NEGATIVE_PRICE_WEEK_FIRST_NIGHT = date(2025, 9, 29)
NIGHTS_PER_WEEK = 7

# A run starts at this clock time on the night's date: just before the first van can arrive.
SIMULATION_START_CLOCK = "15:30"

# Start of the normal night. Used where no scenario is given (for example in small tests).
SIMULATION_START_TIME = datetime(NORMAL_NIGHT_DATE.year, NORMAL_NIGHT_DATE.month, NORMAL_NIGHT_DATE.day,
                                 15, 30, tzinfo=TIMEZONE)

# ---------------------------------------------------------------------------
# OCPP connection between chargers and the controller (CSMS)
# ---------------------------------------------------------------------------
# The controller listens on CSMS_HOST, the chargers connect to it. On one computer
# both are "localhost". In Docker each program is its own container: the controller
# listens on all addresses (0.0.0.0) and the chargers connect to the name "controller"
# (set in docker-compose.yml).
CSMS_HOST = os.environ.get("CSMS_HOST", "localhost")
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
# The meter listens on METER_HOST, the controller connects to it (in Docker: 0.0.0.0
# for the meter, "meter" for the controller; see docker-compose.yml).
METER_HOST = os.environ.get("METER_HOST", "localhost")
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

# ---------------------------------------------------------------------------
# Storage (InfluxDB) and the REST API
# ---------------------------------------------------------------------------
# Connection to InfluxDB. The token is a secret: it comes from .env, never from this file.
# Without a token the controller runs without storing measurements.
INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_ORG = os.environ.get("INFLUXDB_ORG", "depot")
INFLUXDB_BUCKET = os.environ.get("INFLUXDB_BUCKET", "depot")
INFLUXDB_TOKEN = os.environ.get("INFLUXDB_TOKEN", "")

# The REST API (FastAPI) is served by the controller program on this address.
API_HOST = os.environ.get("API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("API_PORT", "8000"))

# Allowed range when the site limit is changed through the API [kW]. The lower end
# must stay above the safety margin, or there would be nothing left to plan with.
SITE_LIMIT_MIN_KW = 20.0
SITE_LIMIT_MAX_KW = 400.0
