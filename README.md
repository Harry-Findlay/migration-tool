# IT INFINITY Migration Tool

## Project folder structure

```
ITInfinityMigrator/
│
├── server.py                   Flask web server — all API routes
├── service.py                  Windows service wrapper (pywin32)
├── requirements.txt            Python dependencies
├── .env.example                Copy to .env and fill in Azure credentials
├── README.md
│
├── auth/
│   ├── __init__.py
│   └── ms365.py                Microsoft 365 MSAL OAuth (auth code flow)
│
├── core/
│   ├── __init__.py
│   ├── models.py               Data models and enums
│   ├── base_datasource.py      Abstract base class for all datasources
│   ├── engine.py               Migration orchestration engine
│   └── migration_store.py      SQLite persistence (sessions, patients, audit)
│
├── datasources/
│   ├── __init__.py
│   ├── fb_client.py            Firebird bridge client (calls lib/fb_bridge/FbBridge.exe)
│   ├── vistasoft_source.py     VistaSoft source reader
│   ├── vistasoft_target.py     VistaSoft target writer
│   ├── dtxstudio_source.py     DTX Studio source
│   ├── dtxstudio_target.py     DTX Studio Core REST API target
│   └── sopro_source.py         SOPRO source reader
│
├── lib/                        Native binaries — do not modify
│   ├── fb_bridge/              Firebird embedded client + FbBridge.exe
│   │   ├── FbBridge.exe        .NET 8 self-contained executable
│   │   ├── fbclient.dll
│   │   ├── fbcrypt.dll
│   │   ├── firebird.msg
│   │   ├── FirebirdSql.Data.FirebirdClient.dll
│   │   ├── ib_util.dll
│   │   ├── icudt63.dll
│   │   ├── icudt63l.dat
│   │   ├── icuin63.dll
│   │   ├── icuuc63.dll
│   │   ├── msvcp140.dll
│   │   ├── vcruntime140.dll
│   │   ├── vcruntime140_1.dll
│   │   ├── intl/               Firebird internationalisation
│   │   └── plugins/            Firebird plugins
│   │
│   └── dcmtk/                  DCMTK binaries for DICOM conversion
│       └── img2dcm.exe         (and companion DLLs if needed)
│
├── static/
│   ├── index.html              The frontend (single-page app)
│   └── icon.ico                App icon (used by installer shortcut)
│
├── api/
│   └── __init__.py             Reserved for future Blueprint splitting
│
└── installer/
    └── setup.nsi               NSIS installer script
```

---

## How it works at runtime

```
Windows boot
    └── Windows Service Manager
            └── ITInfinityMigrator service  (service.py)
                    └── subprocess: python server.py
                            └── Flask on http://localhost:5000
                                    ├── Serves static/index.html
                                    ├── /auth/...   (MSAL OAuth)
                                    └── /api/...    (migration control)

User double-clicks desktop shortcut
    └── Opens http://localhost:5000 in default browser
        (shortcut target: explorer.exe http://localhost:5000)
```

The server is **always running** — no waiting for a process to start. The desktop shortcut is just a browser bookmark.

---

## First-time developer setup

### 1. Install Python 3.11+

Download from https://python.org — tick "Add to PATH" during install.

### 2. Install dependencies

```cmd
cd C:\path\to\ITInfinityMigrator
pip install -r requirements.txt
```

### 3. Register the Azure AD app

1. Go to **https://portal.azure.com** → Azure Active Directory → App registrations
2. **New registration**
   - Name: `IT INFINITY Migration Tool`
   - Supported account types: **Single tenant** (your organisation only)
   - Redirect URI: `Web` → `http://localhost:5000/auth/callback`
3. From the app overview, copy:
   - **Application (client) ID** → `AZURE_CLIENT_ID`
   - **Directory (tenant) ID**   → `AZURE_TENANT_ID`
4. Under **Certificates & secrets** → New client secret → copy the **Value**
   → `AZURE_CLIENT_SECRET`
5. Under **API permissions** → Add → Microsoft Graph → Delegated → `User.Read`
   → **Grant admin consent**

### 4. Configure .env

```cmd
copy .env.example .env
notepad .env
```

Fill in the three Azure values and generate a Flask secret key:
```cmd
python -c "import secrets; print(secrets.token_hex(32))"
```

### 5. Run the server directly (development)

```cmd
python server.py
```

Open http://localhost:5000 — sign in with your Microsoft 365 account.

---

## Building the installer

### Prerequisites

- **NSIS 3.x** — https://nsis.sourceforge.io/
- All files in the correct folder structure (above)
- `static/icon.ico` — create or export a 256×256 ICO file

### Build

```cmd
cd installer
makensis setup.nsi
```

This produces `IT-INFINITY-Migration-Tool-Setup.exe`.

### What the installer does

1. Copies all app files to `C:\Program Files\ITInfinityMigrator\`
2. Copies `lib\` (FbBridge, dcmtk) into the install directory
3. Runs `pip install -r requirements.txt`
4. Installs the Windows service: `python service.py install`
5. Configures the service to **auto-restart on failure**
6. Starts the service immediately
7. Creates a **desktop shortcut** pointing to `http://localhost:5000`
   (opens in the user's default browser — no separate app window)
8. Creates an uninstaller entry in Add/Remove Programs

### Post-install: add Azure credentials

After installing, edit `.env` in the install directory and add the Azure AD values, then restart the service:

```cmd
sc stop  ITInfinityMigrator
sc start ITInfinityMigrator
```

---

## Service management

```cmd
sc start   ITInfinityMigrator    # start
sc stop    ITInfinityMigrator    # stop
sc query   ITInfinityMigrator    # status
sc delete  ITInfinityMigrator    # remove (run installer to re-add)

# View logs
type "%PROGRAMDATA%\ITInfinityMigrator\service.log"
type "%PROGRAMDATA%\ITInfinityMigrator\server_stdout.log"
type "%PROGRAMDATA%\ITInfinityMigrator\server_stderr.log"
```

---

## Data locations (after install)

| Item | Path |
|------|------|
| Application files | `C:\Program Files\ITInfinityMigrator\` |
| Config / credentials | `C:\Program Files\ITInfinityMigrator\.env` |
| SQLite state DB | `C:\ProgramData\ITInfinityMigrator\migration_state.db` |
| Service log | `C:\ProgramData\ITInfinityMigrator\service.log` |
| Server stdout | `C:\ProgramData\ITInfinityMigrator\server_stdout.log` |
| Server stderr | `C:\ProgramData\ITInfinityMigrator\server_stderr.log` |

---

## Adding a new datasource

1. Create `datasources/mysystem_source.py`, extend `BaseDatasource`
2. Implement `name`, `display_name`, `role`, `_setup_configuration()`, `validate()`, `load()`, `test_connection()`
3. Register it in `SOURCE_REGISTRY` (or `TARGET_REGISTRY`) in `server.py`
4. The frontend `/api/datasources` endpoint will pick it up automatically
