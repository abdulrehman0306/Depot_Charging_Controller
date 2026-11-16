"""
Conversion between power (kW) and charging current (A per phase).

The whole project plans and reports in kW. Real AC chargers are controlled
in amps per phase, so the conversion happens only at the edge: when the
controller writes a SetChargingProfile and when the charger reads it.

For a balanced three phase load with power factor 1:
    P = number of phases x phase voltage x current per phase
    3 x 230 V x 32 A = 22 080 W, which is the "22 kW" wallbox.
"""

import math

import settings


def power_kw_to_current_a(power_kw, number_of_phases=settings.CHARGER_NUMBER_OF_PHASES,
                          phase_voltage_v=settings.PHASE_VOLTAGE_V):
    """
    Turn a power limit into the current per phase a charger must not exceed.

    In: power [kW], number of phases, phase-to-neutral voltage [V].
    Out: current per phase [A], rounded DOWN to 0.1 A.
    """
    exact_current_a = power_kw * 1000 / (number_of_phases * phase_voltage_v)

    # OCPP allows one decimal place. We round down, never up, so the charger
    # is never allowed even a little more power than the plan gave it.
    # The small 1e-9 stops values like 15.9999999 (from floating point maths)
    # from being rounded down to 15.9 when they really are 16.0.
    return math.floor(exact_current_a * 10 + 1e-9) / 10


def charging_current_limit_a(power_kw):
    """
    The current limit to send to a charger for a planned power, following the charging standard.

    In: planned power for one van [kW].
    Out: current per phase [A]: either 0 (van paused) or between the 6 A minimum and the 32 A maximum.
    """
    current_a = power_kw_to_current_a(power_kw)

    # IEC 61851-1: the charger cannot signal less than 6 A to the car. A plan
    # below 6 A cannot be carried out, so the van pauses (0 A). Rounding down
    # to 0 instead of up to 6 A keeps the site under its limit. The strategies
    # are responsible for planning 0 or at least 6 A, so this rarely triggers.
    if current_a < settings.CHARGER_MIN_CURRENT_A:
        return 0.0
    return min(current_a, settings.CHARGER_MAX_CURRENT_A)


def current_a_to_power_kw(current_a, number_of_phases=settings.CHARGER_NUMBER_OF_PHASES,
                          phase_voltage_v=settings.PHASE_VOLTAGE_V):
    """
    Turn a current per phase back into power.

    In: current per phase [A], number of phases, phase-to-neutral voltage [V].
    Out: power [kW].
    """
    return number_of_phases * phase_voltage_v * current_a / 1000
