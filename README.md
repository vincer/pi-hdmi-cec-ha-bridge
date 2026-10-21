# Raspberry Pi HDMI-CEC → Home Assistant volume bridge

> [!WARNING]
> **This project is currently for personal use only**, built around my own hardware and Home Assistant setup. If you come across it and would like to try it, **do so at your own risk**. You're welcome to [open an issue](https://github.com/vincer/pi-hdmi-cec-ha-bridge/issues) to ask questions or discuss options for making it more shareable and easier to use with other setups.

Use Apple TV HDMI-CEC volume buttons to control an LG webOS TV through Home Assistant. A Raspberry Pi advertises a CEC audio receiver while keeping the TV's built-in speakers active.

This started because my LG OLED65E6P would not accept HDMI-CEC volume commands for its built-in speakers. The physical Apple TV remote had to use infrared, requiring line of sight to the TV, and volume control from the Apple TV remote on my iPhone did not work. This bridge lets Apple TV send volume commands over HDMI-CEC, then uses Home Assistant's LG webOS integration to change the TV's volume over the network.

The bridge forwards volume up/down and mute through a local HA webhook. Taps change one step; held buttons accelerate after a configurable delay. Expired repeats are discarded to prevent delayed volume jumps.

Any external Active Source broadcast also starts a bounded background check for the TV's webOS port. A separate webhook asks HA to refresh its LG connection once the port is reachable. Repeated hints coalesce, and HA skips refresh when the TV is already on.

## Requirements

- Tested on a first-generation Raspberry Pi Model B, Raspbian Buster, Python 3.7.
- Legacy VideoCore libraries at `/opt/vc/lib`, including `libbcm_host.so`, and `/dev/vchiq` access through the `video` group.
- Python standard library only: no pip dependencies, Docker or libCEC runtime required.
- Home Assistant with the LG webOS integration and local network connectivity.

Modern Raspberry Pi OS releases use a different CEC interface. This implementation has not been ported to `/dev/cec0` and is not a drop-in deployment on current Raspberry Pi OS.

## Setup

1. Install `cec_ha_bridge.py` under `/home/pi/cec-ha-bridge/`.
2. Copy `config.example.json` to `config.json`; supply the two private webhook URLs and your TV's reserved IP address or hostname. Use directory permissions 0700 and config permissions 0600.
3. Adapt both HA automation examples: replace `[REDACTED]` webhook IDs with different random IDs, and replace the LG entity ID with your own. Import/save through HA's supported configuration tools. The volume example uses counted steps and stale-repeat expiry; the readiness example calls `homeassistant.update_entity`.
4. Install the systemd unit under `/etc/systemd/system/`; adjust paths/account as needed. Run `sudo systemctl daemon-reload` and `sudo systemctl enable --now pi-cec-ha.service`.
5. Add `hdmi_ignore_cec_init=1` to the legacy Pi boot configuration to prevent boot selecting the Pi input.
6. On Apple TV enable TV/receiver control and select HDMI volume control. Its on-screen volume display can be hidden if desired.

Keep webhook IDs private. HA triggers accept only local POSTs. No HA bearer token is stored on the Pi.

### Home Assistant setup

1. Set up and pair HA's native **LG webOS Smart TV** integration with the TV. On the tested E6, **LG Connect Apps** must be enabled on the TV. Confirm that HA can change volume and mute while the TV is on before adding the bridge. The TV's separate **Network IP Control** interface is not required by this setup.
2. Find your TV's `media_player` entity ID. Replace every occurrence of `media_player.lg_webos_tv_oled65e6p` in both example automations with that ID.
3. Generate two different, long random webhook IDs, one for volume and one for readiness. For example, run `python3 -c 'import secrets; print(secrets.token_hex(32))'` twice. Replace `[REDACTED]` in each example with its respective ID. The IDs act as secrets; the payload's `sender` field is only a validation check, not authentication.
4. Create two HA automations using [the volume example](ha-volume-automation.example.json) and [the readiness example](ha-readiness-automation.example.json). Each file is a single automation object, not a blueprint or a complete `automations.yaml` file. In the automation editor's **Edit in YAML** view, replace the generated definition with the corresponding JSON object (JSON is valid YAML), then save and enable it. If maintaining `automations.yaml` directly, convert each object to YAML and add it as a separate list entry alongside your existing automations.
5. In the Pi's `config.json`, set `webhook_url` to `http://YOUR_HA_HOST:8123/api/webhook/YOUR_VOLUME_ID` and `ready_webhook_url` to the corresponding readiness URL. Use the address, scheme and port reachable from the Pi; keep each ID matched to its automation. Set `tv_host` to the TV's reserved IP address or resolvable hostname, not HA's address. The readiness probe defaults to the TV's webOS TCP port 3000.
6. Keep both webhooks local-only and POST-only, as in the examples. Restart the bridge after changing its config. With the TV on and Apple TV volume control set to HDMI, test up, down and mute; then test waking the TV and changing volume once the picture appears. Check HA automation traces and the Pi journal if a step fails. An HTTP success from the webhook alone does not prove that HA changed the TV's volume.

