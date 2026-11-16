"""
The simulated clock.

The simulation runs faster than real time (see SIMULATED_SECONDS_PER_REAL_SECOND
in settings.py). The controller and the chargers are separate programs, so they
must agree on what time it is in the simulation.

How they agree: the controller owns the clock. A charger sets its own clock
from the "currentTime" field in the controller's answer to BootNotification
and to every Heartbeat. Real chargers use exactly these fields to set
their clocks from the CSMS.
"""

import time
from datetime import datetime, timedelta


class SimulationClock:
    """
    A clock that runs simulation_speed times faster than real time.

    It remembers one fixed point (a simulated time and the real time it was set),
    and counts forward from there. A class is used because the clock has to
    remember that fixed point between calls.
    """

    def __init__(self, start_time, simulation_speed):
        """In: simulated start time (datetime with time zone), speed [simulated s per real s]."""
        self.simulation_speed = simulation_speed
        self.set_time(start_time)

    def set_time(self, sim_time):
        """Set the clock to sim_time from this real moment on. Used to follow the controller's clock."""
        self.anchor_sim_time = sim_time
        # monotonic() is a real-time counter that never jumps back, even if
        # the computer's own clock is changed. Good for measuring elapsed time.
        self.anchor_real_s = time.monotonic()

    def set_time_from_iso(self, iso_text):
        """Set the clock from an ISO 8601 text, as sent in OCPP messages."""
        self.set_time(datetime.fromisoformat(iso_text))

    def now(self):
        """Out: the present simulated time (datetime)."""
        elapsed_real_s = time.monotonic() - self.anchor_real_s
        return self.anchor_sim_time + timedelta(seconds=elapsed_real_s * self.simulation_speed)

    def real_seconds(self, sim_minutes):
        """Out: how many real seconds pass during sim_minutes simulated minutes."""
        return sim_minutes * 60 / self.simulation_speed
