import unittest
from unittest.mock import Mock, patch

from cec_ha_bridge import Bridge, WebhookClient


class ConnectionTest(unittest.TestCase):
    def response(self, close=False):
        response = Mock(status=200, will_close=close)
        response.read.return_value = b""
        return response

    @patch("cec_ha_bridge.http.client.HTTPConnection")
    def test_requests_in_a_burst_reuse_the_connection(self, connection):
        connection.return_value.getresponse.return_value = self.response()
        client = WebhookClient("http://example.invalid:8123/private-webhook")
        self.assertEqual(client.post({"button": "volume_up"}), (200, False))
        self.assertEqual(client.post({"button": "volume_down"}), (200, True))
        self.assertEqual(connection.call_count, 1)
        self.assertEqual(connection.return_value.request.call_count, 2)
        client.close()

    @patch("cec_ha_bridge.http.client.HTTPConnection")
    def test_ambiguous_failure_is_not_replayed(self, connection):
        first, second = Mock(), Mock()
        first.getresponse.side_effect = TimeoutError()
        second.getresponse.return_value = self.response()
        connection.side_effect = [first, second]
        client = WebhookClient("http://example.invalid/private-webhook")
        with self.assertRaises(TimeoutError):
            client.post({"button": "volume_up"})
        self.assertEqual(first.request.call_count, 1)
        first.close.assert_called_once()
        self.assertEqual(client.post({"button": "volume_down"}), (200, False))
        self.assertEqual(second.request.call_count, 1)
        client.close()

    @patch("cec_ha_bridge.http.client.HTTPConnection")
    def test_idle_connection_is_retired_before_a_new_burst(self, connection):
        connection.return_value.getresponse.return_value = self.response()
        client = WebhookClient("http://example.invalid/private-webhook")
        with patch("cec_ha_bridge.time.monotonic", return_value=100):
            client.post({})
        with patch("cec_ha_bridge.time.monotonic", return_value=111):
            self.assertEqual(client.post({}), (200, False))
        self.assertEqual(connection.call_count, 2)
        client.close()

    @patch("cec_ha_bridge.http.client.HTTPConnection")
    def test_server_close_and_http_failure_discard_connection(self, connection):
        response = self.response(close=True)
        connection.return_value.getresponse.return_value = response
        client = WebhookClient("http://example.invalid/private-webhook")
        client.post({})
        self.assertIsNone(client.connection)
        response.status = 503
        with self.assertRaises(Exception):
            client.post({})
        self.assertIsNone(client.connection)
        self.assertEqual(connection.return_value.request.call_count, 2)


class ExpiryTest(unittest.TestCase):
    def setUp(self):
        with patch("cec_ha_bridge.C.CDLL", return_value=Mock()):
            self.bridge = Bridge({})
        self.bridge.apples = {8}
        self.client = Mock()
        self.client.post.return_value = (200, True)

    def item(self, repeat):
        return {"button": "volume_up", "sequence": "test:1", "repeat": repeat}

    def test_stall_discards_repeat_but_preserves_a_queued_tap(self):
        with patch("cec_ha_bridge.time.monotonic", return_value=10.5):
            self.assertFalse(self.bridge.deliver_one(self.client, 10, 10, self.item(True)))
            self.client.post.assert_not_called()
            self.assertTrue(self.bridge.deliver_one(self.client, 10, 10, self.item(False)))
        self.assertEqual(self.client.post.call_count, 1)

    def test_tap_older_than_one_second_is_still_discarded(self):
        with patch("cec_ha_bridge.time.monotonic", return_value=11.1):
            self.assertFalse(self.bridge.deliver_one(self.client, 10, 10, self.item(False)))
        self.client.post.assert_not_called()

    def test_callback_backlog_counts_toward_repeat_age(self):
        with patch("cec_ha_bridge.time.monotonic", return_value=10):
            self.bridge.press(8, 0x41)
        self.bridge.buttons.get_nowait()
        with patch("cec_ha_bridge.time.monotonic", return_value=10.7):
            self.bridge.handle(4, 0, bytes.fromhex("85 44 41"), 10.25)
            item = self.bridge.buttons.get_nowait()
            self.assertTrue(item[2]["repeat"])
            self.assertFalse(self.bridge.deliver_one(self.client, *item))
        self.client.post.assert_not_called()

    def test_fresh_repeat_carries_age_for_ha_queue_expiry(self):
        body = self.item(True)
        with patch("cec_ha_bridge.time.monotonic", return_value=10.1):
            self.assertTrue(self.bridge.deliver_one(self.client, 10, 10.02, body))
        self.assertEqual(body["age_ms"], 100)


if __name__ == "__main__":
    unittest.main()
