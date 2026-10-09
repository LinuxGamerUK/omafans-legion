# LegionFan

Lenovo Legion fan monitoring and temperature-curve RPM control for the Omarchy 4
Quattro bar.

Dual-fan RPM readout from `lenovo_wmi_other`, CPU/package/hottest-zone
temperatures, and three modes: firmware Auto, manual RPM target, and a
five-point temperature→RPM curve.

Verified on a Lenovo Legion Pro 7 16IRX9H (83DE) running Omarchy, kernel
7.2.8-5-omarchy-bore.

## Requirements

- Omarchy 4 Quattro with its `qs.Ui` and `qs.Commons` interfaces.
- A Legion/LOQ/IdeaPad whose EC exposes the gamezone WMI GUID
  (`887B54E3-…`/`DC2A8805-…`). Verified on 16IRX9H only.
- Python 3.10+ (stdlib only), systemd.

## Install the widget (monitoring only, no root)

```bash
omarchy plugin add https://github.com/LinuxGamerUK/omafans-legion.git
omarchy plugin enable community.omafanslegion --section right
```

The widget reads the hwmon sensors unprivileged. Without the daemon it shows
fan RPM and temperatures and keeps control disabled.

## Optional fan control (root daemon)

```bash
cd ~/.config/omarchy/plugins/community.omafanslegion
python3 scripts/setup.py install --user "$(id -un)" --dry-run
sudo -S -p '' /usr/bin/python3 -I scripts/setup.py install --user "$(id -un)"
sudo -S -p '' systemctl start omafans-legion.service
```

The installer also writes `/etc/modprobe.d/omafans-legion.conf`:

```
options lenovo_wmi_other expose_all_fans=1 relax_fan_constraint=1
```

Without those options the kernel hides the fan channels on this hardware
("fan reporting/tuning is unsupported on this device"). The options apply on
next boot or after `modprobe -r lenovo_wmi_other && modprobe lenovo_wmi_other`.

## Safety model

There is **no kernel watchdog** on this hardware (unlike ThinkPad), so the
daemon cannot hand the fans back with a hardware timer as a backstop. It
therefore:

- releases both fans to firmware control (target 0) on service stop, on write
  failure, on read-back mismatch after three strikes, and on any daemon exit;
- drops back to firmware Auto whenever the widget's heartbeat goes quiet for
  15 s — the widget sends one every 3 s;
- refuses curve points outside 35–92 °C / 1200–5300 RPM, and clamps every
  target into that window;
- releases control and latches a visible fault if the CPU reads ≥ 95 °C
  (the EC's own thermal table is better informed than one sysfs input);
- restarts only via an explicit `systemctl restart` once latched.

**This is experimental hardware control. A wrong curve can cook your machine.
Watch temperatures while testing.**

## Files

| Path | Purpose |
|---|---|
| `omafans_legion.py` | Daemon + client (stdlib only) |
| `fanctl.py` | Unprivileged CLI the widget uses |
| `Service.qml` / `Panel.qml` | Quickshell service and panel |
| `system/omafans-legion.service` | Hardened systemd unit |
| `scripts/setup.py` | Install/remove the privileged component |

## Licence

MIT.
