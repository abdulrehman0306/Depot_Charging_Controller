"""
The building meter's Modbus register map, and how values are packed into registers.

A Modbus register holds 16 bits (a number from 0 to 65535). Anything bigger,
like a 32 bit float or a 32 bit counter, is spread over two registers next to
each other. Both the meter (which writes) and the controller (which reads)
use this one file, the same way a PLC programmer and a meter maker both use
the meter's datasheet.

This map is written for this project in the style of common industrial
energy meters. It is not a copy of any real meter's map.

Holding registers, read with function code 03, unit id 1:

| Address | Value                   | Type    | Unit / scale        |
|---------|-------------------------|---------|---------------------|
| 0-1     | total active power      | FLOAT32 | W                   |
| 2-3     | voltage L1-N            | FLOAT32 | V                   |
| 4-5     | voltage L2-N            | FLOAT32 | V                   |
| 6-7     | voltage L3-N            | FLOAT32 | V                   |
| 8-9     | current L1              | FLOAT32 | A                   |
| 10-11   | current L2              | FLOAT32 | A                   |
| 12-13   | current L3              | FLOAT32 | A                   |
| 14      | grid frequency          | UINT16  | 0.01 Hz (5000 = 50.00 Hz) |
| 15      | (unused)                |         |                     |
| 16-17   | active energy imported  | UINT32  | 0.1 kWh             |
| 18-19   | (unused)                |         |                     |
| 20-21   | clock sync, writable    | UINT32  | seconds since 1970 (Unix time) |

Word order: high word first ("big endian", also written ABCD). Byte and word
order differ between meter makers; with a wrong word order a float decodes
to nonsense, which is a classic commissioning problem.
"""

import struct

# Function code 03 = "read holding registers". The pymodbus server also uses it
# to pick the holding register table when our program changes values.
HOLDING_REGISTERS = 3

TOTAL_POWER_W = 0
VOLTAGE_L1_V = 2
VOLTAGE_L2_V = 4
VOLTAGE_L3_V = 6
CURRENT_L1_A = 8
CURRENT_L2_A = 10
CURRENT_L3_A = 12
FREQUENCY_CENTI_HZ = 14
ENERGY_IMPORT_DECI_KWH = 16
CLOCK_SYNC_UNIX_S = 20

# Total number of registers in the map (addresses 0 to 21).
REGISTER_COUNT = 22

# Number of registers the meter itself fills (0 to 19). 20-21 are written by the controller.
MEASUREMENT_REGISTER_COUNT = 20


# ---------------------------------------------------------------------------
# Packing values into registers and back
# ---------------------------------------------------------------------------

def float32_to_registers(value):
    """
    In: a number.
    Out: [high word, low word], the IEEE 754 32 bit float spread over two registers.
    """
    # ">f" = 4 bytes of a 32 bit float, most significant byte first.
    # ">HH" = read those same 4 bytes back as two 16 bit unsigned numbers.
    four_bytes = struct.pack(">f", value)
    high_word, low_word = struct.unpack(">HH", four_bytes)
    return [high_word, low_word]


def registers_to_float32(registers):
    """In: [high word, low word]. Out: the 32 bit float they hold."""
    four_bytes = struct.pack(">HH", registers[0], registers[1])
    return struct.unpack(">f", four_bytes)[0]


def uint32_to_registers(value):
    """
    In: a whole number from 0 to 4 294 967 295.
    Out: [high word, low word].
    """
    four_bytes = struct.pack(">I", value)
    high_word, low_word = struct.unpack(">HH", four_bytes)
    return [high_word, low_word]


def registers_to_uint32(registers):
    """In: [high word, low word]. Out: the 32 bit unsigned whole number they hold."""
    four_bytes = struct.pack(">HH", registers[0], registers[1])
    return struct.unpack(">I", four_bytes)[0]


# ---------------------------------------------------------------------------
# The whole register table
# ---------------------------------------------------------------------------

def build_meter_registers(total_power_kw, energy_import_kwh, phase_voltage_v, frequency_hz):
    """
    Fill the meter's measurement registers (addresses 0 to 19).

    In: building power [kW], energy counter [kWh], phase voltage [V], frequency [Hz].
    Out: list of 20 register values.
    """
    # Simplification: the building load is spread evenly over the three phases,
    # with power factor 1, so each phase current is P / (3 x U).
    phase_current_a = total_power_kw * 1000 / (3 * phase_voltage_v)

    registers = [0] * MEASUREMENT_REGISTER_COUNT
    registers[TOTAL_POWER_W:TOTAL_POWER_W + 2] = float32_to_registers(total_power_kw * 1000)
    for address in (VOLTAGE_L1_V, VOLTAGE_L2_V, VOLTAGE_L3_V):
        registers[address:address + 2] = float32_to_registers(phase_voltage_v)
    for address in (CURRENT_L1_A, CURRENT_L2_A, CURRENT_L3_A):
        registers[address:address + 2] = float32_to_registers(phase_current_a)

    # Scaled integers: the meter sends a whole number, the reader multiplies by the scale.
    registers[FREQUENCY_CENTI_HZ] = round(frequency_hz * 100)
    energy_deci_kwh = round(energy_import_kwh * 10)
    registers[ENERGY_IMPORT_DECI_KWH:ENERGY_IMPORT_DECI_KWH + 2] = uint32_to_registers(energy_deci_kwh)
    return registers


def decode_meter_registers(registers):
    """
    Turn the meter's measurement registers back into values with units.

    In: list of at least 20 register values, starting at address 0.
    Out: dict with power_kw, phase voltages [V], phase currents [A], frequency_hz, energy_import_kwh.
    """
    return {
        "power_kw": registers_to_float32(registers[TOTAL_POWER_W:TOTAL_POWER_W + 2]) / 1000,
        "voltage_l1_v": registers_to_float32(registers[VOLTAGE_L1_V:VOLTAGE_L1_V + 2]),
        "voltage_l2_v": registers_to_float32(registers[VOLTAGE_L2_V:VOLTAGE_L2_V + 2]),
        "voltage_l3_v": registers_to_float32(registers[VOLTAGE_L3_V:VOLTAGE_L3_V + 2]),
        "current_l1_a": registers_to_float32(registers[CURRENT_L1_A:CURRENT_L1_A + 2]),
        "current_l2_a": registers_to_float32(registers[CURRENT_L2_A:CURRENT_L2_A + 2]),
        "current_l3_a": registers_to_float32(registers[CURRENT_L3_A:CURRENT_L3_A + 2]),
        "frequency_hz": registers[FREQUENCY_CENTI_HZ] * 0.01,
        "energy_import_kwh": registers_to_uint32(registers[ENERGY_IMPORT_DECI_KWH:ENERGY_IMPORT_DECI_KWH + 2]) * 0.1,
    }
