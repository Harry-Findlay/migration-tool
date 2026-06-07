"""
service.py
==========
Windows Service wrapper for the IT INFINITY Migration Tool server.

The service:
  - Starts automatically at Windows boot (before any user logs in)
  - Runs the Flask server on http://localhost:5000
  - Restarts automatically on failure (configured by the installer)
  - Writes logs to %PROGRAMDATA%\ITInfinityMigrator\service.log

Installation (done by the installer — not run manually):
    python service.py install
    python service.py start

Manual control:
    python service.py stop
    python service.py remove
    sc query ITInfinityMigrator
"""

import os
import sys
import logging
import subprocess
import time

# pywin32 — installed with the app
import win32serviceutil
import win32service
import win32event
import servicemanager

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"),
                           "ITInfinityMigrator")
LOG_FILE    = os.path.join(DATA_DIR, "service.log")
SERVER_PY   = os.path.join(BASE_DIR, "server.py")
PYTHON_EXE  = sys.executable   # same Python that installed the app

os.makedirs(DATA_DIR, exist_ok=True)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("ITInfinityService")


class ITInfinityMigratorService(win32serviceutil.ServiceFramework):
    """Windows service that hosts the Flask migration tool server."""

    _svc_name_         = "ITInfinityMigrator"
    _svc_display_name_ = "IT INFINITY Migration Tool"
    _svc_description_  = (
        "Hosts the IT INFINITY dental imaging migration tool web server "
        "on http://localhost:5000"
    )

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._process    = None

    def SvcStop(self):
        logger.info("Service stop requested.")
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            logger.info("Server process terminated.")

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        logger.info("IT INFINITY Migration Tool service starting.")
        self._run()

    def _run(self):
        env = os.environ.copy()

        # Load .env file from the app directory so Azure credentials are available
        env_path = os.path.join(BASE_DIR, ".env")
        if os.path.isfile(env_path):
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        env.setdefault(key.strip(), val.strip())
            logger.info(f"Loaded environment from {env_path}")
        else:
            logger.warning(f".env file not found at {env_path}")

        while True:
            logger.info(f"Starting server: {PYTHON_EXE} {SERVER_PY}")
            try:
                self._process = subprocess.Popen(
                    [PYTHON_EXE, SERVER_PY],
                    cwd=BASE_DIR,
                    env=env,
                    stdout=open(os.path.join(DATA_DIR, "server_stdout.log"),
                                "a", encoding="utf-8"),
                    stderr=open(os.path.join(DATA_DIR, "server_stderr.log"),
                                "a", encoding="utf-8"),
                )
                logger.info(f"Server started with PID {self._process.pid}")
            except Exception as exc:
                logger.exception(f"Failed to start server: {exc}")
                # Wait before retrying
                if win32event.WaitForSingleObject(
                        self._stop_event, 10_000) == win32event.WAIT_OBJECT_0:
                    break
                continue

            # Poll: wait for process to exit OR stop event
            while True:
                rc = win32event.WaitForSingleObject(self._stop_event, 2_000)
                if rc == win32event.WAIT_OBJECT_0:
                    # Stop was requested
                    logger.info("Stop event received, exiting service loop.")
                    return
                if self._process.poll() is not None:
                    exit_code = self._process.returncode
                    logger.warning(
                        f"Server process exited with code {exit_code}. "
                        "Restarting in 5 seconds…"
                    )
                    time.sleep(5)
                    break   # inner loop → restart outer loop


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Called by SCM at service start
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ITInfinityMigratorService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(ITInfinityMigratorService)
