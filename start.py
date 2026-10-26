#!/usr/bin/env python3
"""Run the workspace, API and project MCP endpoints together on port 8000."""
import socket
import sys
from pathlib import Path

import uvicorn

HOST = "127.0.0.1"
PORT = 8000
SHUTDOWN_GRACE_SECONDS = 5


class WorkspaceServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        # A banner before lifespan startup used to imply success when the lock failed.
        if self.started:
            print(f"[self_mcp] Ready: workspace + API + MCP → http://{HOST}:{PORT}", flush=True)
            print("[self_mcp] Keep this terminal open. Ctrl+C stops the service and releases the port.", flush=True)

    def handle_exit(self, sig, frame):
        if not self.should_exit:
            print("\n[self_mcp] Stopping; closing MCP connections and saving task state…", flush=True)
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets=None):
        # Bound uvicorn's connection drain: a long-lived MCP stream must not prevent
        # the ASGI lifespan from cancelling jobs and releasing the runner lease.
        await super().shutdown(sockets=sockets)
        if not self.force_exit:
            print("[self_mcp] Stopped. Port 8000 and the task-runner lock are released.", flush=True)


def main():
    if sys.version_info < (3, 12):
        raise SystemExit("Python 3.12+ is required. Use .venv/bin/python start.py.")
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from providers import ProviderError
    from task_store import TaskStore

    # Reserve the port before application startup, then keep ownership until exit.
    # The context manager closes it even if startup fails or Ctrl+C interrupts it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((HOST, PORT))
        except OSError as error:
            print(f"[self_mcp] Cannot use port {PORT}: {error.strerror or error}.", flush=True)
            print("[self_mcp] Stop the existing service with Ctrl+C, then try again. No process was stopped.", flush=True)
            return 1

        # Distinguish a closed port from an old process still draining connections.
        # This is only a preflight; the runner takes and owns its lease at startup.
        store = TaskStore()
        try:
            store.acquire_owner()
        except ProviderError as error:
            print(f"[self_mcp] Cannot start: {error.message}", flush=True)
            return 1
        finally:
            store.release_owner()

        config = uvicorn.Config("api:app", host=HOST, port=PORT, log_level="warning",
                                lifespan="on", timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS)
        server = WorkspaceServer(config)
        print("[self_mcp] Starting workspace + MCP…", flush=True)
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            pass
        return 0 if server.started else 1


if __name__ == "__main__":
    raise SystemExit(main())
