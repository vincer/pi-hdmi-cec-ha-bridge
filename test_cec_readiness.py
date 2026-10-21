import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from cec_ha_bridge import Bridge, ReadinessMonitor


class ReadinessTest(unittest.TestCase):
    def monitor(self, **kw):
        cfg = dict(tv_host='127.0.0.1', tv_port=3000, readiness_timeout=.04,
                   readiness_interval=.005, ready_webhook_url='http://example.invalid/ready')
        cfg.update(kw)
        return ReadinessMonitor(cfg, threading.Event())

    def test_any_external_active_source_but_no_outgoing_or_unicast(self):
        with patch('cec_ha_bridge.C.CDLL', return_value=Mock()):
            bridge = Bridge({})
        bridge.readiness = Mock()
        for source in (0, 4, 8, 11):
            bridge.handle(2, 0, bytes([source << 4 | 15, 0x82, 0x10, 0]), 123)
        self.assertEqual([c[0] for c in bridge.readiness.request.call_args_list],
                         [(s, 123) for s in (0, 4, 8, 11)])
        bridge.readiness.reset_mock()
        for reason, raw in [(1,'8f821000'), (2,'85821000'), (2,'5f824000'),
                            (2,'ff821000'), (2,'8f82'), (2,'8f82ffff'), (2,'8f871000')]:
            bridge.handle(reason, 0, bytes.fromhex(raw), 123)
        bridge.readiness.request.assert_not_called()

    def test_ready_port_sends_exactly_one_notification(self):
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0)); server.listen()
            monitor = self.monitor(tv_port=server.getsockname()[1])
            client = Mock(); client.post.return_value = (200, False)
            monitor.check(client, 4, time.monotonic())
            client.post.assert_called_once()
            self.assertEqual(client.post.call_args[0][0]['event'], 'webos_ready')
            self.assertEqual(client.post.call_args[0][0]['source'], 4)

    @patch('cec_ha_bridge.socket.create_connection', side_effect=OSError())
    def test_unreachable_port_expires_without_webhook(self, connect):
        monitor = self.monitor(); client = Mock()
        monitor.check(client, 8, time.monotonic())
        client.post.assert_not_called()
        self.assertGreater(connect.call_count, 1)

    def test_shutdown_and_stale_hint_do_not_probe(self):
        monitor = self.monitor(); client = Mock()
        with patch('cec_ha_bridge.socket.create_connection') as connect:
            monitor.check(client, 8, time.monotonic()-1)
            monitor.stop.set()
            monitor.check(client, 8, time.monotonic())
            connect.assert_not_called()
            client.post.assert_not_called()

    @patch('cec_ha_bridge.WebhookClient')
    def test_pending_hints_coalesce_and_failed_delivery_allows_future_check(self, factory):
        monitor = self.monitor()
        monitor.check = Mock(side_effect=RuntimeError('failure'))
        monitor.request(4, time.monotonic()); monitor.request(8, time.monotonic())
        self.assertEqual(monitor.requests.qsize(), 1)
        worker=threading.Thread(target=monitor.run);worker.start()
        try:
            deadline=time.monotonic()+1
            while monitor.pending and time.monotonic()<deadline:time.sleep(.005)
            self.assertFalse(monitor.pending)
            monitor.request(8,time.monotonic())
            self.assertTrue(monitor.requests.empty())
            monitor.next_allowed=0
            monitor.request(8,time.monotonic())
        finally:
            monitor.stop.set();worker.join(1)
        self.assertFalse(worker.is_alive())
        factory.return_value.close.assert_called()

if __name__=='__main__':unittest.main()
