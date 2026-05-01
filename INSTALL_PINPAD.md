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
