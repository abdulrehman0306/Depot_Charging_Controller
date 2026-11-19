"""
The controller's MQTT client for the building load, used instead of the Modbus client
when settings.BUILDING_LOAD_SOURCE is "mqtt".

With Modbus the controller asks the meter ("polling"). With MQTT it subscribes once and
the broker passes on every reading the meter publishes. So the controller cannot tell
from a failed request that something is wrong: if the broker, the network or the meter
stops, the readings simply stop coming. That is why every reading carries a time stamp
and is only used while it is fresh. Without a fresh reading the controller uses the
same safe high guess as for a meter it cannot read.
"""

import logging
import threading

import settings
from src.mqtt_link import MqttConnection, building_power_topic, clock_topic, fresh_power_kw, payload_to_reading


class MqttMeterReader:
    """Keeps the newest building load reading from MQTT, and sends the controller's clock."""

    def __init__(self, host=settings.MQTT_HOST, port=settings.MQTT_PORT, topic_prefix=settings.MQTT_TOPIC_PREFIX,
                 max_age_minutes=settings.MQTT_READING_MAX_AGE_MINUTES):
        """In: broker address and port, topic prefix, the maximum age of a usable reading [simulated minutes]."""
        self.power_topic = building_power_topic(topic_prefix)
        self.clock_topic = clock_topic(topic_prefix)
        self.max_age_minutes = max_age_minutes
        self.events = logging.getLogger("CSMS")
        # The newest reading (time, power [kW]), written by the network thread.
        self.lock = threading.Lock()
        self.newest_reading = None
        self.readings_received = 0
        # Counts for the report and the tests: cycles with a fresh reading, and cycles
        # that had to use the safe guess.
        self.fresh_cycles = 0
        self.safe_guess_cycles = 0
        self.using_safe_guess = False
        self.connection = MqttConnection(host, port, "CSMS", subscriptions=[self.power_topic],
                                         on_message=self.on_message)

    def start(self):
        self.connection.start()

    def stop(self):
        self.connection.stop()

    def on_message(self, topic, payload):
        """Network thread: a new reading from the meter. Readings that are not valid are ignored."""
        reading = payload_to_reading(payload)
        if reading is None:
            return
        # MQTT delivers the messages of one sender on one topic in the order they were
        # sent, so the last one received is the newest. Its time stamp decides whether it
        # is still fresh enough to use.
        with self.lock:
            self.newest_reading = reading
            self.readings_received += 1

    def send_clock(self, sim_time):
        """Publish the controller's simulated time, so the meter's clock follows it."""
        self.connection.publish(self.clock_topic, str(int(sim_time.timestamp())))

    async def building_load_kw(self, sim_time):
        """
        The building load for this control cycle.

        In: the controller's simulated time.
        Out: the newest reading [kW] if it is fresh, otherwise the safe high guess
             (settings.BUILDING_LOAD_IF_METER_UNKNOWN_KW).
        """
        self.send_clock(sim_time)
        with self.lock:
            reading = self.newest_reading
        power_kw = fresh_power_kw(reading, sim_time, self.max_age_minutes)

        if power_kw is None:
            self.safe_guess_cycles += 1
            if not self.using_safe_guess:
                reason = "no reading yet" if reading is None else f"newest reading is from {reading[0]:%H:%M:%S}"
                self.events.warning("%s no fresh building load over MQTT (%s), using the safe guess of %.0f kW",
                                    sim_time.strftime("%H:%M:%S"), reason, settings.BUILDING_LOAD_IF_METER_UNKNOWN_KW)
                self.using_safe_guess = True
            return settings.BUILDING_LOAD_IF_METER_UNKNOWN_KW

        self.fresh_cycles += 1
        if self.using_safe_guess:
            self.events.warning("%s fresh building load over MQTT again: %.1f kW", sim_time.strftime("%H:%M:%S"),
                                power_kw)
            self.using_safe_guess = False
        return power_kw
