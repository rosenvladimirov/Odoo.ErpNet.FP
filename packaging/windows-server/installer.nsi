; NSIS installer for Odoo.ErpNet.FP — Windows service edition.
;
; Built by packaging/windows-server/build-installer.sh, which stages
; everything under /out (= build/win-server/) and then invokes
; makensis from that directory. All `File ...` paths in this script
; are relative to that staging directory.
;
; Macro variables passed via -D:
;   APP_VERSION   e.g. 0.2.9
;   OUTFILE       e.g. erpnet-fp-server-0.2.9-setup.exe

!ifndef APP_VERSION
    !define APP_VERSION "0.0.0"
!endif

!ifndef OUTFILE
    !define OUTFILE "erpnet-fp-server-setup.exe"
!endif

!define APP_NAME "Odoo.ErpNet.FP"
!define APP_PUBLISHER "Rosen Vladimirov"
!define APP_URL "https://github.com/rosenvladimirov/Odoo.ErpNet.FP"
!define SERVICE_NAME "OdooErpNetFP"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "${OUTFILE}"
InstallDir "$PROGRAMFILES64\${APP_NAME}"
RequestExecutionLevel admin
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName"     "${APP_NAME}"
VIAddVersionKey "FileDescription" "ErpNet.FP fiscal printer proxy installer"
VIAddVersionKey "FileVersion"     "${APP_VERSION}.0"
VIAddVersionKey "ProductVersion"  "${APP_VERSION}.0"
VIAddVersionKey "CompanyName"     "${APP_PUBLISHER}"
VIAddVersionKey "LegalCopyright"  "© 2026 ${APP_PUBLISHER} (LGPL-3.0)"

Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

; ─── Sections ────────────────────────────────────────────────────────

Section "Core (Python + server + wheels)" SEC_CORE
    SectionIn RO   ; required, can't deselect

    SetOutPath "$INSTDIR\python"
    File /r "python\*.*"

    SetOutPath "$INSTDIR\wheels"
    File /r "wheels\*.*"

    SetOutPath "$INSTDIR\server"
    File /r "server\*.*"

    SetOutPath "$INSTDIR"
    File "installer.nsi"   ; bundled for diagnostics; harmless

    DetailPrint "Installing wheels into the embedded Python..."
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m pip install --no-index --find-links "$INSTDIR\wheels" --target "$INSTDIR\python\Lib\site-packages" --upgrade pip'
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m pip install --no-index --find-links "$INSTDIR\wheels" --target "$INSTDIR\python\Lib\site-packages" pyserial fastapi uvicorn pydantic PyYAML httpx prometheus_client pywin32'

    DetailPrint "Installing server source..."
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m pip install --no-deps --target "$INSTDIR\python\Lib\site-packages" "$INSTDIR\server"'

    ; pywin32 has a postinstall step that copies pythoncom312.dll +
    ; pywintypes312.dll to a location where the SCM can find them when
    ; it loads our service module. Without this step `python -m
    ; odoo_erpnet_fp.server.win_service install` succeeds but `start`
    ; fails with "service did not respond in a timely fashion".
    DetailPrint "Running pywin32 post-install (copies pythoncom DLLs)..."
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" "$INSTDIR\python\Lib\site-packages\pywin32_postinstall.py" -install'
SectionEnd

Section "Default config (preserves existing)" SEC_CONFIG
    SectionIn RO

    ReadEnvStr $0 "ProgramData"
    SetOutPath "$0\${APP_NAME}"

    ; Only copy the default config if it doesn't already exist —
    ; existing admin tweaks are preserved on reinstall / upgrade.
    IfFileExists "$0\${APP_NAME}\config.yaml" config_exists 0
        File /oname=config.yaml "config\config.yaml"
        DetailPrint "Wrote default config to $0\${APP_NAME}\config.yaml"
        Goto config_done
    config_exists:
        DetailPrint "Existing config preserved at $0\${APP_NAME}\config.yaml"
    config_done:

    ; Always create the logs directory.
    CreateDirectory "$0\${APP_NAME}\logs"

    ; And a sample copy that is overwritten every install (admins can
    ; diff against config.yaml to discover new options after upgrades).
    SetOutPath "$0\${APP_NAME}"
    File /oname=config.example.yaml "config\config.yaml"
SectionEnd

Section "Register + start service" SEC_SERVICE
    SectionIn RO

    DetailPrint "Registering Windows service ${SERVICE_NAME}..."
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m odoo_erpnet_fp.server.win_service --startup auto install'

    DetailPrint "Starting service..."
    nsExec::ExecToLog '"$SYSDIR\sc.exe" start ${SERVICE_NAME}'
SectionEnd

Section "Start menu shortcuts" SEC_SHORTCUTS
    ; NSIS doesn't have a built-in $ProgramData; resolve at install time.
    ReadEnvStr $1 "ProgramData"

    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Dashboard.lnk" \
        "http://127.0.0.1:8001/"
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Open config.lnk" \
        "$WINDIR\notepad.exe" '"$1\${APP_NAME}\config.yaml"'
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\View logs.lnk" \
        "$WINDIR\notepad.exe" '"$1\${APP_NAME}\logs\service.log"'
    CreateShortcut "$SMPROGRAMS\${APP_NAME}\Uninstall.lnk" \
        "$INSTDIR\uninstall.exe"
SectionEnd

Section "-PostInstall"
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Add Programs and Features entry
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "DisplayName" "${APP_NAME}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "Publisher" "${APP_PUBLISHER}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "URLInfoAbout" "${APP_URL}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "InstallLocation" "$INSTDIR"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "NoModify" 1
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" \
        "NoRepair" 1

    ReadEnvStr $1 "ProgramData"
    DetailPrint ""
    DetailPrint "Installation complete."
    DetailPrint "Dashboard: http://127.0.0.1:8001/"
    DetailPrint "Service:   sc query ${SERVICE_NAME}"
    DetailPrint "Config:    $1\${APP_NAME}\config.yaml"
    DetailPrint "Logs:      $1\${APP_NAME}\logs\service.log"
SectionEnd

; ─── Uninstaller ─────────────────────────────────────────────────────

Section "Uninstall"
    DetailPrint "Stopping service..."
    nsExec::ExecToLog '"$SYSDIR\sc.exe" stop ${SERVICE_NAME}'
    Sleep 2000   ; let SCM transition to STOPPED

    DetailPrint "Removing service registration..."
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m odoo_erpnet_fp.server.win_service remove'

    ; Remove install dir
    RMDir /r "$INSTDIR\python"
    RMDir /r "$INSTDIR\wheels"
    RMDir /r "$INSTDIR\server"
    Delete "$INSTDIR\installer.nsi"
    Delete "$INSTDIR\uninstall.exe"
    RMDir "$INSTDIR"

    ; Start menu
    Delete "$SMPROGRAMS\${APP_NAME}\Dashboard.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Open config.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\View logs.lnk"
    Delete "$SMPROGRAMS\${APP_NAME}\Uninstall.lnk"
    RMDir  "$SMPROGRAMS\${APP_NAME}"

    ; Programs and Features
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"

    ; Leave the ProgramData tree in place — admin's data, including
    ; config.yaml, logs, and any custom certs. They can delete it
    ; manually if they really mean to wipe everything.
    ReadEnvStr $1 "ProgramData"
    DetailPrint "Leaving config + logs at $1\${APP_NAME}\ (admin's data)"
SectionEnd
