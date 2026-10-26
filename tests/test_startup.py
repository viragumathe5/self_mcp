"""Launcher and shutdown regression tests. Sockets are mocked; no ports are opened."""
import asyncio
from contextlib import redirect_stdout
import errno
import io
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import start
from providers import ProviderError
from task_store import TaskStore
from task_runner import TaskRunner


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.socket_patch = patch.object(start.socket, 'socket')
        self.socket_factory = self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)
        self.socket = self.socket_factory.return_value.__enter__.return_value
        self.store_patch = patch('task_store.TaskStore')
        self.store = self.store_patch.start().return_value
        self.addCleanup(self.store_patch.stop)
        self.server_patch = patch.object(start, 'WorkspaceServer')
        self.server_class = self.server_patch.start()
        self.addCleanup(self.server_patch.stop)
        self.server = self.server_class.return_value
        self.server.started = True
        self.output = io.StringIO()

    def run_main(self):
        with redirect_stdout(self.output):
            return start.main()

    def test_port_owned_until_exit_and_no_reload_or_extra_worker(self):
        self.assertEqual(self.run_main(), 0)
        self.socket.bind.assert_called_once_with(('127.0.0.1', 8000))
        self.server.run.assert_called_once_with(sockets=[self.socket])
        self.socket_factory.return_value.__exit__.assert_called_once()
        config = self.server_class.call_args.args[0]
        self.assertEqual(config.timeout_graceful_shutdown, 5)
        self.assertEqual(config.workers, 1)
        self.assertFalse(config.reload)
        self.store.release_owner.assert_called_once()
        self.assertNotIn('Ready:', self.output.getvalue())

    def test_busy_port_does_not_touch_runner_or_stop_any_process(self):
        self.socket.bind.side_effect = OSError(errno.EADDRINUSE, 'Address already in use')
        self.assertEqual(self.run_main(), 1)
        self.store.acquire_owner.assert_not_called()
        self.server.run.assert_not_called()
        self.socket_factory.return_value.__exit__.assert_called_once()
        self.assertIn('No process was stopped', self.output.getvalue())

    def test_busy_runner_releases_reserved_port_and_reports_message(self):
        self.store.acquire_owner.side_effect = ProviderError(503, 'Another self_mcp process (PID 42) holds the lock.')
        self.assertEqual(self.run_main(), 1)
        self.server.run.assert_not_called()
        self.socket_factory.return_value.__exit__.assert_called_once()
        self.assertIn('PID 42', self.output.getvalue())
        self.assertNotIn('Ready:', self.output.getvalue())

    def test_failed_startup_returns_failure_and_releases_port(self):
        self.server.started = False
        self.assertEqual(self.run_main(), 1)
        self.socket_factory.return_value.__exit__.assert_called_once()

    def test_interrupt_and_exception_still_close_reserved_socket(self):
        for error in (KeyboardInterrupt(), RuntimeError('failed')):
            with self.subTest(error=type(error).__name__):
                self.socket_factory.return_value.__exit__.reset_mock()
                self.server.run.side_effect = error
                if isinstance(error, KeyboardInterrupt):
                    self.assertEqual(self.run_main(), 0)
                else:
                    with self.assertRaises(RuntimeError):
                        self.run_main()
                self.socket_factory.return_value.__exit__.assert_called_once()


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_mcp_stream_does_not_block_runner_cleanup_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.json'
            runner = TaskRunner(TaskStore(path))
            await runner.startup()
            server = start.WorkspaceServer(uvicorn.Config('api:app', timeout_graceful_shutdown=0.01))
            listener = Mock()
            listener.wait_closed = AsyncMock()
            connection = Mock()  # Deliberately never completes, like an idle MCP stream.
            server.servers = [listener]
            server.server_state.connections.add(connection)
            pending = asyncio.create_task(asyncio.Event().wait())
            server.server_state.tasks.add(pending)
            server.lifespan = Mock()
            server.lifespan.shutdown = AsyncMock(side_effect=runner.shutdown)
            bound_socket = Mock()
            try:
                with redirect_stdout(io.StringIO()), self.assertLogs('uvicorn.error', level='ERROR'):
                    await asyncio.wait_for(server.shutdown(sockets=[bound_socket]), 2)
                await asyncio.gather(pending, return_exceptions=True)
                listener.close.assert_called_once()
                bound_socket.close.assert_called_once()
                connection.shutdown.assert_called_once()
                self.assertTrue(pending.cancelled())
                server.lifespan.shutdown.assert_awaited_once()
                self.assertIsNone(runner.store._owner)
                # Next start can acquire the same lock, without deleting any lock file.
                other = TaskStore(path)
                other.acquire_owner()
                other.release_owner()
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                await runner.shutdown()

    async def test_ready_banner_only_follows_successful_startup(self):
        for ready in (False, True):
            server = start.WorkspaceServer(uvicorn.Config('api:app'))
            async def startup(sockets=None):
                server.started = ready
            output = io.StringIO()
            with patch.object(uvicorn.Server, 'startup', side_effect=startup), redirect_stdout(output):
                await server.startup()
            self.assertEqual('Ready:' in output.getvalue(), ready)

    async def test_ctrl_c_and_terminate_enter_graceful_shutdown(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            server = start.WorkspaceServer(uvicorn.Config('api:app'))
            with redirect_stdout(io.StringIO()):
                server.handle_exit(sig, None)
            self.assertTrue(server.should_exit)
            self.assertFalse(server.force_exit)


class OwnerDiagnosticsTests(unittest.TestCase):
    def test_active_owner_reports_pid_and_stale_file_does_not_block_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.json'
            first, second = TaskStore(path), TaskStore(path)
            first.acquire_owner()
            try:
                with self.assertRaises(ProviderError) as caught:
                    second.acquire_owner()
                self.assertIn(f'PID {os.getpid()}', caught.exception.message)
                self.assertIsNone(second._owner)
            finally:
                first.release_owner()
            # A PID on disk is informational, not a reason to reject or kill anything.
            path.with_suffix('.runner.lock').write_text('99999999')
            second.acquire_owner()
            second.release_owner()

    def test_permission_failure_is_distinguished_from_an_active_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / 'tasks.json')
            with patch('task_store.os.open', side_effect=PermissionError()), self.assertRaises(ProviderError) as caught:
                store.acquire_owner()
            self.assertIn('permissions', caught.exception.message)
            self.assertNotIn('Another self_mcp process', caught.exception.message)
