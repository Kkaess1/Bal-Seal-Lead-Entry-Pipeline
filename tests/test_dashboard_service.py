"""
The dashboard runs as an always-on logon task, so the things that keep a
long-lived server sane are pinned here: exactly one instance, and a log to
read when it dies at 3am behind a windowless pythonw.exe.
"""

import socket
import unittest
from pathlib import Path

from dashboard import serve


class SingleInstanceTests(unittest.TestCase):
    def test_an_occupied_port_is_detected(self):
        # Windows lets a second server bind an in-use port, so this check --
        # not the bind -- is what stops two dashboards serving at once.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]

            self.assertTrue(serve.already_serving('127.0.0.1', port))

    def test_a_free_port_is_free(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        # Socket closed: nothing is listening there now.
        self.assertFalse(serve.already_serving('127.0.0.1', port))


class ServeEntryPointTests(unittest.TestCase):
    def test_project_root_is_importable_when_run_by_path(self):
        # The logon task runs serve.py by absolute path, with no working
        # directory of its own -- `-m dashboard.serve` would not survive that.
        self.assertIn(str(serve.ROOT), __import__('sys').path)
        self.assertTrue((serve.ROOT / 'config.json').is_file())


class SchedulerSetupTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent.parent
        self.script = (root / 'setup_scheduler.bat').read_text(
            encoding='utf-8', errors='replace'
        )
        self.task_script = (root / 'setup_dashboard_task.ps1').read_text(
            encoding='utf-8', errors='replace'
        )

    def test_dashboard_starts_at_logon(self):
        self.assertIn('LeadPipeline_Dashboard', self.script)
        self.assertIn('New-ScheduledTaskTrigger -AtLogOn', self.task_script)

    def test_dashboard_runs_windowless_and_by_path(self):
        # pythonw so no console window; serve.py by path so no cwd dependency.
        self.assertIn('.venv\\Scripts\\pythonw.exe', self.task_script)
        self.assertIn('dashboard\\serve.py', self.task_script)

    def test_dashboard_restarts_and_ignores_duplicate_triggers(self):
        self.assertIn('-RestartCount 3', self.task_script)
        self.assertIn('-RestartInterval', self.task_script)
        self.assertIn('-MultipleInstances IgnoreNew', self.task_script)
        self.assertIn('-Minutes 10', self.task_script)


if __name__ == '__main__':
    unittest.main()
