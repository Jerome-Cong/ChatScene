import importlib.util
from pathlib import Path
import signal
import subprocess
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[2] / "scripts/public_baseline_carla_session.py"
SPEC = importlib.util.spec_from_file_location("public_owned_session", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OwnedSessionTests(unittest.TestCase):
    def session(self):
        session = MODULE.OwnedCarlaSession({}, Path("unused-synthetic-directory"), 2)
        session.receipt = {"argv": ["synthetic-server", "-carla-port=2000"]}
        session.process = Mock(pid=12345, returncode=None)
        session.save = Mock()
        return session

    def test_identity_mismatch_never_signals_another_process(self):
        session = self.session()
        session.process.poll.return_value = None
        with patch.object(MODULE.Path, "read_bytes", return_value=b"other-process\0"), patch.object(MODULE.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "identity changed"):
                session.stop()
        kill.assert_not_called()
        self.assertFalse(session.receipt["process_stopped"])
        self.assertIn("identity changed", session.receipt["cleanup_error"])

    def test_term_timeout_rechecks_owned_identity_before_forced_cleanup(self):
        session = self.session()
        session.process.poll.side_effect = [None, -9]
        session.verify_identity = Mock(return_value=True)
        def wait(timeout):
            if timeout == 15:
                raise subprocess.TimeoutExpired("synthetic-server", timeout)
            session.process.returncode = -9
        session.process.wait.side_effect = wait
        with patch.object(MODULE.os, "kill") as kill:
            session.stop()
        self.assertEqual(session.verify_identity.call_count, 2)
        self.assertEqual(kill.call_args_list[0].args, (12345, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (12345, signal.SIGKILL))
        self.assertTrue(session.receipt["process_stopped"])
        self.assertTrue(session.receipt["forced_stop"])

    def test_zombie_empty_command_is_reaped_without_a_signal(self):
        session = self.session()
        session.process.poll.side_effect = [None, None, -11]
        session.process.wait.return_value = -11
        with patch.object(MODULE.Path, "read_bytes", return_value=b""), patch.object(
            MODULE.Path, "read_text", return_value="12345 (synthetic-server) Z 1"
        ), patch.object(MODULE.os, "kill") as kill:
            session.stop()
        kill.assert_not_called()
        session.process.wait.assert_called_once_with(timeout=1)
        self.assertTrue(session.receipt["process_stopped"])


if __name__ == "__main__":
    unittest.main()
