"""
Charging strategies: how the power available for charging is split between the vans.

1. Uncontrolled: no limits at all. Every van charges at full power from plug-in.
   This shows what happens at the depot without any charging software.
   It has no function here: the controller simply sends nothing.

2. Equal sharing: the available power is split equally between all vans that
   still want power, never more than 22 kW each. Because of the 6 A rule a van
   gets either 0 or at least 4.14 kW. If there is not enough for everyone, the
   vans at the front of the queue charge and the rest pause; the controller
   rotates the queue every 15 minutes so no van waits all night.

The smart controller (Stage 5) will be added here as well.
"""

import math

import settings
from src.electrical import current_a_to_power_kw, power_kw_to_current_a

STRATEGY_NAMES = ["uncontrolled", "equal_sharing", "smart"]

# Smallest power a van can charge with: 6 A on three phases at 230 V = 4.14 kW.
MIN_CHARGING_POWER_KW = current_a_to_power_kw(settings.CHARGER_MIN_CURRENT_A)

# Largest power a charger can really be given: 22 kW rounds down to 31.8 A = 21.94 kW.
MAX_CHARGING_POWER_KW = current_a_to_power_kw(power_kw_to_current_a(settings.CHARGER_MAX_POWER_KW))


def equal_sharing(charger_ids_in_queue_order, available_kw):
    """
    Split the available power equally, respecting the 6 A minimum and the 22 kW maximum.

    In: ids of the chargers whose vans want power, in queue order (front first),
        power available for charging [kW].
    Out: dict charger id -> power limit [kW]. Paused vans get 0.
    """
    number_of_vans = len(charger_ids_in_queue_order)
    if number_of_vans == 0:
        return {}

    # How many vans can get at least the minimum power? The small 1e-9 stops
    # floating point maths from turning an exact 10.0 into 9.
    number_charging = min(number_of_vans, math.floor(max(0.0, available_kw) / MIN_CHARGING_POWER_KW + 1e-9))

    limits_kw = {}
    if number_charging == 0:
        for charger_id in charger_ids_in_queue_order:
            limits_kw[charger_id] = 0.0
        return limits_kw

    # The front of the queue charges with an equal share, the rest pause.
    share_kw = min(available_kw / number_charging, settings.CHARGER_MAX_POWER_KW)
    for position, charger_id in enumerate(charger_ids_in_queue_order):
        if position < number_charging:
            limits_kw[charger_id] = share_kw
        else:
            limits_kw[charger_id] = 0.0
    return limits_kw
