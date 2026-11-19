"""
The meter's MQTT side: like an IoT energy meter, it publishes every reading to the
broker, whoever listens. Its Modbus registers stay as they are, so the controller can
use either one.

It also listens to the controller's clock topic, so the meter's clock can follow the
controller when the controller reads over MQTT and therefore writes no clock registers.
"""

import time

import settings
from src.mqtt_link import MqttConnection, building_power_topic, clock_topic, reading_to_payload


class MeterMqttPublisher:
    """Publishes the meter's readings, and remembers the newest clock time the controller sent."""

    def __init__(self, host=settings.MQTT_HOST, port=settings.MQTT_PORT, topic_prefix=settings.MQTT_TOPIC_PREFIX):
        """In: broker address and port, topic prefix."""
        self.power_topic = building_power_topic(topic_prefix)
        self.clock_topic = clock_topic(topic_prefix)
        # The newest clock message: (controller time in Unix seconds, real time it arrived).
        # Written by the network thread, read by the meter; assigning one tuple is atomic in Python.
        self.newest_clock = None
        self.published = 0
        self.connection = MqttConnection(host, port, "METER", subscriptions=[self.clock_topic],
                                         on_message=self.on_message)

    def start(self):
        self.connection.start()

    def stop(self):
        self.connection.stop()

    def on_message(self, topic, payload):
        """Network thread: a new clock time from the controller."""
        try:
            self.newest_clock = (int(payload), time.monotonic())
        except ValueError:
            pass

    @property
    def newest_clock_unix_s(self):
        """The controller time of the newest clock message [Unix seconds], or None."""
        return None if self.newest_clock is None else self.newest_clock[0]

    def publish_reading(self, sim_time, power_kw):
        """
        Publish one reading. Without a connection the reading is dropped, like a real
        meter does: an old reading delivered later would be useless anyway.

        In: simulated time of the reading, building power [kW].
        Out: True if it was sent.
        """
        sent = self.connection.publish(self.power_topic, reading_to_payload(sim_time, power_kw))
        if sent:
            self.published += 1
        return sent
