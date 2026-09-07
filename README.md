# HR Display - Windows setup

Standalone ANT+ heart rate display for the second PC. Plugs into its own
ANT+ dongle, pairs with the HR strap (wildcard - no pairing step needed),
and shows a big BPM number full-screen in a browser on that PC's monitor.

## 1. Swap the USB driver (one-time, Zadig)

Windows will auto-attach its default driver to the ANT USB2 stick, which
`openant`/`libusb` can't use. Fix once:

1. Plug in the **second** ANT+ dongle (not the one going to the Pi).
2. Download Zadig: https://zadig.akeo.ie/
3. Run Zadig, then in **Options -> List All Devices**, enable that view.
4. Select the ANT USB stick (`0fcf:1008`, usually shows as "Dynastream
   ANT USB Stick" or similar).
5. In the driver dropdown, choose **libusb-win32** (or WinUSB).
6. Click **Replace Driver** / **Install Driver**.

If Device Manager later shows the stick under "libusb-win32 devices" (or
"Universal Serial Bus devices" as WinUSB), the swap worked.

**Note:** the driver swap alone isn't enough - Windows still needs the
actual `libusb-1.0.dll` runtime, which Zadig doesn't install. `ant_rx.py`
handles this automatically via the `libusb-package` dependency below, so
you shouldn't need to manually download or place any DLL yourself. If you
still see `usb.core.NoBackendError: No backend available` after installing
requirements, double check `libusb-package` installed correctly (see
Troubleshooting below).

## 2. Install Python deps

From this folder in PowerShell:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Run it

```powershell
venv\Scripts\activate
uvicorn server:app --host 0.0.0.0 --port 8000
```

Then open **http://localhost:8000** in a browser on this PC and go
full-screen (F11). The page shows "searching for HR strap..." until it
locks onto the strap, then switches to the live BPM number.

If the dongle isn't plugged in or the driver swap didn't take, the page
will show "ANT+ dongle not found - retrying" and keep retrying every 5
seconds - no need to restart the server once you plug it in / fix the
driver.

## Files

- `ant_rx.py` - ANT+ HR receiver (openant), runs on a background thread
- `server.py` - FastAPI app: serves the page, bridges HR updates to
  connected browsers over WebSocket
- `static/index.html` - the display itself
- `requirements.txt` - Python deps

## Troubleshooting

**`usb.core.NoBackendError: No backend available`** - this is different
from "dongle not found." It means pyusb can't locate the libusb-1.0.dll
runtime at all (Zadig only binds the driver, it doesn't install this).
`ant_rx.py` patches pyusb to fall back to the DLL bundled by the
`libusb-package` pip package, so this should be resolved automatically as
long as `pip install -r requirements.txt` succeeded. If it still happens:
- Confirm `libusb-package` actually installed: `pip show libusb-package`
- Try `pip install --force-reinstall libusb-package`
- As a last resort, manually download libusb-1.0.dll (64-bit) from
  https://github.com/libusb/libusb/releases and copy it to
  `C:\Windows\System32`

**`ANT+ dongle not found - retrying`** (a `DriverNotFound` message, not
`NoBackendError`) - this means the backend loaded fine but no ANT USB
stick was detected at all. Check it's plugged in and Device Manager shows
it under libusb-win32/WinUSB as described in step 1.

## Notes

- Pairing is wildcard (`device_id=0`), so it locks onto whichever ANT+ HR
  strap it hears first. If you're ever running two straps near each other,
  that's worth knowing.
- The page dims the number and shows "signal lost" if no update has come
  in for 6 seconds - useful for telling a dropped/poor strap contact apart
  from a genuinely low reading.
- To run this automatically on boot, the simplest option is a shortcut to
  a `.bat` file (activate venv + run uvicorn) in the Windows Startup
  folder - happy to put that together if you want it to launch
  unattended.
