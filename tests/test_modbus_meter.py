"""
Tests of the building meter: how values are packed into Modbus registers,
and reading the simulated meter over a real Modbus TCP connection.
"""

from datetime import timedelta

import pytest

import settings
from src.controller.site_control import read_building_load_kw
from src.controller.meter_reader import create_meter_client, read_meter, write_meter_clock
from src.meter.registers import (
    build_meter_registers,
    decode_meter_registers,
    float32_to_registers,
    registers_to_float32,
    registers_to_uint32,
    uint32_to_registers,
)
from src.scenarios import clock_time_on_day

DAY = settings.SIMULATION_START_TIME.date()


# ---------------------------------------------------------------------------
# Packing values into registers (worked out by hand)
# ---------------------------------------------------------------------------

def test_float32_is_split_into_high_and_low_word():
    # 45000.0 as an IEEE 754 float is 0x472FC800:
    # sign 0, exponent 142 (2^15), mantissa 1.373291015625 -> 1.373291015625 x 32768 = 45000.
    assert float32_to_registers(45000.0) == [0x472F, 0xC800]
    assert registers_to_float32([0x472F, 0xC800]) == 45000.0


def test_wrong_word_order_gives_a_wrong_value():
    # The same two registers read with the words swapped: a classic commissioning mistake.
    assert registers_to_float32([0xC800, 0x472F]) != pytest.approx(45000.0)


def test_uint32_is_split_into_high_and_low_word():
    # 1 234 567 = 0x0012D687 -> high word 0x0012, low word 0xD687
    assert uint32_to_registers(1234567) == [0x0012, 0xD687]
    assert registers_to_uint32([0x0012, 0xD687]) == 1234567


def test_register_table_decodes_to_the_values_put_in():
    registers = build_meter_registers(total_power_kw=22.0, energy_import_kwh=1234.5,
                                      phase_voltage_v=230.0, frequency_hz=50.0)
    values = decode_meter_registers(registers)

    assert values["power_kw"] == pytest.approx(22.0)
    assert values["voltage_l1_v"] == pytest.approx(230.0)
    # 22 000 W / (3 x 230 V) = 31.88 A per phase
    assert values["current_l2_a"] == pytest.approx(31.884, abs=0.001)
    # Scaled integers: frequency stored as 5000 (x 0.01 Hz), energy as 12345 (x 0.1 kWh)
    assert registers[14] == 5000
    assert values["frequency_hz"] == pytest.approx(50.0)
    assert values["energy_import_kwh"] == pytest.approx(1234.5)


# ---------------------------------------------------------------------------
# Over a real Modbus TCP connection
# ---------------------------------------------------------------------------

async def test_controller_reads_the_correct_kw_from_the_meter(meter):
    # Put the meter's clock at 10:30, when the synthetic profile says 45 kW (workshop working).
    meter["meter"].clock.set_time(clock_time_on_day(DAY, "10:30"))
    await meter["meter"].update_registers()

    values = await read_meter(meter["client"])

    assert values["power_kw"] == pytest.approx(45.0)
    assert values["voltage_l3_v"] == pytest.approx(230.0)
    assert values["frequency_hz"] == pytest.approx(50.0)


async def test_meter_clock_follows_the_time_the_controller_writes(meter):
    controller_time = clock_time_on_day(DAY, "19:00")

    await write_meter_clock(meter["client"], controller_time)
    await meter["meter"].update_registers()

    assert abs(meter["meter"].clock.now() - controller_time) < timedelta(minutes=1)
    # At 19:00 the synthetic profile gives 22 kW (evening).
    assert (await read_meter(meter["client"]))["power_kw"] == pytest.approx(22.0)


async def test_meter_counts_energy_over_time(meter):
    simulated_meter = meter["meter"]
    simulated_meter.clock.set_time(clock_time_on_day(DAY, "10:00"))
    await simulated_meter.update_registers()

    # One simulated hour later at 45 kW, the counter must show 45 kWh.
    simulated_meter.clock.set_time(clock_time_on_day(DAY, "11:00"))
    await simulated_meter.update_registers()

    assert (await read_meter(meter["client"]))["energy_import_kwh"] == pytest.approx(45.0)


async def test_unreadable_meter_gives_last_reading_or_a_safe_guess():
    # Nothing listens on this port, so every read fails.
    client = create_meter_client("127.0.0.1", 1)
    now = settings.SIMULATION_START_TIME

    assert await read_building_load_kw(client, now, last_building_kw=None) == settings.BUILDING_LOAD_IF_METER_UNKNOWN_KW
    assert await read_building_load_kw(client, now, last_building_kw=22.0) == 22.0
    client.close()