The volume automation uses HA's native volume/mute actions, runs a small queue to preserve taps, and expires held repeats before each volume step. It requires HA to report the TV as `on`; early commands are not saved for later playback.

The readiness automation addresses that startup gap. After any external CEC Active Source announcement, the Pi probes the TV until webOS becomes reachable, then asks HA to refresh the LG entity if it is not already `on`. This accelerates HA's reconnect without changing inputs or turning devices on. Keep the volume and readiness automations together for the intended startup behavior. No custom HA integration, MQTT broker or HA access token on the Pi is needed.

### TV startup: enable Quick Start+

**Enable Quick Start+ on the LG TV for faster volume control after waking it.** On this OLED65E6P, our wake tests took roughly **16–17 seconds** from the CEC Active Source announcement until the webOS port became reachable with Quick Start+ off, versus roughly **3 seconds** with it on. These are measurements from this setup, not guaranteed timings for other TVs.

The bridge relies on the TV's network control service, so seeing a picture does not necessarily mean volume control is ready. Quick Start+ reduces the TV-side startup delay; the readiness automation above reduces the additional wait for HA to reconnect. It cannot make webOS available before the TV starts it, and very early button presses can still be dropped.

With Quick Start+ enabled, **we measured about 0.3 W total standby power** on this TV using Ethernet, with Wi-Fi disabled, Wake-on-LAN enabled (the TV's mobile wake setting), and internet access blocked at the router. This is the measured total idle draw, not a measurement of the additional power attributable to Quick Start+ alone. Standby consumption can vary with the TV and settings; measure your own setup if that tradeoff matters.

## Validation and operation

Run `python3 -m unittest discover -p 'test_cec_*.py'`. Tests mock firmware access; socket tests use local TCP and Unix listeners.

View logs with `journalctl -u pi-cec-ha.service`. The service starts at boot and restarts after process failures, with a 10-second restart delay. A health supervisor checks that the CEC loop, volume worker and optional readiness worker are alive and making progress. A dead worker or loop stalled for more than 15 seconds causes the process to exit so systemd can restart it. Idle loops still report progress; a sleeping TV or a promptly failed HA request does not cause a restart. The readiness worker reports progress between probes, so its normal 30-second search is allowed to complete.

The supplied service also enables systemd's independent 30-second watchdog. The supervisor feeds it only while the monitored loops are healthy; if the supervisor or Python process stops running, systemd terminates and restarts the bridge. Core dumps are disabled to protect the small SD card. Long DNS stalls are treated as a lack of progress even though they are not covered by the HTTP socket timeout. Recovery drops in-memory queued input and never retries ambiguous volume POSTs. These checks cannot detect every functional failure: an Apple TV CEC wedge or a firmware call that returns successfully without the intended effect may still require investigation.

On the personal Pi deployment, logging also has explicit limits: an 8 MiB target for the volatile RAM journal, 1 MiB maximum journal files, and five-minute logrotate checks. Persistent syslog rotates daily or above 1 MiB and keeps seven compressed archives; other rsyslog files rotate weekly or above 512 KiB and keep four compressed archives each. Login records rotate monthly or above 1 MiB and keep two compressed archives. These are system-wide deployment settings, not configured by the Python bridge or its service file; see the [optional logging configuration examples](ops/README.md). Rotation thresholds can be exceeded between checks, and active journal files can temporarily exceed the journal target. Journald rate limiting reduces fault-generated floods but can discard messages during a flood.

The fake receiver occupies logical address 5 and refuses ARC. Do not run it alongside a real CEC audio receiver. Only discovered Apple vendor devices are accepted as volume senders; any external Active Source can trigger readiness checks.

If Apple TV stops participating in CEC after the Pi loses power, restarting Apple TV has recovered it in testing. Restarting the Pi service alone did not resolve that particular failure.

