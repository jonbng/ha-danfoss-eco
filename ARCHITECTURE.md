## Architecture

This integration implements a native Home Assistant (HA) Bluetooth-based flow for Danfoss Eco (eTRV) thermostats. It replaces the legacy multi-repo stack (BLE library + MQTT bridge + add-ons) with a single custom integration that discovers devices, performs key exchange, and exposes entities directly in HA.

### Goals
- Preserve the legacy BLE protocol and XXTEA + chunk-reversal encoding.
- Provide a single, friendly setup flow (scan → connect → key exchange).
- Expose climate control and key sensors without MQTT.
- Keep dependencies minimal and HA-native.

### Repository Layout
- `custom_components/danfoss_eco/`
  - `__init__.py`: integration entrypoint and platform setup.
  - `manifest.json`: HA metadata, requirements, and Bluetooth matcher.
  - `config_flow.py`: setup flow and options.
  - `coordinator.py`: shared polling, debounced writes.
  - `climate.py`: thermostat entity.
  - `sensor.py`: battery/name/last update/room temperature sensors.
  - `ble/`: BLE protocol + crypto helpers.
  - `translations/en.json`: UI strings for config and options.

### BLE Protocol Layer
The legacy BLE protocol is re-implemented in `custom_components/danfoss_eco/ble/`:
- `crypto.py` preserves XXTEA + 4-byte chunk reversal.
- `structs.py` decodes and encodes characteristic payloads.
- `client.py` wraps Bleak with HA Bluetooth device discovery and PIN handling.
- `device.py` provides high-level read/write operations used by the coordinator.

BLE UUIDs used (verified on device):
- Secret key (pairing mode): `1002000b-2749-0001-0000-00805f9b042f`
- PIN write (unlock reads): `10020001-2749-0001-0000-00805f9b042f`
- Temperature: `10020002-2749-0001-0000-00805f9b042f`
- Name: `10020003-2749-0001-0000-00805f9b042f`
- Battery: `00002a19-0000-1000-8000-00805f9b34fb`

### Setup Flow
`config_flow.py` provides:
- `async_step_bluetooth`: auto-discovery when the HA Bluetooth integration sees a matching device.
- `async_step_user`: manual scan list with fallback to address entry.
- `async_step_pair`: connect, optionally send PIN, and read the secret key.

The pairing step instructs users to press/hold the device button so the secret key can be read from handler `0x3F`.

### Polling + Writes
`coordinator.py` implements a `DataUpdateCoordinator` that:
- Polls BLE data on a configurable interval.
- Centralizes state for all entities.
- Debounces setpoint writes and refreshes state after a write.

### Entities
- `climate.py`: single climate entity (heat mode, target temperature).
- `sensor.py`: battery, reported name, last update, optional room temperature.

### Bluetooth Discovery Matcher
`manifest.json` uses a `local_name` matcher that matches the thermostat’s advertised local name pattern, which includes the MAC address and `;eTRV`. The pattern can be adjusted once a stable prefix is confirmed.

### Dependencies
- `xxtea` (required, pinned in `manifest.json`) for protocol compatibility.
- `bleak` is used via HA’s Bluetooth stack; it is not required in `manifest.json`.

### HACS Notes
HACS expects:
- `hacs.json` at the repository root.
- A single integration under `custom_components/`.
- A properly sorted `manifest.json` (domain, name, then alphabetical).
- Repo description, topics, and a brands submission for the custom domain.

### Future Extensions
Potential additions:
- Extra sensors for device settings and configuration bits.
- Schedule support if required.
- Diagnostics and logbook entries for pairing/troubleshooting.
