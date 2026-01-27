"""Constants for the Danfoss Eco integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "danfoss_eco"

CONF_SECRET_KEY = "secret_key"
CONF_PIN = "pin"
CONF_POLL_INTERVAL = "poll_interval"
CONF_STAY_CONNECTED = "stay_connected"
CONF_REPORT_ROOM_TEMPERATURE = "report_room_temperature"
CONF_SETPOINT_DEBOUNCE = "setpoint_debounce"

DEFAULT_POLL_INTERVAL = timedelta(seconds=3600)
DEFAULT_STAY_CONNECTED = False
DEFAULT_REPORT_ROOM_TEMPERATURE = True
DEFAULT_SETPOINT_DEBOUNCE = 3
DEFAULT_PIN = 0

MANUFACTURER = "Danfoss"
MODEL = "eTRV"

UUID_SERVICE_CUSTOM = "10020000-2749-0001-0000-00805f9b042f"
UUID_SERVICE_PIN = "10010000-2749-0001-0000-00805f9b042f"

UUID_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"
UUID_PIN = "10020001-2749-0001-0000-00805f9b042f"
UUID_SETTINGS = "10020003-2749-0001-0000-00805f9b042f"
UUID_TEMPERATURE = "10020005-2749-0001-0000-00805f9b042f"
UUID_NAME = "10020006-2749-0001-0000-00805f9b042f"
UUID_CURRENT_TIME = "10020008-2749-0001-0000-00805f9b042f"
UUID_SECRET_KEY = "1002000b-2749-0001-0000-00805f9b042f"

# GATT handles from libetrv - skipping service discovery speeds up operations significantly
# These are hardcoded handles that match the Danfoss Eco device's GATT structure
# Map: UUID -> handle (int)
HANDLE_MAP: dict[str, int] = {
    UUID_BATTERY: 0x10,
    UUID_PIN: 0x24,
    UUID_SETTINGS: 0x2A,
    UUID_TEMPERATURE: 0x2D,
    UUID_NAME: 0x30,
    UUID_SECRET_KEY: 0x3F,
}

MIN_TEMP_C = 10.0
MAX_TEMP_C = 40.0
TEMP_STEP = 0.5
