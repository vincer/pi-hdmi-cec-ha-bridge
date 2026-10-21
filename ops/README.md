# Optional appliance logging limits

These are the system-wide logging settings used on the personal Raspbian Buster
Pi deployment. They are separate from the bridge's service watchdog and are not
required to run the bridge. Review your existing rules and back them up first;
other distributions may use different logging services and paths.

| Example | Deployed path |
| --- | --- |
| `journald.conf` | `/etc/systemd/journald.conf.d/cec-appliance.conf` |
| `rsyslog.rotate` | `/etc/logrotate.d/rsyslog` |
| `wtmp.rotate` | `/etc/logrotate.d/wtmp` |
| `btmp.rotate` | `/etc/logrotate.d/btmp` |
| `logrotate-timer.conf` | `/etc/systemd/system/logrotate.timer.d/cec-appliance.conf` |

The rotation examples **replace** the corresponding existing rules; adding a
second rule for the same log path causes duplicate-entry errors. Preserve any
unrelated local customizations. The rsyslog rules assume the existing
`/usr/lib/rsyslog/rsyslog-rotate` helper is available.

Validate the full configuration with `sudo logrotate --debug /etc/logrotate.conf`.
Then run `sudo systemctl daemon-reload`, restart `systemd-journald.service` and
`logrotate.timer`, and run `sudo systemctl start logrotate.service`. Check
`systemctl list-timers logrotate.timer`, `journalctl --disk-usage`, and the
journald/logrotate service journals for errors. This does not force rotation of
all logs. Historical login archives were also retired or compressed during
deployment on our Pi.

The journal stays volatile in RAM with an 8 MiB target and 1 MiB files. Rsyslog
keeps persistent diagnostics: syslog rotates daily or above 1 MiB with seven
compressed archives; the other listed logs rotate weekly or above 512 KiB with
four compressed archives each. Login records rotate monthly or above 1 MiB with
two compressed archives. Rotation is checked every five minutes. These limits
are per file, not a combined `/var/log` quota; files can grow beyond the size
threshold before the next check. Active journal files can also temporarily
exceed its target. Journald rate limiting may discard flood-generated messages
and its effective burst limit depends on available space.

References: [journald limits](https://www.freedesktop.org/software/systemd/man/247/journald.conf.html),
[systemd service watchdog](https://www.freedesktop.org/software/systemd/man/253/systemd.service.html).
