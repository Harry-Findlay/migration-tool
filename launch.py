"""
launch.py
=========
Windows launcher — starts the Flask server and opens the browser.
This is what the desktop shortcut runs.

Usage (from the installer-created shortcut):
    pythonw launch.py
or:
    python launch.py
"""

import os
import sys
import time
import subprocess
import threading
import webbrowser
import socket

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_URL = "http://localhost:5000"


def _load_dotenv():
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_for_server(port: int, timeout: int = 15) -> bool:
    for _ in range(timeout * 2):
        if _port_open(port):
            return True
        time.sleep(0.5)
    return False


def _start_server():
    server_script = os.path.join(BASE_DIR, "server.py")
    python = sys.executable

    # On Windows: hide the console window by using DETACHED_PROCESS
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [python, server_script],
        cwd=BASE_DIR,
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


if __name__ == "__main__":
    _load_dotenv()

    port = int(os.environ.get("PORT", 5000))

    # If server already running, just open browser
    if _port_open(port):
        webbrowser.open(SERVER_URL)
        sys.exit(0)

    # Start server in background
    proc = _start_server()

    # Wait for it to be ready then open the browser
    if _wait_for_server(port, timeout=20):
        webbrowser.open(SERVER_URL)
    else:
        # Show error if server didn't start
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "IT INFINITY Migration Tool",
            f"Server failed to start on port {port}.\n\n"
            f"Check the log at:\n"
            f"%USERPROFILE%\\.config\\ITInfinityMigrator\\server.log"
        )
        root.destroy()
        proc.terminate()
        sys.exit(1)
