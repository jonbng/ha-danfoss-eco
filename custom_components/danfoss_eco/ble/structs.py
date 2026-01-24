"""Data structure helpers for eTRV characteristics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import struct


def _to_temperature(raw: int) -> float:
    return raw * 0.5


def _from_temperature(value: float) -> int:
    return int(value * 2)


@dataclass
class BatteryData:
    battery: int

    @classmethod
    def from_bytes(cls, data: bytes) -> "BatteryData":
        return cls(battery=data[0])


@dataclass
class TemperatureData:
    set_point: float
    room_temperature: float
    _raw: bytes

    @classmethod
    def from_bytes(cls, data: bytes) -> "TemperatureData":
        set_point_raw = data[0]
        room_raw = data[1]
        return cls(
            set_point=_to_temperature(set_point_raw),
            room_temperature=_to_temperature(room_raw),
            _raw=data,
        )

    def with_set_point(self, set_point: float) -> bytes:
        raw = bytearray(self._raw)
        raw[0] = _from_temperature(set_point)
        return bytes(raw)


@dataclass
class NameData:
    name: str

    @classmethod
    def from_bytes(cls, data: bytes) -> "NameData":
        return cls(name=data.decode("utf-8").rstrip("\0"))


@dataclass
class CurrentTimeData:
    time_local: datetime | None
    time_offset: int

    @classmethod
    def from_bytes(cls, data: bytes) -> "CurrentTimeData":
        time_local, time_offset = struct.unpack(">ii", data[:8])
        if time_local == 0:
            return cls(time_local=None, time_offset=time_offset)
        tz = timezone(timedelta(seconds=time_offset))
        return cls(time_local=datetime.fromtimestamp(time_local, tz=tz), time_offset=time_offset)


@dataclass
class SettingsData:
    config_bits: int
    temperature_min: float
    temperature_max: float
    frost_protection_temperature: float
    schedule_mode: int
    vacation_temperature: float
    vacation_from: int
    vacation_to: int

    @classmethod
    def from_bytes(cls, data: bytes) -> "SettingsData":
        (
            config_bits,
            temperature_min,
            temperature_max,
            frost_protection_temperature,
            schedule_mode,
            vacation_temperature,
            vacation_from,
            vacation_to,
        ) = struct.unpack(">6Bii2x", data[:16])
        return cls(
            config_bits=config_bits,
            temperature_min=_to_temperature(temperature_min),
            temperature_max=_to_temperature(temperature_max),
            frost_protection_temperature=_to_temperature(frost_protection_temperature),
            schedule_mode=schedule_mode,
            vacation_temperature=_to_temperature(vacation_temperature),
            vacation_from=vacation_from,
            vacation_to=vacation_to,
        )


@dataclass
class PinSettingsData:
    pin_number: int
    pin_enabled: bool

    @classmethod
    def from_bytes(cls, data: bytes) -> "PinSettingsData":
        pin_number, pin_enabled = struct.unpack(">IB3x", data[:8])
        return cls(pin_number=pin_number, pin_enabled=bool(pin_enabled & 0x01))


@dataclass
class SecretKeyData:
    key: str

    @classmethod
    def from_bytes(cls, data: bytes) -> "SecretKeyData":
        return cls(key=data[:16].hex())
