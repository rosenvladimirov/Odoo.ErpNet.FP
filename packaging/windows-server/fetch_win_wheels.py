#!/usr/bin/env python3
"""Download the Windows wheel closure for the given roots.

Why this exists: `pip download --platform win_amd64` only changes which
wheel TAGS are acceptable — environment markers are still evaluated
against the interpreter that runs pip. On a Linux builder that makes
`evdev>=1.6 ; sys_platform == "linux"` a hard requirement with no
Windows wheel, and the whole download aborts. Listing dependencies by
hand instead is what let `annotated_doc` reach a shop machine.

So we do the marker evaluation ourselves for the target environment and
call pip once per distribution with --no-deps.

Usage:
    fetch_win_wheels.py <dest-dir> <root>...

A root is either a local .whl path (read, not downloaded) or a plain
requirement string such as "pywin32".
"""

import subprocess
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

# Целевата среда: Windows x64, CPython 3.12 — същата, която носи
# вграденият Python в инсталатора.
ENV = {
    "python_version": "3.12",
    "python_full_version": "3.12.7",
    "sys_platform": "win32",
    "platform_system": "Windows",
    "platform_machine": "AMD64",
    "os_name": "nt",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
}

PLATFORM_ARGS = [
    "--platform", "win_amd64",
    "--python-version", "3.12",
    "--only-binary=:all:",
    "--no-deps",
]


def metadata_of(wheel: Path) -> dict:
    """Прочети METADATA от колелото, без да го разпакетваш."""
    with zipfile.ZipFile(wheel) as zf:
        name = next(n for n in zf.namelist()
                    if n.endswith(".dist-info/METADATA"))
        with zf.open(name) as fh:
            return Parser().parsestr(fh.read().decode("utf-8", "replace"))


def requires(wheel: Path, extras: set[str]) -> list[Requirement]:
    """Изискванията на едно колело, с оценени маркери за ENV."""
    meta = metadata_of(wheel)
    out = []
    for raw in meta.get_all("Requires-Dist") or []:
        req = Requirement(raw)
        if req.marker is None:
            out.append(req)
            continue
        # Всяка екстра се оценява отделно; базата е extra == "".
        for extra in {""} | extras:
            if req.marker.evaluate({**ENV, "extra": extra}):
                out.append(req)
                break
    return out


def download(dest: Path, spec: str) -> list[Path]:
    """Свали ЕДНА дистрибуция и върни новопоявилите се колела."""
    before = {p.name for p in dest.glob("*.whl")}
    subprocess.run(
        [sys.executable, "-m", "pip", "download", "--dest", str(dest)]
        + PLATFORM_ARGS + [spec],
        check=True,
    )
    return [p for p in dest.glob("*.whl") if p.name not in before]


def main() -> int:
    dest = Path(sys.argv[1])
    dest.mkdir(parents=True, exist_ok=True)

    queue: list[tuple[str, set[str]]] = []
    pending: list[tuple[Path, set[str]]] = []

    for root in sys.argv[2:]:
        path = Path(root)
        if path.suffix == ".whl" and path.exists():
            pending.append((path, set()))
        else:
            req = Requirement(root)
            queue.append((str(req), set(req.extras)))

    seen: set[str] = set()

    while queue or pending:
        while queue:
            spec, extras = queue.pop()
            key = canonicalize_name(Requirement(spec).name)
            if key in seen:
                continue
            seen.add(key)
            for wheel in download(dest, spec):
                pending.append((wheel, extras))

        while pending:
            wheel, extras = pending.pop()
            seen.add(canonicalize_name(wheel.name.split("-")[0]))
            for req in requires(wheel, extras):
                if canonicalize_name(req.name) in seen:
                    continue
                queue.append((f"{req.name}{req.specifier}", set(req.extras)))

    print("  ok windows closure: %d wheels" % len(list(dest.glob("*.whl"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
