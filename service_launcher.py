import os
import sys
import subprocess
import logging
import win32serviceutil
import win32service
import win32event
import servicemanager
 
BASE_DIR   = os.path.dirname(os.path.abspath(sys.executable
                              if getattr(sys, 'frozen', False) else __file__))
DATA_DIR   = os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"),
                           "ITInfinityMigrator")
LOG_FILE   = os.path.join(DATA_DIR, "service.log")
SERVER_EXE = os.path.join(BASE_DIR, "ITInfinityServer", "ITInfinityServer.exe")
 
os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ITInfinityService")
 
 
class ITInfinityMigratorService(win32serviceutil.ServiceFramework):
    _svc_name_         = "ITInfinityMigrator"
    _svc_display_name_ = "IT INFINITY Migration Tool"
    _svc_description_  = "Hosts the IT INFINITY dental imaging migration tool on http://localhost:5000"
 
    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._process    = None
 
    def SvcStop(self):
        logger.info("Stop requested.")
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:    self._process.wait(timeout=10)
            except: self._process.kill()
 
    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_, ""))
        logger.info("Service starting.")
 
        # Load .env
        env = os.environ.copy()
        env_path = os.path.join(BASE_DIR, ".env")
        if os.path.isfile(env_path):
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        env.setdefault(k.strip(), v.strip())
 
        while True:
            logger.info(f"Launching {SERVER_EXE}")
            try:
                self._process = subprocess.Popen(
                    [SERVER_EXE],
                    cwd=os.path.join(BASE_DIR, "ITInfinityServer"),
                    env=env,
                    stdout=open(os.path.join(DATA_DIR, "server_stdout.log"), "a"),
                    stderr=open(os.path.join(DATA_DIR, "server_stderr.log"), "a"),
                )
                logger.info(f"Server PID {self._process.pid}")
            except Exception as e:
                logger.error(f"Failed to launch server: {e}")
                if win32event.WaitForSingleObject(self._stop_event, 10000) == win32event.WAIT_OBJECT_0:
                    return
                continue
 
            while True:
                rc = win32event.WaitForSingleObject(self._stop_event, 2000)
                if rc == win32event.WAIT_OBJECT_0:
                    return
                if self._process.poll() is not None:
                    logger.warning(f"Server exited ({self._process.returncode}), restarting in 5s…")
                    if win32event.WaitForSingleObject(self._stop_event, 5000) == win32event.WAIT_OBJECT_0:
                        return
                    break
 
 
if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ITInfinityMigratorService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(ITInfinityMigratorService)