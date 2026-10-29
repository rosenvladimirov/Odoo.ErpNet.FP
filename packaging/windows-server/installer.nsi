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
;   OPENSSH_VERSION      версията на OpenSSH-Win64.msi (напр. 10.0.0.0)
;   CLOUDFLARED_VERSION  версията на cloudflared.exe
;
; Отдалеченият достъп (ADR erpnet-kasi-fleet/0001–0003): касата получава
; КОД ЗА ЗАПИСВАНЕ (EFP1.…), направен на лаптопа — само тайната на своя
; тунел, никакъв API токен. Тих режим: /S /ENROLL=EFP1.…
; Без код: проксито и sshd; заварено записване се пази при обновяване.

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

!ifndef OPENSSH_VERSION
    !define OPENSSH_VERSION "0.0.0.0"
!endif
!ifndef CLOUDFLARED_VERSION
    !define CLOUDFLARED_VERSION "0"
!endif

!include "nsDialogs.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"

; Инсталаторът е 32-битов: $SYSDIR е SysWOW64 и оттам тръгва 32-битов
; PowerShell (без DISM, с $env:ProgramFiles = „Program Files (x86)“).
; sysnative е входът на 32-битов процес към истинския System32.
!define PS '"$WINDIR\sysnative\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File'

!define TUNNEL_SERVICE "ErpNetFP-Tunnel"

; Кодът за записване от страницата „Remote access“ (или /ENROLL=)
Var EnrollCode
Var hEnrollCode

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
Page custom RemotePageCreate RemotePageLeave
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

; ─── Remote access page ──────────────────────────────────────────────

Function .onInit
    ${IfNot} ${RunningX64}
        MessageBox MB_ICONSTOP "${APP_NAME} requires 64-bit Windows." /SD IDOK
        Abort
    ${EndIf}
    ; $PROGRAMFILES64 в 32-битов NSIS се оказа „Program Files (x86)“ —
    ; пътят идва от ProgramW6432, освен ако не е подаден с /D=
    ${If} $INSTDIR == "$PROGRAMFILES64\${APP_NAME}"
    ${OrIf} $INSTDIR == "$PROGRAMFILES\${APP_NAME}"
        ReadEnvStr $0 "ProgramW6432"
        StrCpy $INSTDIR "$0\${APP_NAME}"
    ${EndIf}
    ${GetParameters} $R0
    ClearErrors
    ${GetOptions} $R0 "/ENROLL=" $EnrollCode
FunctionEnd

Function RemotePageCreate
    nsDialogs::Create 1018
    Pop $0
    ${If} $0 == error
        Abort
    ${EndIf}

    ${NSD_CreateLabel} 0 0 100% 36u "Remote access (optional). Paste the enrollment code you received for this machine (starts with EFP1.). It connects this machine to its own private Cloudflare tunnel - nothing is published, no firewall port is opened, and no other cloudflared or SSH setup on this machine is changed."
    Pop $0
    ${NSD_CreateLabel} 0 42u 100% 9u "Enrollment code (leave empty for a local-only install):"
    Pop $0
    ${NSD_CreateText} 0 53u 100% 12u $EnrollCode
    Pop $hEnrollCode
    ${NSD_CreateLabel} 0 72u 100% 30u "On an upgrade you can leave it empty: an existing enrollment is kept."
    Pop $0

    nsDialogs::Show
FunctionEnd

Function RemotePageLeave
    ${NSD_GetText} $hEnrollCode $EnrollCode
    ${If} $EnrollCode != ""
        StrCpy $0 $EnrollCode 5
        ${If} $0 != "EFP1."
            MessageBox MB_ICONEXCLAMATION "This is not an enrollment code - it must start with EFP1." /SD IDOK
            Abort
        ${EndIf}
    ${EndIf}
FunctionEnd

; ─── Sections ────────────────────────────────────────────────────────

; Зависимост: OpenSSH сървърът е преди проксито; ако не тръгне,
; инсталацията спира.
Section "OpenSSH Server ${OPENSSH_VERSION}" SEC_OPENSSH
    SectionIn RO

    SetOutPath "$INSTDIR\deps"
    File "deps\OpenSSH-Win64.msi"
    File "deps\versions.env"
    SetOutPath "$INSTDIR\scripts"
    File "scripts\install-openssh.ps1"
    File "scripts\stop-ours.ps1"
    File "scripts\install-edge.ps1"

    DetailPrint "Installing OpenSSH Server ${OPENSSH_VERSION}..."
    nsExec::ExecToLog '${PS} "$INSTDIR\scripts\install-openssh.ps1" -MsiPath "$INSTDIR\deps\OpenSSH-Win64.msi" -MsiVersion "${OPENSSH_VERSION}"'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_ICONSTOP "OpenSSH Server could not be installed (exit $0). See the details above and %ProgramData%\${APP_NAME}\logs\openssh-msi.log. Installation is aborted." /SD IDOK
        Abort
    ${EndIf}
