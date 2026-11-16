"""
The building's own electricity use (lights, heating, IT, workshop).

This profile is SYNTHETIC: made-up values for a small delivery depot in
winter, not a measurement. Base load at night, higher in the morning when
vans are loaded, highest while the workshop works.

Only the simulated meter (modbus_meter.py) uses this file. The controller
never calls it: it reads the meter over Modbus TCP, like it would read a real one.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

# Building power [kW] for each hour of the day. Index 0 = 00:00-01:00.
BUILDING_LOAD_BY_HOUR_KW = [
    15, 15, 15, 15, 15,                       # 00-05  night base load: security lights, IT, frost heating
    30, 30,                                   # 05-07  morning: lights, heating, vans being loaded
    45, 45, 45, 45, 45, 45, 45, 45, 45,       # 07-16  workshop working: lifts, compressor, tools
    35, 35,                                   # 16-18  workshop closing, office
    22, 22, 22, 22,                           # 18-22  evening: cleaning, office lights
    15, 15,                                   # 22-24  night base load
]


@dataclass
class LoadSpike:
    """An extra load on top of the normal profile, e.g. a workshop machine switched on (synthetic)."""
    start_time: datetime
    duration_minutes: int
    extra_power_kw: float

    def is_active(self, sim_time):
        """In: simulated time. Out: True while the spike is drawing power."""
        return self.start_time <= sim_time < self.start_time + timedelta(minutes=self.duration_minutes)


def building_load_kw(sim_time, load_spikes=()):
    """
    In: simulated time (datetime), and any extra load spikes of the scenario.
    Out: the building's power at that time [kW], synthetic profile plus active spikes.
    """
    power_kw = float(BUILDING_LOAD_BY_HOUR_KW[sim_time.hour])
    for spike in load_spikes:
        if spike.is_active(sim_time):
            power_kw += spike.extra_power_kw
    return power_kw
