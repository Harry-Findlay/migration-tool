; installer/setup.nsi
; ============================================================================
; IT INFINITY Migration Tool — NSIS Installer (PyInstaller build)
; Prerequisites: NSIS 3.x from https://nsis.sourceforge.io/
; Build: run build.bat from project root
; ============================================================================

!define APP_NAME      "IT INFINITY Migration Tool"
!define APP_VERSION   "2.5.0"
!define SERVICE_NAME  "ITInfinityMigrator"
!define SERVICE_EXE   "ITInfinityService.exe"
!define INSTALL_DIR   "$PROGRAMFILES64\ITInfinityMigrator"
!define DATA_DIR      "$COMMONAPPDATA\ITInfinityMigrator"
!define SHORTCUT_NAME "IT INFINITY Migration Tool.lnk"
!define PUBLISHER     "IT INFINITY Ltd"
!define UNINSTALLER   "Uninstall.exe"

Name            "${APP_NAME} ${APP_VERSION}"
OutFile         "IT-INFINITY-Migration-Tool-Setup.exe"
InstallDir      "${INSTALL_DIR}"
RequestExecutionLevel admin
SetCompressor   lzma

!include "MUI2.nsh"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; ── Install ───────────────────────────────────────────────────────────────────
Section "Main" SecMain

    SetOutPath "${INSTALL_DIR}"

    ; ── Stop + remove any existing service ───────────────────────────────────
    DetailPrint "Stopping existing service (if running)..."
    nsExec::ExecToLog 'sc stop "${SERVICE_NAME}"'
    Sleep 2000
    nsExec::ExecToLog 'sc delete "${SERVICE_NAME}"'
    Sleep 1000

    ; ── Copy PyInstaller bundle ───────────────────────────────────────────────
    ; dist\ITInfinityServer\ contains the compiled server (no .py source)
    File /r "..\dist\ITInfinityServer"
    File "..\dist\ITInfinityService.exe"
    File "..\env.example"

    ; ── Create data + log directory ───────────────────────────────────────────
    CreateDirectory "${DATA_DIR}"

    ; ── Copy env.example to .env if not already present ──────────────────────
    IfFileExists "${INSTALL_DIR}\.env" env_exists
        CopyFiles "${INSTALL_DIR}\env.example" "${INSTALL_DIR}\.env"
    env_exists:

    ; ── Register Windows service ──────────────────────────────────────────────
    DetailPrint "Installing Windows service..."
    nsExec::ExecToLog '"${INSTALL_DIR}\${SERVICE_EXE}" install'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_ICONEXCLAMATION \
            "Service installation returned code $0.$\nThe tool may not start automatically.$\nTry running as Administrator."
    ${EndIf}

    ; ── Configure service auto-restart on failure ────────────────────────────
    nsExec::ExecToLog 'sc failure "${SERVICE_NAME}" reset=60 actions=restart/5000/restart/10000/restart/30000'

    ; ── Set service to auto-start ────────────────────────────────────────────
    nsExec::ExecToLog 'sc config "${SERVICE_NAME}" start=auto'

    ; ── Start the service ─────────────────────────────────────────────────────
    DetailPrint "Starting service..."
    nsExec::ExecToLog 'sc start "${SERVICE_NAME}"'
    Sleep 3000

    ; ── Desktop shortcut ──────────────────────────────────────────────────────
    CreateShortcut "$DESKTOP\${SHORTCUT_NAME}" \
        "$WINDIR\explorer.exe" "http://localhost:5000" \
        "${INSTALL_DIR}\ITInfinityServer\static\icon.ico" 0

    ; ── Start Menu shortcut ───────────────────────────────────────────────────
    CreateDirectory "$SMPROGRAMS\${PUBLISHER}"
    CreateShortcut "$SMPROGRAMS\${PUBLISHER}\${SHORTCUT_NAME}" \
        "$WINDIR\explorer.exe" "http://localhost:5000" \
        "${INSTALL_DIR}\ITInfinityServer\static\icon.ico" 0

    ; ── Add/Remove Programs registry entry ───────────────────────────────────
    WriteRegStr HKLM \
        "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE_NAME}" \
        "DisplayName" "${APP_NAME}"
    WriteRegStr HKLM \
        "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE_NAME}" \
        "UninstallString" '"${INSTALL_DIR}\${UNINSTALLER}"'
    WriteRegStr HKLM \
        "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE_NAME}" \
        "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKLM \
        "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE_NAME}" \
        "Publisher" "${PUBLISHER}"
    WriteUninstaller "${INSTALL_DIR}\${UNINSTALLER}"

    MessageBox MB_ICONINFORMATION \
        "${APP_NAME} has been installed and the service is running.$\n$\nThe desktop shortcut opens http://localhost:5000 in your browser.$\n$\nIMPORTANT: Edit$\n  ${INSTALL_DIR}\.env$\nwith your Azure AD credentials, then restart the service:$\n  sc stop ${SERVICE_NAME}$\n  sc start ${SERVICE_NAME}"

SectionEnd


; ── Uninstall ─────────────────────────────────────────────────────────────────
Section "Uninstall"

    nsExec::ExecToLog 'sc stop "${SERVICE_NAME}"'
    Sleep 2000
    nsExec::ExecToLog '"${INSTALL_DIR}\${SERVICE_EXE}" remove'
    Sleep 1000

    Delete "$DESKTOP\${SHORTCUT_NAME}"
    Delete "$SMPROGRAMS\${PUBLISHER}\${SHORTCUT_NAME}"
    RMDir  "$SMPROGRAMS\${PUBLISHER}"

    DeleteRegKey HKLM \
        "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE_NAME}"

    RMDir /r "${INSTALL_DIR}\ITInfinityServer"
    Delete    "${INSTALL_DIR}\${SERVICE_EXE}"
    Delete    "${INSTALL_DIR}\env.example"
    Delete    "${INSTALL_DIR}\${UNINSTALLER}"
    ; Leave .env so credentials survive reinstall
    RMDir     "${INSTALL_DIR}"

    MessageBox MB_ICONINFORMATION \
        "${APP_NAME} has been uninstalled.$\n$\nYour .env configuration and migration history in$\n  ${DATA_DIR}$\nhave been preserved."

SectionEnd
