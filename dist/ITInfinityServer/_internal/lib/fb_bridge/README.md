# Imaging Data Migrator

A Python desktop application for migrating patient imaging data between imaging software systems.

## Current Support
- **Source**: VistaSoft (reads from VistaSoftData folder + Firebird database)
- **Target**: DTX Studio (writes via DTX Studio Core REST API)

## Requirements

- Python 3.10+
- `customtkinter` — modern Tkinter UI framework
- `Pillow` — image support

### Install dependencies

```bash
pip install customtkinter Pillow
```

On Linux, also install `python3-tk`:

```bash
sudo apt-get install python3-tk
```

## Running the App

```bash
cd imaging_migrator
python app.py
```

## Project Structure

```
imaging_migrator/
├── app.py                      # Main UI entry point
├── core/
│   ├── models.py               # ConfigurationItem, MigrationResult, enums
│   ├── base_datasource.py      # Abstract base class for all datasources
│   └── engine.py               # Migration orchestration (runs in background thread)
├── datasources/
│   ├── vistasoft.py            # VistaSoft source datasource
│   └── dtxstudio.py            # DTX Studio target datasource
└── ui/                         # (reserved for future UI components)
```

## Adding a New Datasource

1. Create a new file in `datasources/`, e.g. `datasources/my_system.py`
2. Subclass `BaseDatasource` from `core.base_datasource`
3. Implement:
   - `name` — unique identifier string
   - `display_name` — human-readable name
   - `role` — `"source"` or `"target"`
   - `_setup_configuration()` — populate `self.configuration` with `ConfigurationItem` objects
   - `validate()` — return `(bool, message)`
   - `read_patients()` (sources) or `write_patients()` (targets)

4. Register it in `app.py` where source/target are instantiated.

## Replacing Stub Logic

Both datasource files contain stub implementations for data reading/writing.
Replace the stub bodies in:

- `VistaSoftDatasource.read_patients()` — connect to the Firebird `.FDB` database using `fdb` or `firebird-driver` library
- `DTXStudioDatasource.test_connection()` — make an HTTP GET to `{CoreUrl}/api/version` and parse the version
- `DTXStudioDatasource.write_patients()` — POST each patient to the DTX Studio Core REST API
