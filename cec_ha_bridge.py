#!/usr/bin/env python3
"""Bridge Apple TV CEC volume buttons to Home Assistant's LG webOS control.

On the tested LG E6, listening passively was insufficient: Apple TV only sent
HDMI volume commands once an audio receiver reported System Audio Mode active.
We impersonate that receiver for Apple TV, but report audio mode off to the TV
so its built-in speakers remain usable. HA performs the actual volume changes.
This deliberately uses the original Pi's legacy VideoCore API; newer Linux CEC
backends need a port, not just a different shared-library path.
"""
import argparse
import ctypes as C
import http.client
import json
import logging
import os
import queue
import signal
import socket
import struct
import threading
import time
import urllib.parse
import uuid

LOG = logging.getLogger("cec-ha")
APPLE_VENDOR = bytes.fromhex("0010fa")
KEYS = {0x41: "volume_up", 0x42: "volume_down", 0x43: "mute"}


class HealthSupervisor:
    """Only feed systemd's watchdog while every required loop makes progress."""

    def __init__(self, stop, timeout=15):
        self.stop, self.timeout = stop, timeout
        self.lock = threading.Lock()
        self.workers = {}

    def register(self, name, thread):
        with self.lock:
            self.workers[name] = [thread, time.monotonic()]

    def beat(self, name):
        with self.lock:
            self.workers[name][1] = time.monotonic()

    def failure(self):
        now = time.monotonic()
        with self.lock:
            for name, (thread, last_progress) in self.workers.items():
                if not thread.is_alive():
                    return name + " worker exited"
                if now - last_progress > self.timeout:
                    return "%s worker made no progress for %s seconds" % (name, self.timeout)
        return None

    @staticmethod
    def notify():
        address = os.environ.get("NOTIFY_SOCKET")
        if not address:
            return
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.settimeout(0.5)
            channel.connect(address)
            channel.sendall(b"WATCHDOG=1")

    def run(self):
        while not self.stop.is_set():
            failure = self.failure()
            if failure:
                LOG.critical("Bridge unhealthy: %s; exiting for systemd recovery", failure)
                # A blocked CEC call or resolver cannot be cancelled safely in
                # Python. Exit the whole process instead of starting a second
                # delivery worker that could duplicate an in-flight command.
                # systemd restarts it after 10 s; no queued input is persisted.
                os._exit(1)
            self.notify()
            self.stop.wait(1)


class WebhookClient:
    """Reuse one connection per burst, without retrying ambiguous POSTs."""

    def __init__(self, url):
        self.url = urllib.parse.urlsplit(url)
        if self.url.scheme not in ("http", "https") or not self.url.hostname:
            raise ValueError("Invalid webhook configuration")
        self.path = urllib.parse.urlunsplit(("", "", self.url.path or "/", self.url.query, ""))
        self.connection = None
        self.last_used = 0

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def post(self, body):
        # Retire idle connections before a new burst instead of relying on a
        # server's idle timeout. A failed POST is never replayed automatically.
        # HA may have applied a volume step even if its response was lost;
        # retrying that POST could apply the step (or toggle mute) twice.
        if time.monotonic() - self.last_used > 10:
            self.close()
        reused = self.connection is not None
        if not reused:
            cls = (http.client.HTTPSConnection if self.url.scheme == "https"
                   else http.client.HTTPConnection)
            self.connection = cls(self.url.hostname, self.url.port, timeout=0.8)
        try:
            self.connection.request("POST", self.path, json.dumps(body).encode(),
                                    {"Content-Type": "application/json"})
            response = self.connection.getresponse()
            status = response.status
            if len(response.read(4097)) > 4096:
                raise http.client.HTTPException("Oversized webhook response")
            if not 200 <= status < 300:
                raise http.client.HTTPException("Webhook rejected request")
            self.last_used = time.monotonic()
            if response.will_close:
                self.close()
            return status, reused
        except Exception:
            self.close()
            raise


