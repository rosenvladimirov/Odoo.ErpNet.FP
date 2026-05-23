
## [0.13.0] — 2026-05-23
### Added
- DatecsPay pinpad: пълен event-loop `datecs_run_transaction` в C (NDA библиотеката); проксито вика само тази функция.
- `POST /pinpads/{id}/cancel` — прекъсване на текущата транзакция без да чака lock-а; thread-safe `cancel_requested` флаг в C, polled от run_transaction (≤2s latency); опит за TRANSACTION END abort на терминала.
- `/pinpads/{id}/purchase` (реално работещ), `void`, `end_of_day`, `test_connection`.
- USB autodetect: пропускане на pinpad VID-ове като ридъри.

### Changed
- `PinpadEntry.active_pinpad` — публичен handle към активния facade за безлок-овото cancel route.

## [0.13.2] — 2026-05-23
### Added
- `udev/99-erpnet-fp.rules` — stable `/dev/datecs_pinpad` + `/dev/honeywell_scanner` symlinks (по vendor:product ID, оцеляват power-cycle/re-enumeration).
- `scripts/install-udev.sh` — host install helper (sudo).

### Changed
- `reader_autodetect._build_reader_config` предпочита udev DEVLINKS симлинк (custom-named → `/dev/serial/by-id/*` → raw devnode). UI-ът и логовете сега показват `/dev/honeywell_scanner` вместо разменчивия `/dev/ttyACM0`.

## [0.13.3] — 2026-05-23
### Added
- Multi-device udev support: всяко правило създава И `<name>_<serial>` symlink (винаги уникален), не само генеричния. При 2+ устройства от един тип use serial-suffix-а в `config.yaml`; генеричният сочи към последно enumerated устройство (недетерминистичен).
- `_prefer_stable_symlink` сега предпочита по-дългия (specific) symlink → UI винаги показва кое физическо устройство е свързано.

### Changed
- `config.yaml` примерният bluepad55 ползва `/dev/datecs_pinpad_3526900033` (per-serial) — оцелява добавяне на 2-ро устройство.

## [0.13.4] — 2026-05-23
### Security / NDA boundary
- **DockerHub repo превърнат в private** — distribution става само под NDA agreement.
- **Header `datecs_pinpad_driver.h` премахнат от distribution**: pyproject `package-data` сега bundle-ва само `lib/*.so`. Headerът беше „Rosetta stone" за протокола (всички cmd/subcmd/TLV кодове в plain text); .so-то изисква reverse engineering, което е значително по-висока бариера.
- **`.so` strip-нат с `--strip-unneeded`**: запазва exported symbols за ctypes, но премахва debug info → -12% size + по-голяма RE трудност.
