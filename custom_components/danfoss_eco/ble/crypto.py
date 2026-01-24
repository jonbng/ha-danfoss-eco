"""Encryption helpers compatible with legacy eTRV protocol."""

from __future__ import annotations

import xxtea


def _reverse_chunks(data: bytes) -> bytes:
    result = bytearray()
    for i in range(0, len(data), 4):
        result += data[i : i + 4][::-1]
    return bytes(result)


def etrv_decode(data: bytes, key: bytes) -> bytes:
    data = _reverse_chunks(data)
    data = xxtea.decrypt(data, key, padding=False)
    data = _reverse_chunks(data)
    return data


def etrv_encode(data: bytes, key: bytes) -> bytes:
    data = _reverse_chunks(data)
    data = xxtea.encrypt(data, key, padding=False)
    data = _reverse_chunks(data)
    return data