class ReadinessMonitor:
    """Coalesce CEC hints and probe webOS independently of volume delivery."""

    def __init__(self, config, stop, progress=None):
        self.config, self.stop = config, stop
        self.progress = progress or (lambda: None)
        self.requests = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.pending = False
        self.next_allowed = 0

    def request(self, source, received):
        now = time.monotonic()
        with self.lock:
            if self.pending or now < self.next_allowed or self.stop.is_set():
                return
            self.pending = True
            self.requests.put_nowait((source, received))
        LOG.info("WebOS readiness check requested source=%x", source)

    def check(self, client, source, received):
        # A visible picture/CEC wake does not mean webOS is accepting connections.
        # Poll its port for a bounded period, then ask HA to refresh the native
        # integration rather than waiting for HA's normal reconnect interval.
        # TCP reachability is only a hint: HA still owns pairing and connection.
        deadline = received + self.config.get("readiness_timeout", 30)
        host = self.config["tv_host"]
        port = self.config.get("tv_port", 3000)
        while not self.stop.is_set():
            # A normal 30 s readiness search is healthy if each probe returns.
            # A resolver/connect/POST stuck for 15 s is not.
            self.progress()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                LOG.info("WebOS readiness check timed out source=%x", source)
                return
            try:
                with socket.create_connection((host, port), timeout=min(0.2, remaining)):
                    pass
            except OSError:
                self.stop.wait(min(self.config.get("readiness_interval", 0.25),
                                   max(0, deadline - time.monotonic())))
                continue
            if self.stop.is_set():
                return
            elapsed = round((time.monotonic() - received) * 1000, 1)
            status, _ = client.post({"sender": "pi-cec", "event": "webos_ready",
                                     "source": source, "elapsed_ms": elapsed})
            LOG.info("WebOS reachable source=%x elapsed_ms=%s webhook_HTTP=%d",
                     source, elapsed, status)
            return

    def run(self):
        client = WebhookClient(self.config["ready_webhook_url"])
        try:
            while not self.stop.is_set():
                self.progress()
                try:
                    source, received = self.requests.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    self.check(client, source, received)
                except Exception as exc:
                    LOG.warning("WebOS readiness delivery failed: %s", type(exc).__name__)
                finally:
                    client.close()
                    with self.lock:
                        self.pending = False
                        # Devices can announce Active Source repeatedly. One
                        # probe at a time plus a cooldown avoids refresh storms.
                        self.next_allowed = time.monotonic() + 5
        finally:
            client.close()


