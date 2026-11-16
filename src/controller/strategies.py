"""
Charging strategies: given what the controller knows about every charger,
decide each charger's power limit.

Every strategy is a function with the same shape:
    In:  dict of charger id -> ChargerConnection (see central_system.py)
    Out: dict of charger id -> power limit [kW] for chargers that should get a new limit.
         Chargers left out of the dict are not sent anything.

Stage 2 has only the uncontrolled strategy. Equal sharing and the smart
controller are added in Stages 4 and 5.
"""


def uncontrolled(connected_chargers):
    """
    No control at all: send no limits, so every charger runs at full power.
    This shows what happens at the depot without any charging software.
    """
    return {}
