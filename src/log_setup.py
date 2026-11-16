"""How the programs print their output. Shared by the controller and the chargers."""

import logging

import settings


def setup_logging():
    """Print one line per event, with the sender's name in front."""
    logging.basicConfig(level=logging.INFO, format="%(name)-18s | %(message)s")

    # The ocpp library prints every raw message it sends and receives.
    # Great with one charger, too much with ten, so it is a setting.
    if settings.PRINT_OCPP_MESSAGES:
        logging.getLogger("ocpp").setLevel(logging.INFO)
    else:
        logging.getLogger("ocpp").setLevel(logging.WARNING)

    # The WebSocket and Modbus libraries' own messages are not interesting for us.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("pymodbus").setLevel(logging.WARNING)
