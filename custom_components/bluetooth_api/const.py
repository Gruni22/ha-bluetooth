"""Constants for the Bluetooth API integration."""

DOMAIN = "bluetooth_api"

# Adapter mode
CONF_ADAPTER_MODE = "adapter_mode"
ADAPTER_MODE_NATIVE = "native"   # HA uses its own BT adapter (bless GATT server)
ADAPTER_MODE_ESP32 = "esp32"     # HA communicates via ESP32 over USB-Serial

# Native BT adapter identifier (e.g. "hci0" or MAC address)
CONF_BT_ADAPTER = "bt_adapter"

# USB Serial (ESP32-S3 gateway, only used when adapter_mode == "esp32")
CONF_USB_PORT = "usb_port"
CONF_USB_PORT_DEFAULT = "/dev/ttyACM0"

# Passcode (32-bit uint, generated once during setup, included in every packet)
CONF_PASSCODE = "passcode"

# BLE device name advertised
CONF_DEVICE_NAME = "device_name"
CONF_DEVICE_NAME_DEFAULT = "Homeassistant_Home"

# BLE Service & Characteristic UUIDs (identical to ESP32 firmware)
BLE_SERVICE_UUID = "a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1234"
BLE_TX_UUID      = "a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1235"  # HA → App (Notify)
BLE_RX_UUID      = "a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1236"  # App → HA (Write)

# Packet magic bytes
PKT_HEADER = b"\xaa\xbb"
PKT_END    = b"\xcc\xdd"

# Command codes (1 byte each)
CMD_ACK             = 0x01
CMD_NACK            = 0x02
CMD_REQ_AREAS       = 0x10
CMD_ANS_AREAS       = 0x11
CMD_REQ_DEVICES     = 0x12  # payload: {"area_id": "..." | null}
CMD_ANS_DEVICES     = 0x13
CMD_REQ_DASHBOARDS  = 0x14
CMD_ANS_DASHBOARDS  = 0x15
CMD_REQ_STATE       = 0x20  # payload: {"entity_id": "..."}
CMD_ANS_STATE       = 0x21
CMD_CALL_SERVICE    = 0x22  # payload: {domain, service, entity_id, data?}
CMD_ANS_CALL_SERVICE = 0x23
CMD_STATE_CHANGE    = 0x30  # server push, same structure as ANS_STATE

# Kept for backward compatibility only (not actively used)
CONF_RFCOMM_ENABLED = "rfcomm_enabled"
CONF_RFCOMM_CHANNEL = "rfcomm_channel"
CONF_BLE_ENABLED = "ble_enabled"