SectionEnd


Section "Core (Python + server)" SEC_CORE
    SectionIn RO   ; required, can't deselect

    ; Преинсталация върху жива инсталация: NSIS копира ОТГОРЕ и не трие.
    ; Оставените стари *.dist-info правеха /healthz да съобщава чужда
    ; версия (видяно живо: код 3.0.6, отчетено 3.0.3). Затова спираме
    ; услугата и разчистваме рънтайма, преди да сложим новия.
    DetailPrint "Stopping the services and clearing the previous runtime..."
    ; И двете наши услуги вървят от вградения Python — докато някоя е жива,
    ; python3.dll е заключен и подмяната пада с „Can't write“ (видяно
    ; 05.10: ErpNetFP-Tunnel от предишната инсталация). sc stop е
    ; асинхронен ⇒ чакаме спиране и накрая убиваме само процеси, пуснати
    ; от НАШАТА папка (pythonservice.exe, cloudflared.exe) — чужди не.
    nsExec::ExecToLog '${PS} "$INSTDIR\scripts\stop-ours.ps1" -InstallDir "$INSTDIR"'
    Sleep 1000
    ; Регистрацията на услугата сочи python.exe на старото място — трие се
    ; и се прави наново долу (иначе „install“ гърми с „already exists“, а
    ; „start“ пуска старото копие).
    nsExec::ExecToLog '"$SYSDIR\sc.exe" delete ${SERVICE_NAME}'
    Sleep 1000

    ; Предишна инсталация на друго място (напр. „Program Files (x86)“) —
    ; рънтаймът ѝ се маха; config и логовете са в ProgramData и остават.
    ReadRegStr $R5 HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}" "InstallLocation"
    ${If} $R5 != ""
    ${AndIf} $R5 != $INSTDIR
    ${AndIf} ${FileExists} "$R5\python\python.exe"
        DetailPrint "Removing the previous runtime at $R5"
        RMDir /r "$R5\python"
        RMDir /r "$R5\server"
        RMDir /r "$R5\wheels"
        RMDir /r "$R5\deps"
        RMDir /r "$R5\scripts"
        Delete "$R5\installer.nsi"
        Delete "$R5\uninstall.exe"
        RMDir "$R5"
    ${EndIf}

    RMDir /r "$INSTDIR\python\Lib\site-packages"
    RMDir /r "$INSTDIR\server"

    SetOutPath "$INSTDIR\python"
    File /r "python\*.*"

    SetOutPath "$INSTDIR\server"
    File /r "server\*.*"

    SetOutPath "$INSTDIR"
    File "installer.nsi"   ; bundled for diagnostics; harmless

    ; Зависимостите и самият пакет вече са вътре в
    ; python\Lib\site-packages — сглобени при билда. Вграденият Python
    ; няма pip, затова на тази машина не се инсталира нищо.

    ; pywin32 has a postinstall step that copies pythoncom312.dll +
    ; pywintypes312.dll to a location where the SCM can find them when
    ; it loads our service module. Without this step `python -m
    ; odoo_erpnet_fp.server.win_service install` succeeds but `start`
    ; fails with "service did not respond in a timely fashion".
    DetailPrint "Running pywin32 post-install (copies pythoncom DLLs)..."
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" "$INSTDIR\python\Lib\site-packages\bin\pywin32_postinstall.py" -install'
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

