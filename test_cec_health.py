import os
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from cec_ha_bridge import HealthSupervisor, ReadinessMonitor


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.health = HealthSupervisor(self.stop)
        self.worker = Mock()
        self.worker.is_alive.return_value = True
        with patch('cec_ha_bridge.time.monotonic', return_value=100):
            self.health.register('volume', self.worker)

    def test_idle_loop_progress_stays_healthy_without_cec_traffic(self):
        with patch('cec_ha_bridge.time.monotonic', return_value=114):
            self.health.beat('volume')
        with patch('cec_ha_bridge.time.monotonic', return_value=128):
            self.assertIsNone(self.health.failure())

    def test_alive_but_stuck_worker_is_unhealthy(self):
        with patch('cec_ha_bridge.time.monotonic', return_value=116):
            self.assertIn('no progress', self.health.failure())

    def test_dead_worker_is_unhealthy_even_with_recent_progress(self):
        self.worker.is_alive.return_value = False
        with patch('cec_ha_bridge.time.monotonic', return_value=101):
            self.assertEqual(self.health.failure(), 'volume worker exited')

    def test_unhealthy_supervisor_exits_without_feeding_watchdog(self):
        self.worker.is_alive.return_value = False
        with patch('cec_ha_bridge.os._exit', side_effect=SystemExit) as exit_process:
            with patch.object(self.health, 'notify') as notify:
                with self.assertRaises(SystemExit):
                    self.health.run()
                exit_process.assert_called_once_with(1)
                notify.assert_not_called()

    def test_stopped_supervisor_does_not_restart(self):
        self.stop.set()
        with patch('cec_ha_bridge.os._exit') as exit_process:
            self.health.run()
        exit_process.assert_not_called()

    def test_readiness_reports_progress_through_failed_probes(self):
        progress = Mock()
        monitor = ReadinessMonitor(dict(tv_host='127.0.0.1', readiness_timeout=.03,
                                        readiness_interval=.002), self.stop, progress)
        import time
        with patch('cec_ha_bridge.socket.create_connection', side_effect=OSError):
            monitor.check(Mock(), 8, time.monotonic())
        self.assertGreater(progress.call_count, 1)

    def test_systemd_notification_uses_real_unix_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            address = directory + '/notify'
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
                server.bind(address)
                server.settimeout(1)
                with patch.dict(os.environ, NOTIFY_SOCKET=address):
                    self.health.notify()
                self.assertEqual(server.recv(128), b'WATCHDOG=1')

    def test_stale_main_loop_is_unhealthy_even_if_volume_is_healthy(self):
        with patch('cec_ha_bridge.time.monotonic', return_value=100):
            self.health.register('cec', self.worker)
        with patch('cec_ha_bridge.time.monotonic', return_value=116):
            self.health.beat('volume')
            self.assertIn('cec worker', self.health.failure())


if __name__ == '__main__':
    unittest.main()
