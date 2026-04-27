"""Constants for the Bluetooth API integration."""

DOMAIN = "bluetooth_api"

# USB Serial (ESP32-S3 gateway)
CONF_USB_PORT = "usb_port"
CONF_USB_PORT_DEFAULT = "/dev/ttyACM0"

# Kept for backward compatibility with existing config entries (not actively used)
CONF_RFCOMM_ENABLED = "rfcomm_enabled"
CONF_RFCOMM_CHANNEL = "rfcomm_channel"
CONF_BLE_ENABLED = "ble_enabled"
