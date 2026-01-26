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

### Important: Connection Timing

The Danfoss Eco eTRV has **unusually slow BLE communication**:

- **Initial connection**: ~27 seconds
- **GATT service discovery**: ~30 seconds  
- **Total time per operation**: Up to 60-90 seconds

This is normal behavior for this device. The integration uses a 90-second connection timeout to accommodate this. During setup and polling, expect significant wait times—this is not a bug.

### Notes
- The device must be in pairing mode to retrieve the secret key (press and hold the timer button until the display shows the pairing icon).
- Reads are unlocked by writing the PIN (default `0000`) to the device before polling.
- BLE access depends on your Home Assistant host Bluetooth setup.
- Due to the slow connection times, frequent polling is not recommended. The default poll interval is 1 hour.
