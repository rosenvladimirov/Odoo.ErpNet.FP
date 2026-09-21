#!/usr/bin/env bash
# Build the Windows installer for Odoo.ErpNet.FP (the proxy service).
#
# Strategy: Docker container stages everything under build/win/, then
# makensis bundles it into dist/erpnet-fp-server-<version>-setup.exe.
#
# What the resulting setup.exe contains:
#   build/win/python/        Python 3.12 embeddable (CPython 3.12.x x64)
#   build/win/wheels/        Pre-downloaded Windows wheels for all deps
#   build/win/server/        Our source tree (odoo_erpnet_fp/)
#   build/win/config/        Default config-windows.example.yaml
#   build/win/static/        Dashboard HTML/CSS/JS (already inside source)
#
# What the installer does on the target machine:
#   1. Lays the above out under C:\Program Files\Odoo.ErpNet.FP\
#   2. Copies config-windows.example.yaml → C:\ProgramData\Odoo.ErpNet.FP\config.yaml
#      (skipped if config already exists — admin's data is preserved)
#   3. Creates C:\ProgramData\Odoo.ErpNet.FP\logs\
#   4. Runs `python.exe -m odoo_erpnet_fp.server.win_service install`
#      to register the OdooErpNetFP service with the SCM
#   5. Sets startup type = Automatic and starts the service
#
# Run from the repo root:
#   ./packaging/windows-server/build-installer.sh
# Output: dist/erpnet-fp-server-<version>-setup.exe

set -euo pipefail

cd "$(dirname "$0")/../.."

VERSION="$(python3 -c 'import tomllib; print(tomllib.loads(open("pyproject.toml").read())["project"]["version"])')"
echo "→ ErpNet.FP server installer build, version=${VERSION}"

PY_VERSION="3.12.7"
PY_EMBED_URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-embed-amd64.zip"

BUILD_DIR="$(pwd)/build/win-server"
DIST_DIR="$(pwd)/dist"
mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

IMG="erpnet-fp-server-builder:latest"

echo "→ Building / refreshing Docker builder image"
docker build -q -t "${IMG}" -f packaging/windows-server/Dockerfile packaging/windows-server > /dev/null

