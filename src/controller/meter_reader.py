"""
The controller's Modbus TCP client for the building energy meter.

This is the same job a PLC does with an MB_CLIENT block: open a TCP
connection to the meter, read holding registers, and turn the raw 16 bit
words into engineering values using the meter's register map.
"""

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

import settings
from src.meter.registers import (
    CLOCK_SYNC_UNIX_S,
    MEASUREMENT_REGISTER_COUNT,
    decode_meter_registers,
    uint32_to_registers,
)


class MeterReadError(Exception):
    """The meter could not be read: no connection, or it answered with a Modbus error."""


def create_meter_client(host, port):
    """In: meter address and port. Out: a Modbus TCP client (not connected yet)."""
    # timeout: give up on one request after 1 s. retries: try a failed request once more.
    return AsyncModbusTcpClient(host, port=port, timeout=1, retries=1)


async def read_meter(client):
    """
    Read all measurement registers of the meter in one request.

    In: Modbus client.
    Out: dict of decoded values (see decode_meter_registers), e.g. power_kw.
    Raises MeterReadError if the meter cannot be read.
    """
    try:
        # Reconnect if the connection was lost, e.g. the meter was restarted.
        if not client.connected:
            await client.connect()
        response = await client.read_holding_registers(0, count=MEASUREMENT_REGISTER_COUNT,
                                                       device_id=settings.METER_DEVICE_ID)
    except ModbusException as error:
        raise MeterReadError(str(error)) from error

    # A Modbus "exception response" (e.g. illegal address) comes back as an error object, not as data.
    if response.isError():
        raise MeterReadError(str(response))
    return decode_meter_registers(response.registers)


async def write_meter_clock(client, sim_time):
    """
    Write the simulated time into the meter's clock registers (20-21), so its clock follows ours.

    In: Modbus client, simulated time (datetime).
    Raises MeterReadError if the write fails.
    """
    unix_s = int(sim_time.timestamp())
    try:
        if not client.connected:
            await client.connect()
        response = await client.write_registers(CLOCK_SYNC_UNIX_S, uint32_to_registers(unix_s),
                                                device_id=settings.METER_DEVICE_ID)
    except ModbusException as error:
        raise MeterReadError(str(error)) from error
    if response.isError():
        raise MeterReadError(str(response))
