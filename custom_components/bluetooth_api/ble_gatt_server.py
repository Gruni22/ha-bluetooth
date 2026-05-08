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

    async def _run_cmd(self, *args: str) -> None:
        """Run a shell command, suppressing all output."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("BLE: command %s failed: %s", args, exc)

    async def _configure_adapter(self) -> None:
        """Configure BlueZ for Open BLE — no SMP Security Requests, no pairing dialogs.

        Root cause of pairing dialogs: when BlueZ has a stored bond (LTK) for the phone,
        or when pairable=on, it sends an SMP Security Request on every new LE connection,
        causing Android to show a pairing dialog (which we cannot auto-confirm without
        BLUETOOTH_PRIVILEGED — confirmed SecurityException on API 36).

        Two-part fix:
        1. Clear all cached devices (removes any stored LTKs) so BlueZ has no reason
           to initiate SMP re-encryption on reconnect.
        2. Set pairable=off so BlueZ does not send SMP Security Request for new devices.

        Result: connections are pure Open BLE — unencrypted at link layer, secured at the
        application layer via the 32-bit passcode present in every packet.
        """
        await self._clear_bluez_device_cache()
        await self._run_cmd("bluetoothctl", "pairable", "off")
        await self._run_cmd("bluetoothctl", "agent", "NoInputNoOutput")
        await self._run_cmd("bluetoothctl", "default-agent")
        _LOGGER.debug("BLE: adapter configured (pairable=off, LTKs cleared)")

    async def _clear_bluez_device_cache(self) -> None:
        """Remove all non-connected devices from BlueZ to prevent stale SMP keys."""
        try:
            # Collect all known devices
            proc = await asyncio.create_subprocess_exec(
                "bluetoothctl", "devices",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            all_macs: list[str] = []
            for line in stdout.decode().splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0] == "Device":
                    all_macs.append(parts[1])

            # Collect currently-connected devices (skip removal to avoid disconnecting)
            connected_macs: set[str] = set()
            try:
                proc2 = await asyncio.create_subprocess_exec(
                    "bluetoothctl", "devices", "Connected",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5.0)
                for line in out2.decode().splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[0] == "Device":
                        connected_macs.add(parts[1])
            except Exception:  # noqa: BLE001
                pass  # older BlueZ may not support "devices Connected"

            for mac in all_macs:
                if mac not in connected_macs:
                    await self._run_cmd("bluetoothctl", "remove", mac)
                    _LOGGER.debug("BLE: removed cached device %s", mac)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("BLE: could not clear device cache: %s", exc)

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
