"""
What the meter and the controller share for MQTT: the topic names, the message
format, and how a client connects to the broker.

MQTT in short: programs do not talk to each other directly. Each one connects to a
broker (here Mosquitto). A sender "publishes" a message to a topic, a text like
depot/meter/building_power_kw. A receiver "subscribes" to that topic and the broker
passes every new message on to it. The sender does not know who listens.

Topics used:

    <prefix>/meter/building_power_kw   the meter publishes once per simulated minute:
                                       {"time": "2026-01-14T19:31:00+01:00", "power_kw": 22.0}
    <prefix>/controller/clock          the controller publishes its simulated time once
                                       per cycle (only when it reads the building load
                                       over MQTT), as Unix seconds; the meter follows it

The time stamp in each reading lets the controller see how old it is: a reading is
only used while it is fresh (settings.MQTT_READING_MAX_AGE_MINUTES).

The clients use paho-mqtt with its own network thread (loop_start). That thread
connects, reconnects after a lost connection, and receives messages; the asyncio
programs only read the newest value it stored.
"""

import json
import logging
from datetime import datetime, timedelta

import paho.mqtt.client as mqtt

import settings


def building_power_topic(prefix=settings.MQTT_TOPIC_PREFIX):
    """In: topic prefix. Out: the topic the meter publishes its readings to."""
    return f"{prefix}/meter/building_power_kw"


def clock_topic(prefix=settings.MQTT_TOPIC_PREFIX):
    """In: topic prefix. Out: the topic the controller publishes its simulated time to."""
    return f"{prefix}/controller/clock"


def reading_to_payload(sim_time, power_kw):
    """In: simulated time of the reading, building power [kW]. Out: the message text (JSON)."""
    return json.dumps({"time": sim_time.isoformat(), "power_kw": round(power_kw, 3)})


def payload_to_reading(payload):
    """
    In: message bytes from the building power topic.
    Out: (time of the reading, power [kW]), or None if the message is not a valid reading.
    """
    try:
        message = json.loads(payload)
        reading_time = datetime.fromisoformat(message["time"])
        power_kw = float(message["power_kw"])
    except (ValueError, KeyError, TypeError):
        return None
    if reading_time.tzinfo is None:
        return None
    return reading_time, power_kw


def fresh_power_kw(reading, now, max_age_minutes=settings.MQTT_READING_MAX_AGE_MINUTES):
    """
    The rule for trusting a reading that came over MQTT.

    In: the newest reading (time, power [kW]) or None, the controller's simulated time,
        the maximum age [simulated minutes].
    Out: the power [kW] if the reading is fresh, otherwise None.
         A reading is fresh if its time is at most max_age_minutes away from now, in
         either direction: a time stamp far in the future means the meter's clock is
         wrong, so its reading is not trusted either.
    """
    if reading is None:
        return None
    reading_time, power_kw = reading
    if abs(now - reading_time) > timedelta(minutes=max_age_minutes):
        return None
    return power_kw


class MqttConnection:
    """
    One connection to the broker, kept up by paho's network thread.

    It subscribes to the given topics every time it (re)connects: after a lost
    connection the broker may have forgotten the subscriptions, so subscribing again
    on every connect is the safe pattern. Incoming messages go to on_message(topic, payload),
    called in the network thread.
    """

    def __init__(self, host, port, name, subscriptions=(), on_message=None):
        """
        In: broker address and port, a name for the log, topics to subscribe to,
            the function to call for each incoming message.
        """
        self.host = host
        self.port = port
        self.name = name
        self.subscriptions = list(subscriptions)
        self.handle_message = on_message
        self.events = logging.getLogger(name)
        self.connected_once = False
        self.failed_connects = 0

        # An empty client id with a clean session: the broker gives each connection its
        # own id, so two runs (or tests) never throw each other off the broker.
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="", clean_session=True)
        self.client.on_connect = self.on_connect
        self.client.on_connect_fail = self.on_connect_fail
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(settings.MQTT_RECONNECT_MIN_DELAY_S, settings.MQTT_RECONNECT_MAX_DELAY_S)

    def start(self):
        """Start the network thread. It connects in the background and keeps reconnecting."""
        # keepalive: if nothing is sent for this many seconds, client and broker exchange a
        # ping; a broker that does not answer for 1.5 times as long counts as lost.
        self.client.connect_async(self.host, self.port, keepalive=10)
        self.client.loop_start()

    def stop(self):
        """Disconnect and stop the network thread."""
        self.client.disconnect()
        self.client.loop_stop()

    def is_connected(self):
        return self.client.is_connected()

    def publish(self, topic, payload, retain=False):
        """
        Publish one message with the QoS from settings.py.
        Out: True if it was handed to the connection, False if there is no connection now.
        """
        if not self.client.is_connected():
            return False
        result = self.client.publish(topic, payload, qos=settings.MQTT_QOS, retain=retain)
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    # The functions below are called by paho in its network thread.

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.events.warning("MQTT broker %s:%s refused the connection: %s", self.host, self.port, reason_code)
            return
        for topic in self.subscriptions:
            client.subscribe(topic, qos=settings.MQTT_QOS)
        if self.connected_once:
            self.events.warning("MQTT connection to %s:%s is back", self.host, self.port)
        else:
            self.events.info("MQTT connected to %s:%s", self.host, self.port)
        self.connected_once = True
        self.failed_connects = 0

    def on_connect_fail(self, client, userdata):
        # Warn at the first failure and then every 60th, not on every retry.
        if self.failed_connects % 60 == 0:
            self.events.warning("MQTT broker %s:%s not reachable, trying again", self.host, self.port)
        self.failed_connects += 1

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.events.warning("MQTT connection to %s:%s lost (%s), reconnecting", self.host, self.port,
                                reason_code)

    def on_message(self, client, userdata, message):
        if self.handle_message is not None:
            self.handle_message(message.topic, message.payload)