class Bridge:
    def __init__(self, config):
        self.config = config
        # Keep firmware callbacks and network I/O separate. A small button queue
        # bounds backlog during a slow HA/TV response; age checks below prevent
        # held buttons from continuing to change volume after release.
        self.events = queue.Queue(maxsize=256)
        self.buttons = queue.Queue(maxsize=4)
        self.stop = threading.Event()
        self.health = HealthSupervisor(self.stop)
        self.readiness = (ReadinessMonitor(config, self.stop,
                                          lambda: self.health.beat("readiness"))
                          if config.get("ready_webhook_url") and config.get("tv_host")
                          else None)
        self.apples = set()
        self.physical = 0xFFFF
        self.session = uuid.uuid4().hex
        self.sequence = 0
        self.last_key = None
        self.last_press = 0
        self.hold_source = None
        self.hold_start = 0
        self.last_seen = 0
        self.lib = C.CDLL("/opt/vc/lib/libbcm_host.so")
        cb_type = C.CFUNCTYPE(None, C.c_void_p, *([C.c_uint32] * 5))
        # Retain this ctypes callback for the entire firmware registration;
        # letting Python collect it would leave firmware with a dangling pointer.
        self.callback = cb_type(self.receive)
        self.lib.vc_cec_register_callback.argtypes = [cb_type, C.c_void_p]
        self.lib.vc_cec_set_logical_address.argtypes = [C.c_int, C.c_int, C.c_uint32]
        self.lib.vc_cec_send_message.argtypes = [C.c_uint32, C.c_void_p, C.c_uint32, C.c_int]
        self.lib.vc_cec_get_physical_address.argtypes = [C.POINTER(C.c_uint16)]

    def receive(self, context, header, p0, p1, p2, p3):
        # VideoCore packs reason/length into the header and frame bytes into
        # four little-endian words. Timestamp here so later queue delays count
        # toward expiry rather than making old input look freshly received.
        size = min((header >> 16) & 255, 16)
        event = (header & 65535, header >> 24,
                 struct.pack("<IIII", p0, p1, p2, p3)[:size], time.monotonic())
        try:
            self.events.put_nowait(event)
        except queue.Full:
            LOG.warning("CEC event queue full; dropping frame")

    def send(self, target, payload, reply=True):
        rc = self.lib.vc_cec_send_message(target, payload, len(payload), int(reply))
        if rc:
            LOG.warning("CEC transmit rejected rc=%d opcode=%02x", rc, payload[0])

    def advertise(self):
        # Report Physical Address / Device Vendor ID, never Active Source.
        # Advertising the bridge must not steal the TV's selected input.
        self.send(15, b"\x84" + self.physical.to_bytes(2, "big") + b"\x05", False)
        self.send(15, bytes.fromhex("87 18 c0 86"), False)

    def discover(self):
        # Playback addresses are reassigned when devices reconnect.
        for address in (4, 8, 11):
            self.send(address, b"\x8c", False)

    def press(self, source, key, received=None):
        if source not in self.apples or key not in KEYS:
            return
        handled = time.monotonic()
        now = handled if received is None else received
        # Use CEC event timestamps, not worker timing, to distinguish a hold.
        # The gap limit also recovers if a release callback is missed.
        same_hold = (self.last_key == key and self.hold_source == source
                     and now - self.last_seen <= 0.6)
        if not same_hold:
            self.hold_source, self.hold_start = source, now
        self.last_seen = now
        if self.last_key == key:
            # Mute is a toggle: repeated presses from a hold must not oscillate.
            if key == 0x43 and now - self.last_press < 1.5:
                return
            if now - self.last_press < self.config.get("repeat_interval", 0.15):
                return
        steps = 1
        if key in (0x41, 0x42) and same_hold:
            # Accelerate via counted HA steps, preserving precise single taps.
            # The example config uses 1.25 s after testing; 0.75 s remains the
            # original fallback for configurations that omit this setting.
            if now - self.hold_start >= self.config.get("hold_acceleration_after", 0.75):
                steps = max(1, min(3, int(self.config.get("hold_acceleration_steps", 2))))
        self.last_key, self.last_press = key, now
        self.sequence += 1
        # Session UUID + counter lets HA traces correlate with Pi logs across
        # restarts. This is diagnostic metadata, not a retry/deduplication layer.
        item = (now, handled, {"sender": "pi-cec", "button": KEYS[key],
                      "source": source, "frame": "%x5:44:%02x" % (source, key),
                      "sequence": "%s:%d" % (self.session, self.sequence),
                      "steps": steps, "repeat": same_hold and key != 0x43})
        try:
            self.buttons.put_nowait(item)
            LOG.info("Button %s source=%x steps=%d repeat=%s sequence=%s callback_ms=%d",
                     KEYS[key], source, steps, item[2]["repeat"], item[2]["sequence"],
                     (handled - now) * 1000)
        except queue.Full:
            LOG.warning("Button queue full; dropping stale input")

    def handle(self, reason, rc, raw, received=None):
        # Legacy callback reasons: 2 = received message, 4 = button pressed,
        # 8 = button released, 32768 = logical-address loss. Specialized button
        # callbacks and ordinary frames both go through the same repeat limiter.
        if reason == 32768:
            raise RuntimeError("CEC logical address lost")
        if len(raw) < 2:
            return
        source, destination = raw[0] >> 4, raw[0] & 15
        opcode = raw[1]
        if reason in (2, 4) and destination == 5 and opcode == 0x44 and len(raw) >= 3:
            self.press(source, raw[2], received)
            return
        if reason == 8 and destination == 5 and source in self.apples:
            self.last_key = None
            return
        if reason != 2:
            return
        if (self.readiness is not None and destination == 15 and source not in (5, 15)
                and opcode == 0x82 and len(raw) == 4 and raw[2:] != b"\xff\xff"):
            # Any external source (including PS5) may wake/select the TV. This
            # only starts a readiness probe; it never selects an input or powers
            # a device, and does not buffer/replay early volume commands.
            self.readiness.request(source, time.monotonic() if received is None else received)
        if opcode == 0x87 and len(raw) >= 5:
            if raw[2:5] == APPLE_VENDOR:
                new = source not in self.apples
                self.apples.add(source)
                if new:
                    LOG.info("Discovered Apple TV at logical address %x", source)
                # Report System Audio Mode ON directly to Apple TV only. This
                # was necessary in testing to make it send volume buttons.
                self.send(source, bytes.fromhex("7e 01"))
            else:
                self.apples.discard(source)
        if destination != 5:
            return
        identity = {
            # Physical address (audio device type 5), vendor, OSD name,
            # CEC version 1.4 and power status ON identify the virtual receiver.
            0x83: (15, b"\x84" + self.physical.to_bytes(2, "big") + b"\x05"),
            0x8C: (15, bytes.fromhex("87 18 c0 86")),
            0x46: (source, b"\x47Pi Volume"),
            0x9F: (source, bytes.fromhex("9e 05")),
            0x8F: (source, bytes.fromhex("90 00")),
            # Only discovered Apple devices see System Audio Mode as active.
            0x7D: (source, bytes([0x7E, int(source in self.apples)])),
            # Audio Status volume 0x7f means unknown. We do not mirror LG volume
            # here, so Apple TV's overlay cannot display the actual LG level.
            0x71: (source, bytes.fromhex("7a 7f")),
            # Decline System Audio Mode requests and ARC initiation. Broadcasting
            # audio-mode ON would risk routing LG sound away from its speakers.
            0x70: (source, bytes.fromhex("72 00")),
            0xC3: (source, bytes.fromhex("00 c3 04")),
            0xC0: (source, bytes.fromhex("00 c0 04")),
        }.get(opcode)
        if identity:
            self.send(*identity)

    def deliver_one(self, client, received, queued, body):
        start = time.monotonic()
        # Prefer losing stale repeats over an audible "catch up" burst. Taps
        # get a longer window; HA separately checks repeat age before each step
        # to cover time spent waiting in its own automation queue.
        max_age = 0.3 if body.get("repeat", False) else 1
        if start - received > max_age:
            LOG.warning("Discarded expired input sequence=%s repeat=%s age_ms=%d",
                        body["sequence"], body.get("repeat", False),
                        (start - received) * 1000)
            return False
        try:
            body["age_ms"] = round((start - received) * 1000, 1)
            status, reused = client.post(body)
            end = time.monotonic()
            LOG.info("Forwarded %s HTTP=%d sequence=%s reused=%s queue_ms=%d "
                     "latency_ms=%d total_ms=%d", body["button"], status,
                     body["sequence"], reused, (start - queued) * 1000,
                     (end - start) * 1000, (end - received) * 1000)
            return True
        except Exception as exc:
            # Do not log the secret webhook URL or replay ambiguous requests.
            LOG.warning("Home Assistant delivery failed sequence=%s: %s",
                        body["sequence"], type(exc).__name__)
            return False

    def deliver(self):
        client = WebhookClient(self.config["webhook_url"])
        try:
            while not self.stop.is_set():
                self.health.beat("volume")
                try:
                    item = self.buttons.get(timeout=0.2)
                except queue.Empty:
                    continue
                self.deliver_one(client, *item)
        finally:
            client.close()

    def run(self):
        # Reject missing/invalid URLs before launching daemon workers. Otherwise
        # a startup exception could leave only the CEC loop running.
        WebhookClient(self.config["webhook_url"])
        if self.readiness is not None:
            WebhookClient(self.config["ready_webhook_url"])
        self.lib.bcm_host_init()
        self.lib.vc_cec_register_callback(self.callback, None)
        # Manage identity/replies ourselves instead of firmware's automatic CEC
        # behavior. register_all below requests all callback types; it is not a
        # guarantee that firmware exposes all traffic between other devices.
        if self.lib.vc_cec_set_passive(1):
            raise RuntimeError("Cannot enable passive CEC management")
        claimed = False
        try:
            physical = C.c_uint16()
            if self.lib.vc_cec_get_physical_address(C.byref(physical)) or physical.value == 0xFFFF:
                raise RuntimeError("No HDMI physical address available")
            self.physical = physical.value
            # Check for a real receiver before occupying its address.
            if self.lib.vc_cec_poll_address(5) == 0:
                raise RuntimeError("CEC audio address already occupied")
            if self.lib.vc_cec_set_logical_address(5, 5, 0x18C086):
                raise RuntimeError("Cannot claim audio address")
            claimed = True
            if self.lib.vc_cec_register_all():
                raise RuntimeError("Cannot register CEC callbacks")
            self.advertise()
            self.discover()
            # Heartbeats measure loop progress, not CEC traffic or HA success:
            # sleeping devices and promptly rejected requests are healthy.
            self.health.register("cec", threading.current_thread())
            worker = threading.Thread(target=self.deliver, name="volume", daemon=True)
            self.health.register("volume", worker)
            worker.start()
            if self.readiness is not None:
                # Probing a TV during startup must not block volume delivery.
                ready_worker = threading.Thread(target=self.readiness.run,
                                                name="readiness", daemon=True)
                self.health.register("readiness", ready_worker)
                ready_worker.start()
            threading.Thread(target=self.health.run, name="health", daemon=True).start()
            LOG.info("Ready; physical=%04x, TV audio mode stays off", self.physical)
            refresh = time.monotonic() + 60
            while not self.stop.is_set():
                self.health.beat("cec")
                try:
                    self.handle(*self.events.get(timeout=0.2))
                except queue.Empty:
                    pass
                if time.monotonic() >= refresh:
                    # Relearn playback addresses after reconnects without
                    # requesting Active Source or switching the TV's input.
                    self.advertise()
                    self.discover()
                    refresh = time.monotonic() + 60
        finally:
            self.stop.set()
            if claimed:
                # Withdraw the receiver's active-audio claim on clean shutdown.
                # A power cut cannot send this; Apple TV has sometimes needed
                # a restart to recover CEC after the Pi was unplugged.
                for address in self.apples:
                    self.send(address, bytes.fromhex("7e 00"))
                time.sleep(0.3)
                self.lib.vc_cec_release_logical_address()
            self.lib.vc_cec_set_passive(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config) as handle:
        bridge = Bridge(json.load(handle))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: bridge.stop.set())
    bridge.run()


if __name__ == "__main__":
    main()
