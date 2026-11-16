"""
A simple electric van battery model.

Simplification: the van takes any power the charger offers until the battery
is full. Real batteries take less power near full (the charging curve); that
is not modelled.
"""

from dataclasses import dataclass
from datetime import datetime

import settings


@dataclass
class Van:
    """One van's visit to the depot: when it arrives, when it must leave, and its state of charge on arrival."""
    van_id: str
    arrival_time: datetime
    departure_time: datetime
    arrival_state_of_charge_percent: float


def grid_energy_to_reach_kwh(state_of_charge_percent, target_state_of_charge_percent,
                             battery_kwh=settings.VAN_BATTERY_USABLE_KWH,
                             charging_efficiency=settings.CHARGING_EFFICIENCY):
    """
    Energy that must be drawn from the grid to lift the battery to the target.

    In: present and target state of charge [%], usable battery size [kWh], charging efficiency [0..1].
    Out: grid energy [kWh], 0 if the target is already reached.
    """
    missing_battery_energy_kwh = max(0.0, target_state_of_charge_percent - state_of_charge_percent) / 100 * battery_kwh

    # Some energy is lost as heat on the way into the battery,
    # so the grid has to deliver more than the battery stores.
    return missing_battery_energy_kwh / charging_efficiency


def charge_van(state_of_charge_percent, charge_power_kw, duration_hours,
               battery_kwh=settings.VAN_BATTERY_USABLE_KWH,
               charging_efficiency=settings.CHARGING_EFFICIENCY):
    """
    Charge the van at a constant power for a while, stopping when the battery is full.

    In: state of charge [%], charging power from the grid [kW], duration [h],
        usable battery size [kWh], charging efficiency [0..1].
    Out: (new state of charge [%], energy drawn from the grid [kWh]).
    """
    offered_grid_energy_kwh = charge_power_kw * duration_hours
    grid_energy_to_full_kwh = grid_energy_to_reach_kwh(state_of_charge_percent, 100.0, battery_kwh, charging_efficiency)

    # A full battery stops taking energy, even if the charger offers more.
    grid_energy_kwh = min(offered_grid_energy_kwh, grid_energy_to_full_kwh)

    stored_energy_kwh = grid_energy_kwh * charging_efficiency
    new_state_of_charge_percent = state_of_charge_percent + stored_energy_kwh / battery_kwh * 100
    return new_state_of_charge_percent, grid_energy_kwh
