# DatecsPay pinpad — native library installation

The DatecsPay BluePad-50 / BlueCash-50 driver in
`odoo_erpnet_fp/drivers/pinpad/datecs_pay/` wraps a **proprietary C
library** (`libdatecs_pinpad.so`) via `ctypes`. The library is **not
distributed with this repo** for licensing reasons; it must be built
from its own (closed-source) source tree and dropped into:

    odoo_erpnet_fp/drivers/pinpad/datecs_pay/lib/libdatecs_pinpad.so

## Without the .so

The Python wrapper (`_native.py`) detects the missing library at import
time and defers the failure: importing the package and running the rest
of the proxy works fine. Calls to `DatecsPayPinpad.open()` (or any
method that touches the device) will raise:

    RuntimeError: libdatecs_pinpad.so could not be loaded: ...

The pinpad endpoints `/pinpads/{id}/...` will respond with `ok: false`
+ that error message. Other registries (printers, scales, readers) are
unaffected.

## Building the .so

Source tree (private — request access from
[@rosenvladimirov](https://github.com/rosenvladimirov)):

    /path/to/datecs_pinpad_driver/
    ├── datecs_pinpad_driver.c
    ├── datecs_pinpad_driver.h
    ├── Makefile
    └── ...

Build:

```bash
cd /path/to/datecs_pinpad_driver
make            # produces libdatecs_pinpad.so + libdatecs_pinpad.a
```

Install into this proxy:

```bash
cp libdatecs_pinpad.so \
   /path/to/Odoo.ErpNet.FP/odoo_erpnet_fp/drivers/pinpad/datecs_pay/lib/
```

Or system-wide (the wrapper falls back to `ctypes.CDLL('libdatecs_pinpad.so')`
on the system library path):

```bash
sudo cp libdatecs_pinpad.so /usr/local/lib/
sudo ldconfig
```

## Docker

Build the proxy Docker image after copying the .so into the source
tree — the image bundles whatever is in `odoo_erpnet_fp/`:

```bash
cp /path/to/libdatecs_pinpad.so \
   odoo_erpnet_fp/drivers/pinpad/datecs_pay/lib/
docker compose up -d --build
```

`*.so` is in `.gitignore`, so the file stays on your deployment host
without risk of accidental publication.

## TCP transport — the BlueCash pinpad bridge

The library also opens `tcp://host:port` directly — the
`PinpadBridgeService` of the BlueCash client app (port 9101). No socat
PTY is needed any more:

```yaml
pinpads:
  - id: bluepad
    driver: datecs_pay
    port: "tcp://192.168.0.101:9101"
```

The bridge forwards the pinpad bytes 1:1, so the protocol is the same as
over a serial port. `get_info` / `get_status` time out while the DatecsPay
app on the device is not on its idle ECR screen — that is the device,
not the transport.

## Windows

The Windows build is `datecs_pinpad.dll`, **TCP only** (a serial path
returns an error). Build it from the same source tree with mingw-w64:

```bash
make windows          # local x86_64-w64-mingw32-gcc
make windows-docker   # same, inside a Debian container
```

and drop it next to the .so:

    odoo_erpnet_fp/drivers/pinpad/datecs_pay/lib/datecs_pinpad.dll

`_native.py` loads the .dll on Windows and the .so elsewhere; the
Windows installer picks it up through `package-data` like the .so.
`*.dll` is in `.gitignore` too.

⚠️ **Smart App Control.** Windows 11 with Smart App Control on blocks an
unsigned DLL that has no reputation with Microsoft:
`WinError 4551: An Application Control policy has blocked this file`.
The DLL has to be Authenticode-signed for such machines.
