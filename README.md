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

## Validation and operation

Run `python3 -m unittest test_cec_delivery test_cec_hold_acceleration test_cec_readiness`. Tests mock firmware access; one test uses a local TCP listener.

View logs with `journalctl -u pi-cec-ha.service`. The service starts at boot and restarts after process failures. It currently has no worker-health supervision or service watchdog; a process that remains alive but stuck may need intervention.

The fake receiver occupies logical address 5 and refuses ARC. Do not run it alongside a real CEC audio receiver. Only discovered Apple vendor devices are accepted as volume senders; any external Active Source can trigger readiness checks.

If Apple TV stops participating in CEC after the Pi loses power, restarting Apple TV has recovered it in testing. Restarting the Pi service alone did not resolve that particular failure.

