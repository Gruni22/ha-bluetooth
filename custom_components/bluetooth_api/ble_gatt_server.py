"""Native BLE GATT server using the bless library.

Implements the same BLE service/characteristic UUIDs as the ESP32 firmware so
the btdashboard app can connect to HA directly when a native BT adapter is present.

Security model: Open BLE (no pairing/bonding). Every packet contains the
shared passcode which is validated by the protocol layer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from homeassistant.core import HomeAssistant

from .const import BLE_RX_UUID, BLE_SERVICE_UUID, BLE_TX_UUID
from .dispatcher import PacketDispatcher
from .protocol import BLE_CHUNK_CONTINUES, BLE_CHUNK_FINAL, BLE_MAX_PAYLOAD

_LOGGER = logging.getLogger(__name__)

OnFrameCallback = Callable[[bytes], None]
OnConnectCallback = Callable[[], None]
OnDisconnectCallback = Callable[[], None]

_CHUNK_SIZE = 244  # BLE_MAX_CHUNK from ESP32 firmware (MTU 247 – 3 ATT header)
_LEAK_NOTIFICATION_ID = "bluetooth_api_bluez_leak"


class HaBleGattServer:
    """BLE GATT peripheral server.

    Uses the *bless* library (https://github.com/kevincar/bless) which wraps
    BlueZ D-Bus on Linux and CoreBluetooth/WinRT on other platforms.
    """

    def __init__(
        self,
        device_name: str,
        adapter: str | None,
        on_frame: OnFrameCallback,
        on_connect: OnConnectCallback,
        on_disconnect: OnDisconnectCallback,
    ) -> None:
        self._device_name = device_name
        self._adapter = adapter
        self._on_frame = on_frame
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

        self._server = None  # bless.BlessServer instance
        self._rx_buf: bytearray = bytearray()
        self._connected = False
        self._running = False
        self._task: asyncio.Task | None = None
        # Queue for outgoing chunks to avoid concurrent writes
        self._tx_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        # Populated by _configure_via_dbus after the adapter is read. List of
        # BlueZ-registered service UUIDs that are likely to trigger pairing on
        # Android (LE-Audio, HID, A2DP/HSP/HFP, etc.). Empty = clean adapter.
        self.leaking_uuids: list[str] = []
        # Set after _configure_adapter() returns. NativeBleServer awaits this
        # before reading `leaking_uuids` so the check sees post-config state.
        self.configure_done: asyncio.Event = asyncio.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start advertising and the TX worker."""
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        """Stop advertising and clean up."""
        self._running = False
        await self._tx_queue.put(None)  # stop TX worker
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._server is not None:
            try:
                await self._server.stop()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    async def send_frame(self, data: bytes) -> bool:
        """Enqueue *data* as BLE chunks to the connected client.

        Returns False if the queue is full or no client is connected.
        """
        if not self._connected or self._server is None:
            return False
        chunks = _encode_chunks(data)
        for chunk in chunks:
            try:
                self._tx_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                _LOGGER.warning("BLE TX queue full — dropping %d-byte frame", len(data))
                return False
        return True

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Set up the GATT server and start advertising."""
        try:
            from bless import BlessServer, BlessGATTCharacteristic  # type: ignore[import]
            from bless.backends.characteristic import GATTCharacteristicProperties, GATTAttributePermissions  # type: ignore[import]
        except ImportError:
            _LOGGER.error(
                "bless library not installed. Install it via the integration requirements."
            )
            return

        loop = asyncio.get_event_loop()

        try:
            self._server = BlessServer(name=self._device_name, loop=loop)
            self._server.read_request_func = self._handle_read
            self._server.write_request_func = self._handle_write

            # Add HA service
            await self._server.add_new_service(BLE_SERVICE_UUID)

            # TX characteristic: Notify (HA → App)
            tx_props = (
                GATTCharacteristicProperties.notify
            )
            tx_perms = GATTAttributePermissions.readable
            await self._server.add_new_characteristic(
                BLE_SERVICE_UUID, BLE_TX_UUID, tx_props, None, tx_perms
            )

            # RX characteristic: Write (App → HA)
            rx_props = (
                GATTCharacteristicProperties.write
                | GATTCharacteristicProperties.write_without_response
            )
            rx_perms = GATTAttributePermissions.writeable
            await self._server.add_new_characteristic(
                BLE_SERVICE_UUID, BLE_RX_UUID, rx_props, None, rx_perms
            )

            await self._server.start()
            _LOGGER.info(
                "BLE GATT server started: advertising as '%s' (service %s)",
                self._device_name,
                BLE_SERVICE_UUID,
            )

            # Configure adapter for Open BLE — no SMP Security Requests.
            await self._configure_adapter()

            # Start TX worker and disconnect detector in parallel
            await asyncio.gather(
                self._tx_worker(),
                self._disconnect_detector(),
            )

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("BLE GATT server error: %s", exc)

    async def _configure_adapter(self) -> None:
        """Configure BlueZ for Open BLE — no SMP Security Requests, no pairing dialogs.

        Root cause of pairing dialogs: when BlueZ has a stored bond (LTK) for the
        phone, or when `Pairable=true`, BlueZ sends an SMP Security Request on
        every new LE connection. Android then shows a pairing dialog which we
        cannot auto-confirm without `BLUETOOTH_PRIVILEGED` (`SecurityException`
        on API 36).

        Three-part hardening, applied in order:
          1. Set `Pairable=false` and `Discoverable=true` on the adapter via the
             BlueZ D-Bus interface (org.bluez.Adapter1). D-Bus because (a) bless
             already speaks it, so the dependency is free, (b) `bluetoothctl`
             may not even be in PATH inside HAOS-style containers, and (c) we
             can actually log what worked.
          2. Walk every `org.bluez.Device1` under our adapter and call
             `RemoveDevice` on the disconnected ones. This wipes cached LTKs so
             BlueZ has no reason to re-encrypt on reconnect.
          3. Fall back to `bluetoothctl` only if D-Bus is unavailable — and
             this time actually capture stderr so failures surface in the log.

        Result: link layer unencrypted, no Pi-side bond, authentication lives
        one layer up in the 32-bit passcode each packet carries.
        """
        try:
            ok = await self._configure_via_dbus()
            if not ok:
                _LOGGER.warning("BLE: D-Bus path failed, falling back to bluetoothctl")
                await self._configure_via_bluetoothctl()
        finally:
            # Signal NativeBleServer that the adapter check is done, regardless
            # of which path ran. Without this it would block forever waiting.
            self.configure_done.set()

    async def _configure_via_dbus(self) -> bool:
        """Set Pairable=false, Discoverable=true, and clear LTK cache via D-Bus.

        Returns True if the adapter was configured successfully. Bless ships
        with one of {dbus_fast, dbus_next} as a Linux dependency — we try
        dbus_fast first (newer, faster) and fall back to dbus_next.
        """
        adapter_path = self._adapter_dbus_path()
        try:
            from dbus_fast import BusType, Variant
            from dbus_fast.aio import MessageBus
        except ImportError:
            try:
                from dbus_next import BusType, Variant  # type: ignore[no-redef]
                from dbus_next.aio import MessageBus  # type: ignore[no-redef]
            except ImportError:
                _LOGGER.debug("BLE: no D-Bus library available")
                return False

        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("BLE: D-Bus system bus connect failed: %s", exc)
            return False

        try:
            adapter_intro = await bus.introspect("org.bluez", adapter_path)
            adapter_obj = bus.get_proxy_object("org.bluez", adapter_path, adapter_intro)
            adapter_props = adapter_obj.get_interface("org.freedesktop.DBus.Properties")

            await adapter_props.call_set(
                "org.bluez.Adapter1", "Pairable", Variant("b", False)
            )
            await adapter_props.call_set(
                "org.bluez.Adapter1", "Discoverable", Variant("b", True)
            )
            _LOGGER.info(
                "BLE: D-Bus set Pairable=false, Discoverable=true on %s",
                adapter_path,
            )

            # Inspect what UUIDs BlueZ has loaded on this adapter. Anything
            # that triggers Android-side authentication during GATT discovery
            # gets flagged so NativeBleServer can warn the user.
            try:
                uuids_v = await adapter_props.call_get("org.bluez.Adapter1", "UUIDs")
                self.leaking_uuids = _classify_leaking_uuids(uuids_v.value)
                if self.leaking_uuids:
                    _LOGGER.warning(
                        "BLE: BlueZ adapter exposes %d service(s) likely to trigger Android pairing: %s",
                        len(self.leaking_uuids),
                        ", ".join(self.leaking_uuids),
                    )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("BLE: could not read Adapter1.UUIDs: %s", exc)

            # ── Clear stale LTKs by removing every known disconnected device.
            adapter_iface = adapter_obj.get_interface("org.bluez.Adapter1")
            om_intro = await bus.introspect("org.bluez", "/")
            om_obj = bus.get_proxy_object("org.bluez", "/", om_intro)
            om = om_obj.get_interface("org.freedesktop.DBus.ObjectManager")
            objects = await om.call_get_managed_objects()

            adapter_prefix = adapter_path + "/"
            removed = 0
            for path, ifaces in objects.items():
                if not path.startswith(adapter_prefix):
                    continue
                if "org.bluez.Device1" not in ifaces:
                    continue
                connected_v = ifaces["org.bluez.Device1"].get("Connected")
                connected = bool(connected_v.value) if connected_v is not None else False
                if connected:
                    continue  # don't yank an active session
                try:
                    await adapter_iface.call_remove_device(path)
                    removed += 1
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("BLE: RemoveDevice(%s) failed: %s", path, exc)
            if removed:
                _LOGGER.info("BLE: removed %d cached device(s) (LTKs cleared)", removed)
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("BLE: D-Bus adapter config failed: %s", exc)
            return False
        finally:
            try:
                bus.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _adapter_dbus_path(self) -> str:
        """Resolve `self._adapter` ("hci0" / None) to a BlueZ object path."""
        adapter = self._adapter or "hci0"
        if adapter.startswith("/org/bluez/"):
            return adapter
        return f"/org/bluez/{adapter}"

    async def _configure_via_bluetoothctl(self) -> None:
        """Fallback path. Logs stdout/stderr so silent failures stop being silent."""
        for cmd in (
            ("bluetoothctl", "pairable", "off"),
            ("bluetoothctl", "agent", "NoInputNoOutput"),
            ("bluetoothctl", "default-agent"),
        ):
            await self._run_cmd_logged(*cmd)

    async def _run_cmd_logged(self, *args: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode != 0:
                _LOGGER.warning(
                    "BLE: %s exited %d — stderr=%s",
                    " ".join(args),
                    proc.returncode,
                    stderr.decode(errors="replace").strip()[:200],
                )
            else:
                _LOGGER.debug("BLE: %s ok", " ".join(args))
        except FileNotFoundError:
            _LOGGER.warning("BLE: %s not found in PATH — install bluez tools or rely on D-Bus", args[0])
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("BLE: command %s failed: %s", args, exc)

    async def _disconnect_detector(self) -> None:
        """Poll bluetoothctl every 5 s to detect client disconnection.

        Does NOT clear the device cache — bonds are wiped once on startup and pairable=off
        prevents new ones, so periodic clearing would only interfere.
        """
        prev_connected: set[str] = set()
        while self._running:
            await asyncio.sleep(5)
            if not self._running:
                break

            connected_macs: set[str] = set()
            try:
                proc = await asyncio.create_subprocess_exec(
                    "bluetoothctl", "devices", "Connected",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                for line in out.decode().splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[0] == "Device":
                        connected_macs.add(parts[1])
            except Exception:  # noqa: BLE001
                pass

            # Detect client disconnect: previously had connected devices, now none
            if prev_connected and not connected_macs and self._connected:
                _LOGGER.info("BLE: client disconnected (detected via bluetoothctl)")
                self._mark_disconnected()

            prev_connected = connected_macs

    async def _tx_worker(self) -> None:
        """Send queued BLE chunks to the subscribed client."""
        while self._running:
            chunk = await self._tx_queue.get()
            if chunk is None:
                break
            if self._server is None or not self._connected:
                continue
            try:
                char = self._server.get_characteristic(BLE_TX_UUID)
                if char is None:
                    continue
                char.value = bytearray(chunk)
                _LOGGER.debug("BLE TX: calling update_value (%d bytes)", len(chunk))
                result = self._server.update_value(BLE_SERVICE_UUID, BLE_TX_UUID)
                _LOGGER.debug("BLE TX: update_value returned %s", result)
                if not result and self._connected:
                    # No subscribers — client disconnected
                    _LOGGER.info("BLE: client disconnected (no subscribers)")
                    self._mark_disconnected()
                await asyncio.sleep(0.02)  # 20 ms between chunks (> 15 ms connection interval)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("BLE TX error: %s", exc)

    def _mark_disconnected(self) -> None:
        """Flip the connected flag *and* fire the on_disconnect callback.

        Both code paths that detect a drop (no-subscriber TX failure, BlueZ
        polling) used to only flip `_connected` — leaving the upstream
        dispatcher subscribed to state-changes for a phantom client.
        """
        if not self._connected:
            return
        self._connected = False
        # Drain any pending TX so the next client doesn't get bytes for the
        # previous one's last in-flight request.
        try:
            while True:
                self._tx_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        loop = getattr(self, "_loop", None)
        if loop is not None:
            loop.call_soon_threadsafe(self._on_disconnect)
        else:
            try:
                self._on_disconnect()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("BLE: on_disconnect callback raised", exc_info=True)

    def _handle_read(self, characteristic: object, **kwargs: object) -> bytearray:  # noqa: ARG002
        return bytearray()

    def _handle_write(self, characteristic: object, value: bytearray, **kwargs: object) -> None:  # noqa: ARG002
        """Receive a BLE write; reassemble chunks into complete frames."""
        data = bytes(value)
        _LOGGER.debug("BLE write received: %d bytes, flag=0x%02x", len(data), data[0] if data else -1)
        if not data:
            return

        flag = data[0]
        payload = data[1:]
        self._rx_buf.extend(payload)

        if flag == BLE_CHUNK_FINAL:
            frame = bytes(self._rx_buf)
            self._rx_buf.clear()
            loop = getattr(self, "_loop", None)
            if loop is None:
                _LOGGER.warning("BLE: _loop not set, dropping frame")
                return
            if not self._connected:
                self._connected = True
                _LOGGER.info("BLE client connected (first write)")
                loop.call_soon_threadsafe(self._on_connect)
            _LOGGER.debug("BLE: dispatching frame (%d bytes) via loop", len(frame))
            loop.call_soon_threadsafe(self._on_frame, frame)


# UUIDs whose services typically have at least one characteristic with the SMP
# encryption-required flag set. When BlueZ has these registered (because the
# `audio` / `input` plugins are loaded) Android walks them during GATT
# discovery, sees the flag, and pops the pairing dialog — even though our own
# service has no auth requirements. We can't unload these from a non-host
# context (HAOS sandboxing), so we flag them so the user can fix it once on
# the host. UUID strings are lower-case to match BlueZ's reporting.
_LEAKING_UUID_TABLE: dict[str, str] = {
    # Classic profiles (SDP) — present on the same controller and listed in
    # bluetoothctl, also surface as device-class hints to Android.
    "00001108-0000-1000-8000-00805f9b34fb": "Headset (HSP)",
    "0000110a-0000-1000-8000-00805f9b34fb": "A2DP Audio Source",
    "0000110b-0000-1000-8000-00805f9b34fb": "A2DP Audio Sink",
    "0000110c-0000-1000-8000-00805f9b34fb": "AVRCP Target",
    "0000110e-0000-1000-8000-00805f9b34fb": "AVRCP Controller",
    "00001112-0000-1000-8000-00805f9b34fb": "Headset Audio Gateway",
    "0000111e-0000-1000-8000-00805f9b34fb": "Hands-Free (HFP)",
    "0000111f-0000-1000-8000-00805f9b34fb": "Hands-Free Audio Gateway",
    "00001124-0000-1000-8000-00805f9b34fb": "HID over Bluetooth",
    "00001200-0000-1000-8000-00805f9b34fb": "PnP Information (DI)",
    # LE Audio / GATT — these *are* on the BLE side and the actual primary
    # cause of pairing prompts during BLE GATT discovery.
    "00001843-0000-1000-8000-00805f9b34fb": "Audio Input Control (LE)",
    "00001844-0000-1000-8000-00805f9b34fb": "Volume Control (LE)",
    "00001845-0000-1000-8000-00805f9b34fb": "Volume Offset Control (LE)",
    "0000184d-0000-1000-8000-00805f9b34fb": "Microphone Control (LE)",
    "0000184f-0000-1000-8000-00805f9b34fb": "Broadcast Audio Scan (LE)",
}


def _classify_leaking_uuids(adapter_uuids: list[str]) -> list[str]:
    """Return human-readable names for UUIDs that match the leak table."""
    return [
        _LEAKING_UUID_TABLE[u.lower()]
        for u in adapter_uuids
        if u.lower() in _LEAKING_UUID_TABLE
    ]


def _encode_chunks(data: bytes) -> list[bytes]:
    """Split *data* into BLE chunks (flag byte + payload)."""
    chunks: list[bytes] = []
    offset = 0
    while offset < len(data):
        end = min(offset + _CHUNK_SIZE, len(data))
        flag = BLE_CHUNK_FINAL if end == len(data) else BLE_CHUNK_CONTINUES
        chunks.append(bytes([flag]) + data[offset:end])
        offset = end
    return chunks


# ── NativeBleServer ──────────────────────────────────────────────────────────


class NativeBleServer(PacketDispatcher):
    """Native Pi BT bridge: wraps `HaBleGattServer` to fit the dispatcher pattern.

    Same shape as `EsphomeApiServer` and `UsbSerialServer` — the integration
    `__init__.py` calls `start()` / `stop()`, the dispatcher protocol layer
    talks application packets, this class handles BLE chunking + GATT.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        device_name: str,
        passcode: int = 0,
        adapter: str | None = None,
    ) -> None:
        super().__init__(hass, passcode)
        self._gatt = HaBleGattServer(
            device_name=device_name,
            adapter=adapter,
            on_frame=self._on_frame_from_gatt,
            on_connect=self._on_ble_client_connected,
            on_disconnect=self._on_ble_client_disconnected,
        )

    async def start(self) -> None:
        await self._gatt.start()
        _LOGGER.info("Native BLE GATT server starting (%s)", self._gatt._device_name)
        # Don't block setup on the leak-check; spin it off so failed/slow
        # D-Bus calls can't hold up the rest of the integration.
        asyncio.ensure_future(self._post_leak_notification())

    async def _post_leak_notification(self) -> None:
        """Wait for the BlueZ adapter check, then notify the user if BlueZ has
        loaded plugins (audio/HID/LE-Audio) that will pull Android into a
        pairing dialog during GATT discovery.

        We can't fix this from inside the HA Core container (HAOS sandbox), so
        the notification carries the exact host-shell command the user needs
        to run once over the HAOS host SSH (port 22222).
        """
        try:
            await asyncio.wait_for(self._gatt.configure_done.wait(), timeout=20)
        except asyncio.TimeoutError:
            return
        leaks = self._gatt.leaking_uuids
        if not leaks:
            # Already clean — clear any old notification from a previous run.
            await self._hass.services.async_call(
                "persistent_notification", "dismiss",
                {"notification_id": _LEAK_NOTIFICATION_ID},
                blocking=False,
            )
            return

        bullet_list = "\n".join(f"- {name}" for name in leaks)
        message = (
            "Native BT mode is up, but BlueZ on this Pi has extra services "
            "loaded that will trigger an Android pairing dialog during GATT "
            "discovery:\n\n"
            f"{bullet_list}\n\n"
            "**Fix on the HAOS host (port 22222 SSH or HDMI console):**\n\n"
            "```sh\n"
            "sed -i '/^\\[General\\]$/a DisablePlugins = audio,input,sap' "
            "/etc/bluetooth/main.conf\n"
            "systemctl restart bluetooth\n"
            "```\n\n"
            "Reload the Bluetooth API integration afterwards. This "
            "notification disappears automatically once the adapter is clean."
        )
        await self._hass.services.async_call(
            "persistent_notification", "create",
            {
                "notification_id": _LEAK_NOTIFICATION_ID,
                "title": "Bluetooth API: BlueZ plugin conflict",
                "message": message,
            },
            blocking=False,
        )

    async def stop(self) -> None:
        # Tell the dispatcher first so its state-change subscription is torn
        # down before bless rips out the GATT stack underneath us.
        self._on_ble_client_disconnected()
        await self._gatt.stop()

    async def _send_raw(self, data: bytes) -> None:
        # `send_frame` enqueues; the GATT TX worker drains with built-in
        # 20 ms throttling, same flow-control story as the ESPHome path.
        await self._gatt.send_frame(data)

    def _on_frame_from_gatt(self, frame: bytes) -> None:
        # `HaBleGattServer` invokes this on the HA loop (via
        # call_soon_threadsafe). We're sync here but the dispatcher's
        # incoming-packet handler is async, so schedule it.
        asyncio.ensure_future(self._handle_incoming_packet(frame))
