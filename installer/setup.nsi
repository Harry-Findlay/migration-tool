; installer/setup.nsi
; ============================================================================
; IT INFINITY Migration Tool — NSIS Installer
;
; Requires NSIS 3.x: https://nsis.sourceforge.io/
; Build: makensis setup.nsi
;
; What this installer does:
;   1. Copies all application files to %PROGRAMFILES%\ITInfinityMigrator\
;   2. Copies lib\ (FbBridge, dcmtk, intl, plugins) into the install dir
;   3. Installs Python dependencies via pip (bundled pip or system Python)
;   4. Installs and starts the Windows service (ITInfinityMigrator)
;   5. Configures the service to auto-restart on failure
;   6. Creates a desktop shortcut that simply opens http://localhost:5000
;   7. Creates an uninstaller
; ============================================================================

!define APP_NAME        "IT INFINITY Migration Tool"
!define APP_VERSION     "2.5.0"
!define SERVICE_NAME    "ITInfinityMigrator"
!define INSTALL_DIR     "$PROGRAMFILES64\ITInfinityMigrator"
!define DATA_DIR        "$COMMONAPPDATA\ITInfinityMigrator"
!define SHORTCUT_NAME   "IT INFINITY Migration Tool.lnk"
!define PUBLISHER       "IT INFINITY Ltd"
!define UNINSTALLER     "Uninstall.exe"

Name            "${APP_NAME} ${APP_VERSION}"
OutFile         "IT-INFINITY-Migration-Tool-Setup.exe"
InstallDir      "${INSTALL_DIR}"
RequestExecutionLevel admin
SetCompressor   lzma

; ── Pages ─────────────────────────────────────────────────────────────────────
Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

; ── Install ───────────────────────────────────────────────────────────────────
Section "Main" SecMain

    SetOutPath "${INSTALL_DIR}"

    ; ── Core application files ────────────────────────────────────────────────
    File "server.py"
    File "service.py"
    File "requirements.txt"
    File ".env.example"
    File "README.md"

    ; ── Python packages (core, auth, datasources, static, api) ───────────────
    File /r "core\"
    File /r "auth\"
    File /r "datasources\"
    File /r "static\"
    File /r "api\"

    ; ── Native libraries (fb_bridge, dcmtk, intl, plugins) ───────────────────
    File /r "lib\"

    ; ── Create data / log directory ───────────────────────────────────────────
    CreateDirectory "${DATA_DIR}"

    ; ── Copy .env.example → .env if .env doesn't already exist ───────────────
    IfFileExists "${INSTALL_DIR}\.env" env_exists
        CopyFiles "${INSTALL_DIR}\.env.example" "${INSTALL_DIR}\.env"
    env_exists:

    ; ── Install Python dependencies ───────────────────────────────────────────
    DetailPrint "Installing Python dependencies…"
    nsExec::ExecToLog 'python -m pip install --quiet -r "${INSTALL_DIR}\requirements.txt"'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_ICONEXCLAMATION \
            "pip install returned code $0.$\nDependencies may be missing.$\nCheck the log and run pip manually if needed."
    ${EndIf}

    ; ── Stop existing service if running ──────────────────────────────────────
    DetailPrint "Stopping existing service (if running)…"
    nsExec::ExecToLog 'sc stop "${SERVICE_NAME}"'
    Sleep 2000

    ; ── Remove old service registration ───────────────────────────────────────
    nsExec::ExecToLog 'sc delete "${SERVICE_NAME}"'
    Sleep 1000

    ; ── Install the Windows service ───────────────────────────────────────────
    DetailPrint "Installing Windows service…"
    nsExec::ExecToLog 'python "${INSTALL_DIR}\service.py" install'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_ICONEXCLAMATION \
            "Service installation returned code $0.$\nThe tool may not start automatically."
    ${EndIf}

    ; ── Configure automatic restart on failure ────────────────────────────────
    ; sc failure: reset counter after 60s; restart after 5s on 1st/2nd failure,
    ;             10s on 3rd
    DetailPrint "Configuring service recovery…"
    nsExec::ExecToLog 'sc failure "${SERVICE_NAME}" reset= 60 actions= restart/5000/restart/5000/restart/10000'

    ; ── Set service description ───────────────────────────────────────────────
    nsExec::ExecToLog 'sc description "${SERVICE_NAME}" "Hosts the IT INFINITY dental imaging migration tool on http://localhost:5000"'

    ; ── Start the service ─────────────────────────────────────────────────────
    DetailPrint "Starting service…"
    nsExec::ExecToLog 'sc start "${SERVICE_NAME}"'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_ICONEXCLAMATION \
            "Service failed to start (code $0).$\n$\nCheck that .env is configured with your Azure AD credentials,$\nthen start the service manually:$\n  sc start ${SERVICE_NAME}"
    ${EndIf}

    ; ── Desktop shortcut — opens the browser, nothing else ───────────────────
    DetailPrint "Creating desktop shortcut…"
    CreateShortcut "$DESKTOP\${SHORTCUT_NAME}" \
        "$WINDIR\explorer.exe" \
        "http://localhost:5000" \
        "${INSTALL_DIR}\static\icon.ico" 0 \
        SW_SHOWNORMAL \
        "" \
        "Open IT INFINITY Migration Tool"

    ; ── Start Menu shortcut ───────────────────────────────────────────────────
    CreateDirectory "$SMPROGRAMS\${PUBLISHER}"
    CreateShortcut  "$SMPROGRAMS\${PUBLISHER}\${SHORTCUT_NAME}" \
        "$WINDIR\explorer.exe" \
        "http://localhost:5000" \
        "${INSTALL_DIR}\static\icon.ico" 0

    ; ── Add/Remove Programs entry ─────────────────────────────────────────────
    WriteRegStr HKLM \
        "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE_NAME}" \
        "DisplayName" "${APP_NAME}"
    WriteRegStr HKLM \
        "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE_NAME}" \
        "UninstallString" '"${INSTALL_DIR}\${UNINSTALLER}"'
    WriteRegStr HKLM \
        "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE_NAME}" \
        "Publisher" "${PUBLISHER}"
    WriteRegStr HKLM \
        "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE_NAME}" \
        "DisplayVersion" "${APP_VERSION}"

    WriteUninstaller "${INSTALL_DIR}\${UNINSTALLER}"

    DetailPrint "Installation complete."
    MessageBox MB_ICONINFORMATION \
        "${APP_NAME} has been installed.$\n$\nThe server is now running as a Windows service.$\nThe desktop shortcut will open http://localhost:5000 in your browser.$\n$\nIMPORTANT: Edit$\n  ${INSTALL_DIR}\.env$\nand add your Azure AD credentials, then restart the service:$\n  sc stop ${SERVICE_NAME}$\n  sc start ${SERVICE_NAME}"

