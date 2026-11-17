"""
A simulated building energy meter that speaks Modbus TCP.

It works like a real meter in a switch cabinet: it measures the building's
power (here: taken from the synthetic profile in building_load.py), keeps
its register table up to date, and answers whoever reads it. It does not
know or care who reads it.

Clock: the meter needs the simulated time to know the building load. The
controller writes the simulated time into registers 20-21 on every read,
and the meter follows it. Real meters have similar clock registers that a
SCADA system writes to keep the meter's clock right.

Run it (before the controller):
    python -m src.meter.modbus_meter
"""

import asyncio
import logging
from datetime import datetime

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

import settings
from src.log_setup import setup_logging
from src.meter.building_load import building_load_kw
from src.scenarios import get_scenario
from src.meter.registers import (
    CLOCK_SYNC_UNIX_S,
    HOLDING_REGISTERS,
    REGISTER_COUNT,
    build_meter_registers,
    registers_to_uint32,
)
from src.sim_clock import SimulationClock

GRID_FREQUENCY_HZ = 50.0


async def start_meter_server(host, port):
    """
    Start the Modbus TCP server with an empty register table.

    In: address and port to listen on (port 0 picks a free one, used in tests).
    Out: (the running server, the port it listens on).
    """
    # One device (unit id 1) with REGISTER_COUNT holding registers, all 0 at the start.
    # Reading beyond the table gives the Modbus error "illegal data address", like a real meter.
    device = SimDevice(settings.METER_DEVICE_ID,
                       simdata=[SimData(0, count=REGISTER_COUNT, values=0, datatype=DataType.REGISTERS)])
    server = ModbusTcpServer(device, address=(host, port))

    # background=True: start listening and return, instead of blocking here forever.
    await server.serve_forever(background=True)
    listening_port = server.transport.sockets[0].getsockname()[1]
    return server, listening_port


class SimulatedMeter:
    """
    The meter's measuring part: keeps the register table of a running server up to date.
    A class is used because the meter has to remember its energy counter and clock between updates.
    """

    def __init__(self, server, clock, load_spikes=()):
        self.server = server
        self.clock = clock
        # Extra loads of the scenario (synthetic), e.g. the 40 kW workshop spike.
        self.load_spikes = load_spikes
        self.building_power_kw = 0.0
        self.energy_import_kwh = 0.0
        self.last_update_time = None
        self.last_clock_sync_unix_s = 0
        self.events = logging.getLogger("METER")

    async def follow_clock_sync(self):
        """If the controller has written a new time into registers 20-21, set our clock to it."""
        registers = await self.server.async_getValues(settings.METER_DEVICE_ID, HOLDING_REGISTERS,
                                                      CLOCK_SYNC_UNIX_S, 2)
        unix_s = registers_to_uint32(registers)
        if unix_s != 0 and unix_s != self.last_clock_sync_unix_s:
            sync_time = datetime.fromtimestamp(unix_s, tz=settings.TIMEZONE)
            self.clock.set_time(sync_time)
            self.last_clock_sync_unix_s = unix_s

    async def update_registers(self):
        """One measuring step: count energy, take the new building power, write the register table."""
        await self.follow_clock_sync()
        now = self.clock.now()

        # The energy counter adds up the power that was flowing since the last step.
        if self.last_update_time is not None:
            duration_hours = max(0.0, (now - self.last_update_time).total_seconds() / 3600)
            self.energy_import_kwh += self.building_power_kw * duration_hours
        self.last_update_time = now

        new_power_kw = building_load_kw(now, self.load_spikes)
        if new_power_kw != self.building_power_kw:
            self.events.info("%s building load %.1f kW", now.strftime("%H:%M:%S"), new_power_kw)
        self.building_power_kw = new_power_kw

        registers = build_meter_registers(self.building_power_kw, self.energy_import_kwh,
                                          settings.PHASE_VOLTAGE_V, GRID_FREQUENCY_HZ)
        await self.server.async_setValues(settings.METER_DEVICE_ID, HOLDING_REGISTERS, 0, registers)

    async def run(self):
        """Update the registers once per simulated minute, forever."""
        while True:
            await self.update_registers()
            await asyncio.sleep(self.clock.real_seconds(1))


async def run_meter():
    """Start the meter server and keep its registers up to date. Runs forever."""
    scenario = get_scenario(settings.SCENARIO)
    # Until the controller writes its time into the clock registers, the meter starts at the scenario's start.
    clock = SimulationClock(scenario["start_time"], settings.SIMULATED_SECONDS_PER_REAL_SECOND)
    server, port = await start_meter_server(settings.METER_HOST, settings.METER_PORT)
    logging.getLogger("METER").info("Modbus TCP meter listening on %s:%s, unit id %s, scenario %s",
                                    settings.METER_HOST, port, settings.METER_DEVICE_ID, scenario["name"])
    await SimulatedMeter(server, clock, scenario["load_spikes"]).run()


def main():
    setup_logging()
    asyncio.run(run_meter())


if __name__ == "__main__":
    main()