Section "Remote access tunnel" SEC_TUNNEL
    SectionIn RO
    ReadEnvStr $1 "ProgramData"

    ; Нашата услуга (не чуждата „Cloudflared“) се спира, преди да се
    ; подмени cloudflared.exe, и се регистрира наново накрая.
    nsExec::ExecToLog '"$SYSDIR\sc.exe" stop ${TUNNEL_SERVICE}'
    Sleep 2000
    nsExec::ExecToLog '"$SYSDIR\sc.exe" delete ${TUNNEL_SERVICE}'
    Sleep 1000

    ${If} $EnrollCode != ""
        SetOutPath "$INSTDIR\tunnel"
        File "deps\cloudflared.exe"
        ; Кодът носи тайна ⇒ през средата, не през командния ред
        System::Call 'kernel32::SetEnvironmentVariable(t "ERPNET_ENROLL_CODE", t "$EnrollCode")'
        DetailPrint "Enrolling this machine (cloudflared ${CLOUDFLARED_VERSION})..."
        nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m odoo_erpnet_fp.enroll apply'
        Pop $0
        System::Call 'kernel32::SetEnvironmentVariable(t "ERPNET_ENROLL_CODE", i 0)'
        StrCpy $EnrollCode ""
        ${If} $0 != 0
            MessageBox MB_ICONEXCLAMATION "The proxy is installed, but enrollment failed (exit $0). See the details above; rerun the installer with a valid code." /SD IDOK
            Return
        ${EndIf}
    ${ElseIf} ${FileExists} "$1\${APP_NAME}\tunnel\token"
        SetOutPath "$INSTDIR\tunnel"
        File "deps\cloudflared.exe"
        DetailPrint "Keeping the existing enrollment ($1\${APP_NAME}\enroll.json)."
    ${Else}
        DetailPrint "No enrollment code - remote access is not set up."
        Return
    ${EndIf}

    DetailPrint "Registering service ${TUNNEL_SERVICE}..."
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m odoo_erpnet_fp.server.win_tunnel_service --startup auto install'
    nsExec::ExecToLog '"$SYSDIR\sc.exe" start ${TUNNEL_SERVICE}'

    ; Кодът може да носи и апарата/CORS (setup_fiscal) и сертификата пред касата
    ; (install-edge: Traefik + certpull) — ADR erpnet-kasi-fleet/0005.
    ${If} ${FileExists} "$1\${APP_NAME}\edge\edge.json"
        DetailPrint "Fiscal device and CORS from the enrollment code..."
        nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m odoo_erpnet_fp.setup_fiscal --from-enroll'
        nsExec::ExecToLog '"$SYSDIR\sc.exe" stop ${SERVICE_NAME}'
        Sleep 3000
        nsExec::ExecToLog '"$SYSDIR\sc.exe" start ${SERVICE_NAME}'
        DetailPrint "HTTPS in front of the till (Traefik + certificate)..."
        nsExec::ExecToLog '${PS} "$INSTDIR\scripts\install-edge.ps1" -FromEnroll'
        Pop $0
        ${If} $0 != 0
            MessageBox MB_ICONEXCLAMATION "The proxy and the tunnel are installed, but the HTTPS front (Traefik) failed (exit $0). See the details above and %ProgramData%\${APP_NAME}\logs\edge.log." /SD IDOK
        ${EndIf}
    ${EndIf}
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
    DetailPrint "Remote:    $1\${APP_NAME}\enroll.json, logs\tunnel.log"
SectionEnd

; ─── Uninstaller ─────────────────────────────────────────────────────

Section "Uninstall"
    nsExec::ExecToLog '${PS} "$INSTDIR\scripts\stop-ours.ps1" -InstallDir "$INSTDIR"'
    DetailPrint "Stopping service..."
    nsExec::ExecToLog '"$SYSDIR\sc.exe" stop ${SERVICE_NAME}'
    Sleep 2000   ; let SCM transition to STOPPED

    DetailPrint "Removing service registration..."
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m odoo_erpnet_fp.server.win_service remove'

    ; Тунелът и записването: махаме само нашето — услугата, блока в
    ; hosts, редовете с белег erpnet-fp: в ключовете, токена (ADR-0003).
    nsExec::ExecToLog '"$SYSDIR\sc.exe" stop ${TUNNEL_SERVICE}'
    Sleep 2000
    nsExec::ExecToLog '"$SYSDIR\sc.exe" delete ${TUNNEL_SERVICE}'
    ; Traefik пред касата: нашата услуга и нашият блок в hosts (сертификатът остава в ProgramData)
    IfFileExists "$INSTDIR\scripts\install-edge.ps1" 0 +2
        nsExec::ExecToLog '${PS} "$INSTDIR\scripts\install-edge.ps1" -Remove'
    nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m odoo_erpnet_fp.enroll remove'

    ; Remove install dir
    RMDir /r "$INSTDIR\edge"
    RMDir /r "$INSTDIR\python"
    RMDir /r "$INSTDIR\wheels"
    RMDir /r "$INSTDIR\server"
    RMDir /r "$INSTDIR\deps"
    RMDir /r "$INSTDIR\scripts"
    RMDir /r "$INSTDIR\tunnel"
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

    ; sshd остава — може да го ползва и друг; тунелът в Cloudflare се маха
    ; от лаптопа (kasi.py retire).
    DetailPrint "OpenSSH Server is left installed (remove it from Programs and Features if unused)."
SectionEnd
