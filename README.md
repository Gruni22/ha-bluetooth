# Home Assistant Bluetooth API

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/Gruni22/ha-bluetooth?include_prereleases)](https://github.com/Gruni22/ha-bluetooth/releases)
[![License](https://img.shields.io/github/license/Gruni22/ha-bluetooth)](LICENSE)

Control your Home Assistant **fully offline** via Bluetooth Low Energy — straight from your Android phone or Android Auto. No Wi-Fi required, no cloud account, no Companion-App login.

> **Companion app:** [`btdashboard`](https://github.com/Gruni22/ha-android) — standalone APK for phone & Android Auto.
> **Firmware:** [`esp32-ha`](https://github.com/Gruni22/esp32-ha) — open-source firmware for the BLE gateway ESP32.

---

## How it works

The integration speaks one protocol over three pluggable transports — pick the one that fits your hardware:

```
                                 ┌──────────────────┐
                       USB-CDC   │  ESP32-S3        │
                  ┌────────────▶ │  (open BLE)      │ ◀───╮
                  │              └──────────────────┘     │
                  │                                       │
                  │              ┌──────────────────┐     │   BLE
┌──────────────┐  │   WiFi/API   │  ESPHome device  │     │
│  Home        │ ─┼────────────▶ │  (ble_server)    │ ◀───┤
│  Assistant   │  │              └──────────────────┘     │
│  Custom      │  │                                       │
│  Component   │  │              ┌──────────────────┐     │
└──────────────┘  │   D-Bus      │  Pi BlueZ stack  │     │
                  └────────────▶ │  (bless GATT)    │ ◀───╯
                                 └──────────────────┘
                                                          ▲
                                                          │
                                                  ┌───────┴───────┐
                                                  │  btdashboard  │
                                                  │  phone / AA   │
                                                  └───────────────┘
```

The custom component talks to the HA Core API directly (`hass.states`, `hass.services`, `area_registry`, `label_registry`, …) — no REST round-trips.

### Key points

- **Three adapter modes** — pick at setup: ESP32-S3 over USB, an existing ESPHome device with the `ble_server` component over WiFi, or the Pi's own Bluetooth adapter via [bless](https://pypi.org/project/bless/).
- **Open BLE** — no pairing dialog, no bonding, no LTK. Authentication happens at the application layer via a 32-bit passcode embedded in every packet.
- **Local control** — all data stays on the LAN/PAN. No external service, no internet.
- **Label-based exposure filter** — only entities (or their parent devices) carrying the auto-created labels `BTDASH` / `BTDASHAA` are pushed to the app. Dashboards in the app are derived from `DASH_*` labels (e.g. `DASH_Battery` → app dashboard "Battery").
- **Auto-sync** — areas, devices and dashboards sync into a local Room database on first setup; state changes are pushed live, scoped to the exposure filter.
- **Android Auto-ready** — the companion app's `CarAppService` picks up entities labelled `BTDASHAA`.

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

After clicking "Add Integration" the wizard branches by adapter mode:

| Step | When | Description |
|------|------|-------------|
| **1. Adapter mode** | always | Pick ESP32-S3 (USB-Serial), ESPHome (`ble_server` over WLAN/native API), or Raspberry Pi Bluetooth (native, bless). Set the BLE device name shown to the phone here. |
| **2a. USB port** | ESP32 only | Auto-discovery dropdown — Espressif VID `0x303A` is shown at the top, plus common USB-UART bridges. "Manual entry…" as fallback. |
| **2b. ESPHome host** | ESPHome only | Dropdown harvested from your existing ESPHome integrations — host, port and `noise_psk` are pulled in automatically. Manual entry as fallback. |
| **2c. Conflict check** | Native only — and only when needed | If HA's own Bluetooth integration is enabled it'll fight bless for the adapter. The wizard lists conflicting entries and offers a one-click *disable* (they stay configured, just stopped). Skipped silently when no conflict. |

After "Submit" the entry is created and a **Persistent Notification** is posted with the passcode and a QR-code link at `/api/bluetooth_api/setup_qr/<entry_id>` — scan that during app setup.

### Exposing entities to the app

Setup auto-creates two labels in HA's label registry:

| Label | Purpose |
|-------|---------|
| `BTDASH` | Entity (or parent device) is exposed to the phone app |
| `BTDASHAA` | Entity (or parent device) is exposed to Android Auto |

Anything **without** at least one of these labels is invisible to the app — both in the device list and in live state updates. Labelling the *device* is usually enough; the integration unions device and entity labels so all of a device's entities inherit it.

Dashboards inside the app are also label-driven: every label whose name starts with `DASH_` becomes an app dashboard. Example:

- HA label `DASH_Battery` → app dashboard "Battery", listing every entity that carries this label *and* `BTDASH` / `BTDASHAA`.
- HA label `DASH_Lights` → app dashboard "Lights", same logic.

---

## App setup (`btdashboard`)

1. Install the APK — see the [btdashboard repo](https://github.com/Gruni22/ha-android).
2. Open the app → **"Connect via Bluetooth"** → pick your gateway from the discovery list (filtered by service UUID, so any of the three adapter modes shows up the same way).
3. **Scan the QR code** (from the HA notification) — the passcode is stored.
4. The initial sync runs (areas → devices → dashboards).
5. Done — the dashboard appears, populated only with `BTDASH`-labelled entities and grouped by `DASH_*` labels.

### Android Auto

The same APK contains a `CarAppService`. As soon as the phone is connected to the car (or to the [Desktop Head Unit](https://developer.android.com/training/cars/testing) for testing), **Home Assistant Bluetooth** shows up in the launcher.

Car content is driven by the `BTDASHAA` label — anything carrying it (entity- or device-level) shows up in Android Auto. This replaces the older "view title contains 'aa'" heuristic.

---

## Protocol details

### BLE GATT structure

| UUID | Direction | Purpose |
|------|-----------|---------|
| `a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1234` | — | Service |
| `a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1235` | Gateway → App | TX (Notify) |
| `a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1236` | App → Gateway | RX (Write / WriteNoResponse) |

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
| `0x13` | ANS_DEVICES | HA → App | `[{id, entity_id, name, domain, area_id, state, attrs, labels}, …]` — only entities exposed via `BTDASH`/`BTDASHAA` |
| `0x14` | REQ_DASHBOARDS | App → HA | — |
| `0x15` | ANS_DASHBOARDS | HA → App | `[{id, url_path, title, views:[{entity_ids,…}]}, …]` — derived from `DASH_*` labels |
| `0x20` | REQ_STATE | App → HA | `{entity_id}` |
| `0x21` | ANS_STATE | HA → App | `{entity_id, state, attributes, last_changed}` |
| `0x22` | CALL_SERVICE | App → HA | `{domain, service, entity_id, data?}` |
| `0x23` | ANS_CALL_SERVICE | HA → App | `{success, error?}` |
| `0x30` | STATE_CHANGE | HA → App | identical to `ANS_STATE` (server push) |

### BLE chunking

BLE has a 247-byte MTU. Larger packets are split using `[flag u8][chunk …]`: `0x00` = more chunks follow, `0x01` = final chunk.

### Per-transport framing

Each adapter mode wraps the same packet in its own outer frame:

- **ESP32-S3 (USB-Serial Gateway)** — `[length u32 BE][payload]`, 64 KB max. The Pi-side reader has a sliding-window recovery that automatically re-syncs after corruption (e.g. ESP32 boot logs).
- **ESPHome (`ble_server` over WLAN)** — three new native-API messages (IDs 149–151) carry the packet end-to-end. Plaintext or Noise NNpsk0 transport, picked by whether the device YAML defines `api: encryption: key:`. The ESP-side `ble_server` component handles BLE chunking and runs notifications through a one-per-loop-tick queue so 30 KB+ responses don't overrun the GATT TX buffer.
- **Native (Pi BlueZ via bless)** — packets go straight onto the BLE notify characteristic with the same chunking flags and a 20 ms inter-chunk gap.

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

<details>
<summary><b>ESPHome mode: <code>Unexpected preamble 0x01: device may be using noise encryption</code></b></summary>

The ESPHome device has `api: encryption: key: …` set, but no PSK was saved in the integration. Either remove the `encryption:` block from the device YAML and re-flash, or pick the device from the host dropdown — the wizard auto-imports the `noise_psk` from your existing ESPHome integration. Manual entry can paste the base64 key into the optional `noise_psk` field.
</details>

<details>
<summary><b>Native mode: app sees no <code>Homeassistant_Home</code> in nRF Connect</b></summary>

The Pi BT adapter is busy. The wizard's conflict-check step *should* catch this; if you skipped it or another integration grabbed the adapter later, disable HA's Bluetooth integration (Settings → Devices & Services) and reload the entry. `bluetoothctl show` should report `Powered: yes`, `Discoverable: yes` once we've started.
</details>

<details>
<summary><b>App times out on <code>cmd 0x12</code> after labelling many devices</b></summary>

The label-based exposure filter shrinks the response a lot, but a very large `BTDASH` label set can still produce a multi-KB ANS_DEVICES that takes a moment over BLE. The ESPHome path throttles notifications to one per ESPHome loop tick (~16 ms). If timeout persists, verify the connection is actually up (`Connected` banner in the app) and re-tap; second connect inside the same session re-uses the existing CCCD subscription and is usually fast.
</details>

---

## Module map

| File | Purpose |
|------|---------|
| `__init__.py` | Setup entry, ensures `BTDASH`/`BTDASHAA` labels, dispatches by adapter mode |
| `dispatcher.py` | Transport-agnostic packet protocol — command dispatch, label-based exposure filter, state-change push |
| `usb_serial_server.py` | ESP32-S3 over USB-CDC (`PacketDispatcher` subclass) |
| `esphome_server.py` | ESPHome `ble_server` device over WiFi/native API — plaintext + Noise NNpsk0 transports |
| `ble_gatt_server.py` | Native Pi BT via `bless` (`HaBleGattServer` + `NativeBleServer`) |
| `protocol.py` | Frame codec, CRC16-CCITT, sliding-window reader, BLE chunking flags |
| `const.py` | UUIDs, command codes, config keys, label names, `DASH_` prefix |
| `config_flow.py` | UI setup wizard (per-mode branches incl. native conflict-check) |
| `api.py` | HTTP endpoints incl. `/api/bluetooth_api/setup_qr/<entry_id>` |
| `button.py` | "Enable OTA" button (ESP32 mode only) |
| `ble_server_pb2.py` | Generated protobuf bindings for the ESPHome `ble_server` API extensions |

---

## Roadmap

- [x] Native Pi Bluetooth adapter (ESP32-less) via [bless](https://pypi.org/project/bless/) GATT server
- [ ] In-frontend HA card for live monitoring of connected BLE clients (RSSI, last cmd, sync state)
- [ ] Optional BLE encryption (LE Secure Connections) as an alternative to the passcode

---

## Contributing

Pull requests welcome! Please open an issue first for larger changes so the direction can be agreed on.

## License

[MIT](LICENSE) — see `LICENSE.md` in the repo root.