# Run the staging steps inside the Docker builder. /work is bound to
# the repo root, /out to the build dir.
docker run --rm \
    -v "$(pwd)":/work \
    -v "${BUILD_DIR}":/out \
    -e PY_VERSION="${PY_VERSION}" \
    -e PY_EMBED_URL="${PY_EMBED_URL}" \
    "${IMG}" bash -ec '
        set -e
        cd /out

        echo "  ▸ download Python ${PY_VERSION} embeddable"
        rm -rf python-embed.zip python
        curl -fsSL -o python-embed.zip "${PY_EMBED_URL}"
        mkdir -p python
        cd python
        unzip -q ../python-embed.zip
        cd ..

        echo "  ▸ enable site-packages in embeddable Python"
        # The embeddable distribution disables site by default — we
        # need to enable it so wheels installed under Lib/site-packages
        # are discovered. Edit python312._pth to uncomment "import site".
        PTHFILE=$(ls python/python*._pth)
        if [ -n "${PTHFILE}" ]; then
            sed -i "s|^#import site|import site|" "${PTHFILE}"
        fi

        echo "  ▸ copy ErpNet.FP source tree"
        rm -rf server
        mkdir -p server
        cp -r /work/odoo_erpnet_fp server/
        cp /work/pyproject.toml server/
        cp /work/README.md server/

        echo "  ▸ build the ErpNet.FP wheel"
        rm -rf wheels
        mkdir -p wheels
        python3 -m pip wheel --no-deps -w wheels ./server

        echo "  ▸ download Windows wheels — resolved from our own metadata"
        # Никакъв ръчен списък. pip оценява маркерите спрямо СВОЯ
        # интерпретатор, затова затварянето се смята от помощния скрипт
        # за целевата среда (win32/3.12) — иначе `evdev ; linux` събаря
        # свалянето, а изпуснат annotated_doc гърми чак в магазина.
        python3 /work/packaging/windows-server/fetch_win_wheels.py \
            wheels wheels/odoo_erpnet_fp-*.whl pywin32

        echo "  ▸ install everything into the embedded site-packages"
        # Вграденият Python НЯМА pip — затова машината-цел не може да
        # инсталира нищо при setup. Слагаме готово site-packages тук.
        SP=python/Lib/site-packages
        mkdir -p ${SP}
        python3 -m pip install \
            --no-index --no-deps \
            --platform win_amd64 --python-version 3.12 --only-binary=:all: \
            --target ${SP} wheels/*.whl

        echo "  ▸ pythoncom/pywintypes DLLs next to python.exe"
        # Без тях SCM-ът не зарежда служебния модул и `sc start` пада с
        # "service did not respond in a timely fashion". Копираме ги тук,
        # за да не зависи стартът единствено от pywin32_postinstall.
        cp ${SP}/pywin32_system32/*.dll python/

        echo "  ▸ verify the dependency closure is complete"
        # Тестът МОЖЕ да падне: чете Requires-Dist на всяка инсталирана
        # дистрибуция, оценява маркерите за Windows/py3.12 и иска всяка
        # останала да е налична. Точно това хваща липсващ annotated_doc
        # ПРЕДИ да стигне до машината-цел.
        python3 - <<'"'"'PYCHECK'"'"'
import sys, pathlib
from packaging.requirements import Requirement

ENV = {
    "python_version": "3.12", "python_full_version": "3.12.7",
    "sys_platform": "win32", "platform_system": "Windows",
    "platform_machine": "AMD64", "os_name": "nt",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "extra": "",
}
sp = pathlib.Path("python/Lib/site-packages")
have = {d.name.split("-")[0].lower().replace("_", "-") for d in sp.glob("*.dist-info")}
missing = set()
for meta in sp.glob("*.dist-info/METADATA"):
    owner = meta.parent.name.split("-")[0]
    for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("Requires-Dist:"):
            continue
        req = Requirement(line.split(":", 1)[1].strip())
        if req.marker is not None and not req.marker.evaluate(ENV):
            continue
        name = req.name.lower().replace("_", "-")
        if name not in have:
            missing.add((owner, name))
if missing:
    for owner, dep in sorted(missing):
        print("  x %s wants %s - MISSING" % (owner, dep))
    sys.exit(1)
print("  ok dependency closure complete: %d distributions" % len(have))
PYCHECK

        echo "  ▸ stage default config"
        rm -rf config
        mkdir -p config
        cp /work/config-examples/config-windows.example.yaml config/config.yaml

        echo "  ▸ stage NSIS installer script"
        cp /work/packaging/windows-server/installer.nsi installer.nsi

        echo "  ▸ build setup.exe with NSIS"
        makensis -V2 \
            -DAPP_VERSION="'"${VERSION}"'" \
            -DOUTFILE="erpnet-fp-server-'"${VERSION}"'-setup.exe" \
            installer.nsi

        echo "  ▸ done"
    '

# Move the resulting setup.exe to dist/
mv "${BUILD_DIR}/erpnet-fp-server-${VERSION}-setup.exe" "${DIST_DIR}/"

echo ""
echo "Built: ${DIST_DIR}/erpnet-fp-server-${VERSION}-setup.exe"
ls -lh "${DIST_DIR}/erpnet-fp-server-${VERSION}-setup.exe"

echo ""
echo "On a Windows 11 / 10 machine, run as Administrator:"
echo "  erpnet-fp-server-${VERSION}-setup.exe"
echo ""
echo "After install:"
echo "  - Service:   sc query OdooErpNetFP"
echo "  - Dashboard: http://127.0.0.1:8001/"
echo "  - Logs:      C:\\ProgramData\\Odoo.ErpNet.FP\\logs\\service.log"
echo "  - Config:    C:\\ProgramData\\Odoo.ErpNet.FP\\config.yaml"
