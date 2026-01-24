## Danfoss Eco Home Assistant Integration

This folder contains a custom Home Assistant integration for Danfoss Eco (eTRV) thermostats.
It connects over BLE, performs the legacy key exchange flow, and exposes a climate entity plus sensors.

### Credits
Original inspiration and protocol reference from [Keton](https://github.com/keton/etrv2mqtt) and [AdamStrojek](https://github.com/AdamStrojek/libetrv).

### Features
- Bluetooth discovery and guided setup flow
- Key retrieval during pairing (single flow)
- Climate entity with setpoint control
- Battery, reported name, last update, and optional room temperature sensors
- Debounced setpoint writes

### Install (custom integration)
1. Copy `custom_components/danfoss_eco` into your Home Assistant `custom_components/` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **Danfoss Eco**.
4. Follow the setup flow, and press/hold the thermostat button when prompted.

### Notes
- The device must be in pairing mode to retrieve the secret key.
- BLE access depends on your Home Assistant host Bluetooth setup.