SectionEnd


; ── Uninstall ─────────────────────────────────────────────────────────────────
Section "Uninstall"

    ; Stop and remove the service
    DetailPrint "Stopping and removing service…"
    nsExec::ExecToLog 'sc stop "${SERVICE_NAME}"'
    Sleep 2000
    nsExec::ExecToLog 'python "${INSTALL_DIR}\service.py" remove'

    ; Remove shortcuts
    Delete "$DESKTOP\${SHORTCUT_NAME}"
    Delete "$SMPROGRAMS\${PUBLISHER}\${SHORTCUT_NAME}"
    RMDir  "$SMPROGRAMS\${PUBLISHER}"

    ; Remove registry entry
    DeleteRegKey HKLM \
        "Software\Microsoft\Windows\CurrentVersion\Uninstall\${SERVICE_NAME}"

    ; Remove application files (but NOT .env or the SQLite database in DATA_DIR)
    RMDir /r "${INSTALL_DIR}\core"
    RMDir /r "${INSTALL_DIR}\auth"
    RMDir /r "${INSTALL_DIR}\datasources"
    RMDir /r "${INSTALL_DIR}\static"
    RMDir /r "${INSTALL_DIR}\api"
    RMDir /r "${INSTALL_DIR}\lib"
    Delete    "${INSTALL_DIR}\server.py"
    Delete    "${INSTALL_DIR}\service.py"
    Delete    "${INSTALL_DIR}\requirements.txt"
    Delete    "${INSTALL_DIR}\README.md"
    Delete    "${INSTALL_DIR}\${UNINSTALLER}"
    ; Leave .env in place so credentials survive reinstall
    RMDir     "${INSTALL_DIR}"   ; only removes if empty

    MessageBox MB_ICONINFORMATION \
        "${APP_NAME} has been uninstalled.$\n$\nYour .env configuration and migration history in$\n  ${DATA_DIR}$\nhave been preserved."

SectionEnd
