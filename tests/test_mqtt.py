"""
The building load over MQTT: the message format, the freshness rule, the meter
publishing to a real broker, the controller reading from it, and what happens when
the controller loses its connection to the broker.

The tests with a broker need Mosquitto at settings.MQTT_HOST:MQTT_PORT
(docker compose up -d mqtt); see the mqtt_topic_prefix fixture in conftest.py.
"""

import asyncio
import socket
from datetime import timedelta

import pytest

import settings
from src.controller.mqtt_meter_reader import MqttMeterReader
from src.meter.modbus_meter import SimulatedMeter, start_meter_server
from src.meter.mqtt_publisher import MeterMqttPublisher
from src.mqtt_link import fresh_power_kw, payload_to_reading, reading_to_payload
from src.results import minutes_over_limit, over_limit_episodes, van_results
from src.scenarios import get_scenario
from src.sim_clock import SimulationClock
from tests.conftest import run_night

T0 = settings.SIMULATION_START_TIME
SAFE_GUESS_KW = settings.BUILDING_LOAD_IF_METER_UNKNOWN_KW


async def wait_for(condition, timeout_s=10):
    """Wait in real time until condition() is true. Fails the test if it does not happen."""
    for _ in range(int(timeout_s / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"waited {timeout_s} s in vain for {condition.__doc__ or 'a condition'}")


# ---------------------------------------------------------------------------
# The message format and the freshness rule (no broker needed)
# ---------------------------------------------------------------------------

def test_a_reading_survives_the_trip_through_a_message():
    assert payload_to_reading(reading_to_payload(T0, 22.0).encode()) == (T0, 22.0)


@pytest.mark.parametrize("payload", [b"22.0", b"not json", b'{"power_kw": 22.0}',
                                     b'{"time": "2026-01-14T15:30:00+01:00", "power_kw": "lots"}',
                                     b'{"time": "2026-01-14T15:30:00", "power_kw": 22.0}'])
def test_a_message_that_is_not_a_valid_reading_is_ignored(payload):
    # The last one has no time zone, so its age cannot be known.
    assert payload_to_reading(payload) is None


def test_a_reading_is_used_only_while_it_is_fresh():
    reading = (T0, 22.0)
    assert fresh_power_kw(None, T0) is None
    assert fresh_power_kw(reading, T0) == 22.0
    assert fresh_power_kw(reading, T0 + timedelta(minutes=settings.MQTT_READING_MAX_AGE_MINUTES)) == 22.0
    assert fresh_power_kw(reading, T0 + timedelta(minutes=settings.MQTT_READING_MAX_AGE_MINUTES, seconds=1)) is None
    # A time stamp far ahead of the controller's clock: the meter's clock is wrong.
    assert fresh_power_kw(reading, T0 - timedelta(minutes=3)) is None


# ---------------------------------------------------------------------------
# With a real broker
# ---------------------------------------------------------------------------

@pytest.fixture
async def publisher_and_reader(mqtt_topic_prefix):
    """A connected meter publisher and controller reader on this test's own topics."""
    publisher = MeterMqttPublisher(settings.MQTT_HOST, settings.MQTT_PORT, mqtt_topic_prefix)
    reader = MqttMeterReader(settings.MQTT_HOST, settings.MQTT_PORT, mqtt_topic_prefix)
    publisher.start()
    reader.start()
    await wait_for(lambda: publisher.connection.is_connected() and reader.connection.is_connected())
    # The broker confirms a subscription a moment after the connection; wait for it to settle.
    await asyncio.sleep(0.2)
    yield publisher, reader
    reader.stop()
    publisher.stop()


async def test_a_meter_reading_reaches_the_controller_over_mqtt(publisher_and_reader):
    publisher, reader = publisher_and_reader
    assert publisher.publish_reading(T0, 22.0)
    await wait_for(lambda: reader.readings_received == 1)
    assert await reader.building_load_kw(T0 + timedelta(seconds=30)) == 22.0
    assert (reader.fresh_cycles, reader.safe_guess_cycles) == (1, 0)


async def test_without_a_fresh_reading_the_controller_uses_the_safe_guess(publisher_and_reader):
    publisher, reader = publisher_and_reader
    # Nothing received yet.
    assert await reader.building_load_kw(T0) == SAFE_GUESS_KW

    publisher.publish_reading(T0, 22.0)
    await wait_for(lambda: reader.readings_received == 1)
    assert await reader.building_load_kw(T0 + timedelta(minutes=1)) == 22.0
    # No new reading for 3 minutes: the old one is too old to trust.
    assert await reader.building_load_kw(T0 + timedelta(minutes=3)) == SAFE_GUESS_KW

    # A new reading comes in: it is used again.
    publisher.publish_reading(T0 + timedelta(minutes=3), 35.0)
    await wait_for(lambda: reader.readings_received == 2)
    assert await reader.building_load_kw(T0 + timedelta(minutes=3, seconds=20)) == 35.0
    assert (reader.fresh_cycles, reader.safe_guess_cycles) == (2, 2)


async def test_without_a_broker_the_controller_uses_the_safe_guess():
    # A port where nothing listens: take a free port and close it again.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    reader = MqttMeterReader("127.0.0.1", closed_port, "no-broker")
    reader.start()
    try:
        await asyncio.sleep(0.3)
        assert not reader.connection.is_connected()
        assert await reader.building_load_kw(T0) == SAFE_GUESS_KW
        assert await reader.building_load_kw(T0 + timedelta(minutes=5)) == SAFE_GUESS_KW
    finally:
        reader.stop()


class CuttableLink:
    """
    A small TCP relay between a client and the broker, standing in for the network.
    cut() drops every connection and refuses new ones; restore() lets them through again.
    """

    def __init__(self, target_host, target_port):
        self.target = (target_host, target_port)
        self.server = None
        self.port = None
        self.writers = []

    async def open(self):
        self.server = await asyncio.start_server(self.relay, "127.0.0.1", self.port or 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def relay(self, client_reader, client_writer):
        broker_reader, broker_writer = await asyncio.open_connection(*self.target)
        self.writers += [client_writer, broker_writer]

        async def copy(source, destination):
            try:
                while data := await source.read(4096):
                    destination.write(data)
                    await destination.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                destination.close()

        await asyncio.gather(copy(client_reader, broker_writer), copy(broker_reader, client_writer))

    async def cut(self):
        # Drop the open connections first: wait_closed() waits until they are all gone.
        self.server.close()
        for writer in self.writers:
            writer.close()
        self.writers = []
        await self.server.wait_closed()

    async def restore(self):
        await self.open()


async def test_a_lost_mqtt_connection_gives_the_safe_guess_until_it_is_back(mqtt_topic_prefix):
    # The meter publishes straight to the broker; the controller is connected through the link.
    link = CuttableLink(settings.MQTT_HOST, settings.MQTT_PORT)
    await link.open()
    publisher = MeterMqttPublisher(settings.MQTT_HOST, settings.MQTT_PORT, mqtt_topic_prefix)
    reader = MqttMeterReader("127.0.0.1", link.port, mqtt_topic_prefix)
    publisher.start()
    reader.start()
    try:
        await wait_for(lambda: publisher.connection.is_connected() and reader.connection.is_connected())
        await asyncio.sleep(0.2)
        publisher.publish_reading(T0, 22.0)
        await wait_for(lambda: reader.readings_received == 1)
        assert await reader.building_load_kw(T0) == 22.0

        # The network between controller and broker fails. The meter goes on publishing,
        # but nothing reaches the controller.
        await link.cut()
        await wait_for(lambda: not reader.connection.is_connected())
        publisher.publish_reading(T0 + timedelta(minutes=1), 22.0)
        publisher.publish_reading(T0 + timedelta(minutes=2), 22.0)
        await asyncio.sleep(0.3)
        assert reader.readings_received == 1
        # The newest reading it has is from T0: 3 minutes later that is too old.
        assert await reader.building_load_kw(T0 + timedelta(minutes=3)) == SAFE_GUESS_KW
        assert reader.using_safe_guess

        # The network is back: the client reconnects by itself, subscribes again,
        # and the next reading is used.
        await link.restore()
        await wait_for(lambda: reader.connection.is_connected())
        await asyncio.sleep(0.2)
        publisher.publish_reading(T0 + timedelta(minutes=4), 30.0)
        await wait_for(lambda: reader.readings_received == 2)
        assert await reader.building_load_kw(T0 + timedelta(minutes=4)) == 30.0
        assert not reader.using_safe_guess
    finally:
        reader.stop()
        publisher.stop()
        await link.cut()


async def test_the_meter_follows_the_controller_clock_over_mqtt(publisher_and_reader):
    publisher, reader = publisher_and_reader
    server, _ = await start_meter_server("127.0.0.1", 0)
    try:
        # The meter's own clock starts somewhere else; slow speed, so it barely moves during the test.
        meter = SimulatedMeter(server, SimulationClock(T0 - timedelta(hours=3), 60), mqtt_publisher=publisher)
        controller_time = T0 + timedelta(hours=4)  # 19:30, building load 22 kW
        reader.send_clock(controller_time)
        await wait_for(lambda: publisher.newest_clock_unix_s == int(controller_time.timestamp()))

        await meter.update_registers()
        assert abs(meter.clock.now() - controller_time) < timedelta(minutes=1)
        # Its reading carries the controller's time, so the controller accepts it.
        await wait_for(lambda: reader.readings_received == 1)
        assert await reader.building_load_kw(controller_time) == 22.0

        # A clock message that is only a little behind the meter's clock (it arrived late)
        # does not move the meter's clock back.
        meter_time_before = meter.clock.now()
        late_time = controller_time - timedelta(seconds=10)
        reader.send_clock(late_time)
        await wait_for(lambda: publisher.newest_clock_unix_s == int(late_time.timestamp()))
        await meter.update_registers()
        assert meter.clock.now() >= meter_time_before
    finally:
        await server.shutdown()


# ---------------------------------------------------------------------------
# A whole night with the building load over MQTT
# ---------------------------------------------------------------------------

# A message through the broker takes a few real milliseconds, more while the whole night
# runs in this one program. At the usual test speed of 3000x a simulated minute is only
# 20 ms, so those milliseconds alone would make readings look a minute old. At 1000x a
# simulated minute is 60 ms, and the delay is a few simulated seconds.
MQTT_NIGHT_SIMULATION_SPEED = 1000


@pytest.mark.night
async def test_a_load_spike_night_over_mqtt_keeps_the_limit_like_modbus(mqtt_topic_prefix):
    scenario = get_scenario("load_spike")
    spike = scenario["load_spikes"][0]
    night = await run_night(scenario["vans"], scenario["load_spikes"], "equal_sharing",
                            simulation_speed=MQTT_NIGHT_SIMULATION_SPEED,
                            building_load_source="mqtt", mqtt_topic_prefix=mqtt_topic_prefix)

    # Over the limit only during the spike, as with Modbus, and the safety layer acted.
    for start, end in over_limit_episodes(night["true_site_log"], settings.SITE_LIMIT_KW):
        assert spike.start_time <= start < spike.start_time + timedelta(minutes=spike.duration_minutes)
    assert minutes_over_limit(night["true_site_log"], settings.SITE_LIMIT_KW) > 0
    assert len(night["controller"].safety_actions) > 0

    # Every van reached its target.
    visits = [visit for charger in night["chargers"] for visit in charger.completed_visits]
    assert all(result["reached_target"] for result in van_results(scenario["vans"], visits))

    # The building load really came over MQTT: a fresh reading in almost every cycle.
    # The rest used the safe guess, which only ever leaves less room for charging:
    # the first cycle (the meter's first reading is published before the controller has
    # subscribed), and cycles where the busy test program delayed a message so much that
    # the newest reading had become too old. Measured on this computer: 3 to 10 of about 930.
    reader = night["controller"].meter_client
    assert isinstance(reader, MqttMeterReader)
    cycles = reader.fresh_cycles + reader.safe_guess_cycles
    assert cycles > 900
    assert reader.fresh_cycles >= 0.97 * cycles


async def test_the_controller_marks_cycles_with_the_safe_guess_in_its_site_log():
    # A reader that never gets a reading (it is not even started): every cycle uses the guess.
    from src.controller.site_control import SiteController

    reader = MqttMeterReader("127.0.0.1", 1, "never-started")
    controller = SiteController({}, SimulationClock(T0, 60), reader, "equal_sharing")
    await controller.control_cycle()
    sample = controller.site_log[-1]
    assert sample["building_kw"] == SAFE_GUESS_KW
    assert sample["building_is_guess"]
    assert reader.safe_guess_cycles == 1
