"""
ant_rx.py

Standalone ANT+ Heart Rate receiver for the HR display PC.

Wraps openant's built-in HeartRate device profile, runs the ANT+ node on a
background thread (Node.start() blocks the calling thread), and reports
updates via callbacks so server.py can push them out over WebSocket.

Pairing is two-phase to work around an openant/ANT+ quirk:
  1. A Scanner channel (filtered to device_type=HeartRate) passively listens
     for any HR strap nearby and reports its device_id/trans_type.
  2. Once found, a *separate* HeartRate channel is opened directly against
     that specific device_id.
This avoids openant's wildcard-pairing path, which reassigns the *same*
channel (close -> set_id -> reopen) as soon as a device_id=0 device is
found - that reopen can race the ANT chip's internal state and raise
"CHANNEL_IN_WRONG_STATE". Opening a second, distinct channel for the known
device sidesteps the race entirely. Once found once, the device_id is
cached for the life of the process so later reconnects skip scanning and
pair directly.
"""

import logging
import threading
import time
from typing import Callable, Optional, Tuple

import usb.backend.libusb1

from openant.easy.node import Node
from openant.devices import ANTPLUS_NETWORK_KEY
from openant.devices.common import DeviceType
from openant.devices.heart_rate import HeartRate, HeartRateData
from openant.devices.scanner import Scanner
from openant.base.driver import DriverNotFound

logger = logging.getLogger("ant_rx")

# --- Windows libusb backend fix -------------------------------------------
# On Windows, Zadig only binds a driver to the USB device - it doesn't
# install the libusb-1.0.dll runtime that pyusb (used by openant) needs to
# talk to it, which surfaces as usb.core.NoBackendError: No backend
# available. The libusb-package PyPI package bundles that DLL, so we patch
# pyusb's backend lookup to fall back to the bundled copy instead of making
# people hunt down and manually drop a DLL into System32. This is a no-op
# on platforms (like the Pi) that already have a system libusb.
try:
    import libusb_package

    _original_get_backend = usb.backend.libusb1.get_backend

    def _get_backend_with_bundled_fallback(find_library=None, *args, **kwargs):
        if find_library is None:
            find_library = libusb_package.find_library
        return _original_get_backend(find_library=find_library, *args, **kwargs)

    usb.backend.libusb1.get_backend = _get_backend_with_bundled_fallback
except ImportError:
    # libusb-package not installed - fine on systems with libusb already
    # available system-wide (e.g. Linux/Pi via apt, macOS via brew).
    pass

NETWORK_NUM = 0x00
RETRY_SECONDS = 5


class AntHrReceiver:
    """
    Runs an ANT+ node on a background thread and listens for a heart rate
    strap. Call start() once; on_hr_update(bpm: int) fires on every new
    reading, on_status(msg: str) fires on connection state changes.
    """

    def __init__(
        self,
        on_hr_update: Callable[[int], None],
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self.on_hr_update = on_hr_update
        self.on_status = on_status or (lambda msg: None)

        self._node: Optional[Node] = None
        self._scanner: Optional[Scanner] = None
        self._hr_device: Optional[HeartRate] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Cached (device_id, trans_type) from a previous successful scan,
        # so reconnects within this process's lifetime can skip straight to
        # direct pairing instead of scanning again.
        self._known_device: Optional[Tuple[int, int]] = None

    def start(self) -> None:
        """Start listening on a background thread. Safe to call once."""
        self._thread = threading.Thread(
            target=self._run_forever, name="ant-rx-thread", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the receiver and release the ANT+ USB dongle."""
        self._stop_event.set()
        self._teardown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- internals ---------------------------------------------------------

    def _run_forever(self) -> None:
        """
        Keeps trying to open the dongle and listen. If the dongle is
        unplugged, not yet driver-swapped (see README), or the strap goes
        quiet at the OS/USB level, this retries instead of dying silently.
        """
        while not self._stop_event.is_set():
            try:
                self._connect_and_listen()
            except DriverNotFound:
                self.on_status("ANT+ dongle not found - retrying")
                logger.warning("ANT+ dongle not found, retrying in %ss", RETRY_SECONDS)
            except Exception as exc:  # noqa: BLE001 - deliberately broad, this is a retry loop
                self.on_status(f"ANT+ error - retrying ({exc})")
                logger.exception("ANT+ RX error, retrying in %ss", RETRY_SECONDS)
            finally:
                self._teardown()

            if not self._stop_event.is_set():
                time.sleep(RETRY_SECONDS)

    def _connect_and_listen(self) -> None:
        self._node = Node()
        self._node.set_network_key(NETWORK_NUM, ANTPLUS_NETWORK_KEY)

        if self._known_device is not None:
            device_id, trans_type = self._known_device
            logger.info(
                "Pairing directly with previously-found strap (device_id=%s)",
                device_id,
            )
            self.on_status("connecting to known HR strap")
            self._open_hr_channel(device_id, trans_type)
        else:
            self.on_status("scanning for HR strap")
            self._scanner = Scanner(
                self._node, device_id=0, device_type=DeviceType.HeartRate.value
            )

            def on_scanner_found(tuple_device):
                device_id, _device_type, trans_type = tuple_device
                logger.info(
                    "HR strap discovered via scan (device_id=%s, trans_type=%s)",
                    device_id,
                    trans_type,
                )
                self._known_device = (device_id, trans_type)
                self._open_hr_channel(device_id, trans_type)

            self._scanner.on_found = on_scanner_found

        logger.info("ANT+ node started")
        self._node.start()  # blocks this thread until self._node.stop()

    def _open_hr_channel(self, device_id: int, trans_type: int) -> None:
        """
        Opens a dedicated HeartRate channel for a known device_id. Because
        device_id != 0, openant's wildcard-pairing close/reopen dance never
        runs - this just opens once and starts receiving.
        """
        self._hr_device = HeartRate(self._node, device_id=device_id, trans_type=trans_type)

        def on_device_data(page, page_name, data: HeartRateData):
            if page_name == "heart_rate" and data.heart_rate:
                self.on_hr_update(data.heart_rate)

        self._hr_device.on_device_data = on_device_data
        self.on_status("connected")

    def _teardown(self) -> None:
        if self._hr_device is not None:
            try:
                self._hr_device.close_channel()
            except Exception:
                pass
            self._hr_device = None
        if self._scanner is not None:
            try:
                self._scanner.close_channel()
            except Exception:
                pass
            self._scanner = None
        if self._node is not None:
            try:
                self._node.stop()
            except Exception:
                pass
            self._node = None


if __name__ == "__main__":
    """
    Standalone console test - no FastAPI/WebSocket involved. Run this
    directly to validate ANT+ pairing against real hardware:

        python ant_rx.py

    Prints status changes and every HR reading to the console. Ctrl+C to
    stop.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    def _print_hr(bpm: int) -> None:
        print(f"HR: {bpm} bpm")

    def _print_status(msg: str) -> None:
        print(f"STATUS: {msg}")

    receiver = AntHrReceiver(on_hr_update=_print_hr, on_status=_print_status)
    receiver.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
        receiver.stop()
