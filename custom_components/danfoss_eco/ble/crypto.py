"""Encryption helpers compatible with legacy eTRV protocol."""

from __future__ import annotations

import xxtea


class EtrvDecodeError(Exception):
    """Error decoding data from the device."""


def _reverse_chunks(data: bytes) -> bytes:
    result = bytearray()
    for i in range(0, len(data), 4):
        result += data[i : i + 4][::-1]
    return bytes(result)


def etrv_decode(data: bytes, key: bytes) -> bytes:
    if len(data) < 8:
        # Single byte = device error code (protocol-specific behavior)
        if len(data) == 1:
            error_code = data[0]
            raise EtrvDecodeError(
                f"Device returned error code {error_code:#04x}. "
                "The device may not be paired or the PIN may be incorrect."
            )
        raise EtrvDecodeError(
            f"Cannot decode data: length {len(data)} is less than 8 bytes. "
            "This may indicate the device is not paired or the secret key is invalid."
        )
    if len(data) % 4 != 0:
        raise EtrvDecodeError(
            f"Cannot decode data: length {len(data)} is not a multiple of 4 bytes. "
            "This may indicate the device is not paired or the secret key is invalid."
        )
    data = _reverse_chunks(data)
    data = xxtea.decrypt(data, key, padding=False)
    data = _reverse_chunks(data)
    return data


def etrv_encode(data: bytes, key: bytes) -> bytes:
    data = _reverse_chunks(data)
    data = xxtea.encrypt(data, key, padding=False)
    data = _reverse_chunks(data)
    return data
