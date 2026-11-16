"""
All settings for the depot charging controller, in one place.

Change a value here and every part of the project uses the new value.
Units are written in the names: _kw = kilowatt, _kwh = kilowatt hour,
_s = real seconds, _minutes = simulated minutes.
"""

from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Chargers
# ---------------------------------------------------------------------------
NUMBER_OF_CHARGERS = 10

# A standard AC wallbox: three phases x 230 V x 32 A = 22.08 kW, rounded to 22 kW.
CHARGER_MAX_POWER_KW = 22.0
CHARGER_MAX_CURRENT_A = 32
CHARGER_NUMBER_OF_PHASES = 3

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
# Simulation clock
# ---------------------------------------------------------------------------
# 60 means one simulated minute passes every real second,
# so a full simulated day takes 24 real minutes.
SIMULATED_SECONDS_PER_REAL_SECOND = 60

# Simulated time when a run starts. German winter time (CET, UTC+1).
# Summer time is not handled yet.
SIMULATION_START_TIME = datetime(2025, 1, 15, 17, 0, tzinfo=timezone(timedelta(hours=1)))

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

# How often a charging charger reports its meter, in simulated minutes.
METER_VALUE_INTERVAL_MINUTES = 5
