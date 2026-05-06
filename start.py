#!/usr/bin/env python3
"""
start.py — starts both the API server (port 8000) and MCP server (port 8001).
Usage: /Users/vumathe/miniforge3/envs/venv_mcp/bin/python start.py
"""
import subprocess
import sys
import signal
import socket
from pathlib import Path

SRC = Path(__file__).parent / "src"

# Always use the python that's running this script — ensures correct venv.
PYTHON = sys.executable


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def kill_port(port: int):
    """Kill whatever is sitting on a port before we try to bind it."""
    import os
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"], capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        for pid in pids:
            os.kill(int(pid), signal.SIGKILL)
        if pids:
            print(f"[self_mcp] Cleared stale process(es) on port {port}: {', '.join(pids)}")
    except Exception:
        pass


procs = []


def stop(sig, frame):
    print("\n[self_mcp] Shutting down...")
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    sys.exit(0)


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

# Clear stale processes on both ports before starting.
for port in (8000, 8001):
    if port_in_use(port):
        print(f"[self_mcp] Port {port} busy — clearing...")
        kill_port(port)

import time; time.sleep(0.5)  # brief wait for OS to release ports

print("[self_mcp] Starting API server  → http://localhost:8000")
print("[self_mcp] Starting MCP server  → http://localhost:8001/sse")
print("[self_mcp] GUI                  → http://localhost:8000")
print("[self_mcp] Press Ctrl+C to stop\n")

api = subprocess.Popen(
    [PYTHON, "-m", "uvicorn", "api:app",
     "--host", "0.0.0.0", "--port", "8000", "--reload"],
    cwd=SRC,
)
mcp = subprocess.Popen(
    [PYTHON, "mcp_server.py"],
    cwd=SRC,
)

procs = [api, mcp]

# Wait for either process to exit; if one dies, kill the other.
try:
    while True:
        if api.poll() is not None:
            print("[self_mcp] API server exited unexpectedly.")
            mcp.terminate()
            break
        if mcp.poll() is not None:
            print("[self_mcp] MCP server exited unexpectedly.")
            api.terminate()
            break
        time.sleep(1)
except KeyboardInterrupt:
    stop(None, None)
