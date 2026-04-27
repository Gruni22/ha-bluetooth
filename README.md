# Home Assistant Bluetooth API

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/Gruni22/ha-bluetooth?include_prereleases)](https://github.com/Gruni22/ha-bluetooth/releases)
[![License](https://img.shields.io/github/license/Gruni22/ha-bluetooth)](LICENSE)

Control your Home Assistant **fully offline** via Bluetooth Low Energy — straight from your Android phone or Android Auto. No Wi-Fi required, no cloud account, no Companion-App login.

> **Companion app:** [`btdashboard`](https://github.com/Gruni22/ha-android) — standalone APK for phone & Android Auto.
> **Firmware:** [`esp32-ha`](https://github.com/Gruni22/esp32-ha) — open-source firmware for the BLE gateway ESP32.

---

## How it works

```
┌──────────────────────────┐         ┌──────────────────┐
│  btdashboard             │  BLE    │   ESP32-S3       │
│  Phone / Android Auto    │◀═══════▶│   Open BLE       │
└──────────────────────────┘         └────────┬─────────┘
                                              │ USB-CDC
                                              ▼
                                     ┌──────────────────┐
                                     │  Home Assistant  │
                                     │  Custom Compo.   │
                                     └──────────────────┘
```

The ESP32 acts as the BLE radio and is plugged into the HA host (Pi/mini-PC) via USB. The custom component talks to the HA Core API directly (`hass.states`, `hass.services`, `area_registry`, …) — no REST round-trips.

### Key points

- **Open BLE** — no pairing dialog, no bonding, no LTK. Authentication happens at the application layer via a 32-bit passcode embedded in every packet.
- **Local control** — all data stays on the LAN/PAN. No external service, no internet.
- **Auto-sync** — areas, devices and dashboards are synced into a local Room database on first setup; state changes are pushed by the server.
- **Android Auto-ready** — the companion app ships with a `CarAppService`. Dashboards/views containing "aa" in their name appear automatically in the car's launcher.

---

## Installation

### Option A — via HACS (recommended)

1. **HACS** → ⋮ → **Custom repositories**
2. Repository: `https://github.com/Gruni22/ha-bluetooth` · Category: **Integration** → **Add**
3. Search for "Bluetooth API" → **Download**
4. Restart Home Assistant
5. **Settings → Devices & Services → + Add Integration → "Bluetooth API"**

### Option B — manual

```bash
cd /config/custom_components
git clone https://github.com/Gruni22/ha-bluetooth.git tmp
mv tmp/custom_components/bluetooth_api .
rm -rf tmp
ha core restart
```

### Configuration wizard

After clicking "Add Integration" the wizard walks you through:

| Step | Description |
|------|-------------|
| **1. Adapter mode** | "ESP32-S3 (USB-Serial Gateway)" or "Raspberry Pi Bluetooth" *(planned)* |
| **2. USB port** | Auto-discovery dropdown — the ESP32 is detected via VID `0x303A` (Espressif) and shown at the top. A "Manual entry…" option is kept as a fallback. |
| **3. Passcode** | HA generates a 32-bit passcode (displayed as `XXXX-XXXX`) and exposes a QR code at `/api/bluetooth_api/setup_qr`. |

After "Submit" a **Persistent Notification** is posted with the passcode and a link to the QR code — scan it during app setup.

---

## App setup (`btdashboard`)

1. Install the APK — see the [btdashboard repo](https://github.com/Gruni22/ha-android).
2. Open the app → **"Connect via Bluetooth"** → pick your ESP32 from the discovery list.
3. **Scan the QR code** (from the HA notification) — the passcode is stored.
4. The initial sync runs (areas → devices → dashboards).
5. Done — the dashboard appears.

### Android Auto

The same APK contains a `CarAppService`. As soon as the phone is connected to the car (or to the [Desktop Head Unit](https://developer.android.com/training/cars/testing) for testing), **Home Assistant Bluetooth** shows up in the launcher.

Dashboard filter: only views whose title or path contains **"aa"** (case-insensitive) are shown in the car — this lets you cleanly separate phone and Android Auto layouts.

---

## Protocol details

### BLE GATT structure

| UUID | Direction | Purpose |
|------|-----------|---------|
| `a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1234` | — | Service |
| `a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1235` | ESP32 → App | TX (Notify) |
| `a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1236` | App → ESP32 | RX (Write / WriteNoResponse) |

### Packet format

```
[0xAA 0xBB][passcode u32 BE][cmd u8][flags u8][len u16 BE][payload …][crc16 BE][0xCC 0xDD]
```

### Command codes

| Code | Name | Direction | Payload |
|------|------|-----------|---------|
| `0x01` | ACK | both | — |
| `0x02` | NACK | both | `{error}` |
| `0x10` | REQ_AREAS | App → HA | — |
| `0x11` | ANS_AREAS | HA → App | `[{id, name, icon?}, …]` |
| `0x12` | REQ_DEVICES | App → HA | `{area_id?}` |
| `0x13` | ANS_DEVICES | HA → App | `[{id, entity_id, name, domain, area_id, state, attrs}, …]` |
| `0x14` | REQ_DASHBOARDS | App → HA | — |
| `0x15` | ANS_DASHBOARDS | HA → App | `[{id, url_path, title, views:[…]}, …]` |
| `0x20` | REQ_STATE | App → HA | `{entity_id}` |
| `0x21` | ANS_STATE | HA → App | `{entity_id, state, attributes, last_changed}` |
| `0x22` | CALL_SERVICE | App → HA | `{domain, service, entity_id, data?}` |
| `0x23` | ANS_CALL_SERVICE | HA → App | `{success, error?}` |
| `0x30` | STATE_CHANGE | HA → App | identical to `ANS_STATE` (server push) |

### BLE chunking

BLE has a 247-byte MTU. Larger packets are split using `[flag u8][chunk …]`: `0x00` = more chunks follow, `0x01` = final chunk.

### USB framing (Pi ↔ ESP32)

`[length u32 BE][payload]` — 64 KB max. The Pi-side reader has a sliding-window recovery that automatically re-syncs after corruption (e.g. ESP32 boot logs).

---

## Security model

| Layer | Measure |
|-------|---------|
| BLE | **Open** — `setSecurityAuth(false, false, false)`. No bonding, no encryption. |
| App | **Passcode** (32 bits) in every packet. The Pi rejects packets with the wrong passcode. |
| HA | Only locally callable API methods. No remote access. |

**Threat model:** anyone within BLE range (~10 m) who learns the passcode can issue commands. The passcode is only displayed once as a QR code inside HA itself.

---

## Persistent debug logging

The UI logger level is stored in `.storage/core.logger` and overrides `configuration.yaml` on restart. To reliably enable debug logs:

```bash
TOKEN="YOUR_LONG_LIVED_TOKEN"
curl -X POST "http://homeassistant.local:8123/api/services/logger/set_level" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"custom_components.bluetooth_api": "debug"}'
```

View logs: `ha core logs | grep bluetooth_api`.

---

## Troubleshooting

<details>
<summary><b>App shows no devices</b></summary>

The initial sync may have failed. In the app: **Settings → Active device → Remove**, then re-run the QR-code setup.
</details>

<details>
<summary><b>Sync timeout (cmd 0x10) despite an active BLE connection</b></summary>

Most often caused by stale NimBLE state on the ESP32. **Unplug the ESP32 from USB → wait 5 s → plug it back in.** Restart the app and re-run setup.
</details>

<details>
<summary><b><code>/dev/ttyACM0: No such file or directory</code> after reflash</b></summary>

After re-flashing, the kernel often assigns `/dev/ttyACM1` instead. Fix: unplug ESP32, wait 5 s, plug it back in — `/dev/ttyACM0` is reclaimed. Or use the stable `/dev/serial/by-id/…` path from the auto-discovery dropdown (v0.3+).
</details>

<details>
<summary><b><code>Bridge ended: Invalid RFCOMM frame length</code></b></summary>

Triggered by firmware that still writes plain-text `Serial.print(…)` on the USB-CDC data channel. Current firmware (≥ 2026-05-04) sends logs only as JSON frames. The Pi-side sliding-window recovery handles the noise — the message is harmless.
</details>

---

## Module map

| File | Purpose |
|------|---------|
| `__init__.py` | Setup entry, starts `UsbSerialServer` |
| `usb_serial_server.py` | USB ↔ HA bridge, command dispatch |
| `protocol.py` | Frame codec, CRC16-CCITT, sliding-window reader |
| `const.py` | UUIDs, command codes, config keys |
| `config_flow.py` | UI setup wizard (3 steps) |
| `api.py` | HTTP endpoints incl. `/api/bluetooth_api/setup_qr` |
| `button.py` | "Enable OTA" button |

---

## Roadmap

- [ ] Native Pi Bluetooth adapter (ESP32-less) via [bless](https://pypi.org/project/bless/) GATT server
- [ ] Optional BLE encryption (LE Secure Connections) as an alternative to the passcode
- [ ] In-frontend HA card for live monitoring of connected BLE clients

---

## Contributing

Pull requests welcome! Please open an issue first for larger changes so the direction can be agreed on.

## License

[MIT](LICENSE) — see `LICENSE.md` in the repo root.
