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

MANUFACTURER = "Danfoss"
MODEL = "eTRV"

HANDLER_BATTERY = 0x10
HANDLER_PIN = 0x24
HANDLER_PIN_SETTINGS = 0x27
HANDLER_SETTINGS = 0x2A
HANDLER_TEMPERATURE = 0x2D
HANDLER_NAME = 0x30
HANDLER_CURRENT_TIME = 0x36
HANDLER_SECRET_KEY = 0x3F

MIN_TEMP_C = 10.0
MAX_TEMP_C = 40.0
TEMP_STEP = 0.5
