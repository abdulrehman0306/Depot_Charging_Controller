"""
The building's own electricity use (lights, heating, IT, workshop).

This profile is SYNTHETIC: made-up values for a small delivery depot in
winter, not a measurement. Base load at night, higher in the morning when
vans are loaded, highest while the workshop works.

In Stage 2 the controller reads this function directly. From Stage 3 on it
is served by a simulated energy meter over Modbus TCP, like a real meter.
"""

# Building power [kW] for each hour of the day. Index 0 = 00:00-01:00.
BUILDING_LOAD_BY_HOUR_KW = [
    15, 15, 15, 15, 15,                       # 00-05  night base load: security lights, IT, frost heating
    30, 30,                                   # 05-07  morning: lights, heating, vans being loaded
    45, 45, 45, 45, 45, 45, 45, 45, 45,       # 07-16  workshop working: lifts, compressor, tools
    35, 35,                                   # 16-18  workshop closing, office
    22, 22, 22, 22,                           # 18-22  evening: cleaning, office lights
    15, 15,                                   # 22-24  night base load
]


def building_load_kw(sim_time):
    """
    In: simulated time (datetime).
    Out: the building's power at that time [kW], synthetic profile.
    """
    return float(BUILDING_LOAD_BY_HOUR_KW[sim_time.hour])
