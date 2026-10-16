# ec-su_axb35-linux

Linux driver for the embedded controller on the Sixunited AXB35-02 board.

**Vendors using that board:**

- GMKtec EVO-X2
- Bosgame M5
- FEVM FA-EX9
- Peladn YO1
- NIMO AI MiniPC
- Corsair AI Workstation 300

An up-to-date list can be found [here](https://strixhalo-homelab.d7.wtf/Hardware/Boards/Sixunited-AXB35).  
For more details, see the wiki: [Power Mode and Fan Control](https://strixhalo-homelab.d7.wtf/Guides/Power-Mode-and-Fan-Control)

---

## Build Instructions

```bash
$ make
$ sudo make install
$ sudo insmod ec_su_axb35
```

> **Note:** For persistent loading across reboots, use DKMS (see `contrib/ubuntu/`).

---

## Sysfs Interface

### Fan devices

| Path | Access | Description |
|------|--------|-------------|
| `/sys/class/ec_su_axb35/fan1/` | – | CPU fan 1 |
| `/sys/class/ec_su_axb35/fan2/` | – | CPU fan 2 |
| `/sys/class/ec_su_axb35/fan3/` | – | System fan |
| `fanX/rpm` | RO | Current speed in RPM |
| `fanX/mode` | RW | `auto`, `fixed`, or `curve` |
| `fanX/level` | RW | 0–5 (0 = off, 1 = 20 %, … 5 = 100 %) |
| `fanX/rampup_curve` | RW | 5 temperature thresholds (°C) for levels 1–5 |
| `fanX/rampdown_curve` | RW | 5 temperature thresholds (°C) for levels 1–5 |

### Temperature device

| Path | Access | Description |
|------|--------|-------------|
| `/sys/class/ec_su_axb35/temp1/temp` | RO | Current CPU temperature (°C) |
| `/sys/class/ec_su_axb35/temp1/min` | RO | Minimum since driver load |
| `/sys/class/ec_su_axb35/temp1/max` | RO | Maximum since driver load |

### APU / Power Mode

| Path | Access | Description |
|------|--------|-------------|
| `/sys/class/ec_su_axb35/apu/power_mode` | RW | `quiet`, `balanced`, or `performance` |

---

## Python GUI Apps (optional)

Both GUIs are located in the `python-gui/` directory and require **Python 3 + Tkinter**:

```bash
sudo apt install python3-tk -y
```

### 1. Fan Control Panel *(original)*

Interactive control panel with fan curve editing, level setting, and power mode switching.  
Requires root for writing to sysfs (fan levels, curves, power mode).

```bash
# Quick test:
python3 ./python-gui/ec-su_axb35-linux-gui.py

# Install system-wide:
sudo install -m 755 ./python-gui/ec-su_axb35-linux-gui.py /usr/local/bin/ec-fan-control
cp ./python-gui/ec-fan-control.desktop ~/.local/share/applications/
```

### 2. AXB35 System-Monitor *(fork addition)*

Read-only system monitor showing CPU/GPU/NVMe temperatures, usage bars (CPU %, GPU %, RAM, VRAM, disk), fan RPMs with mode badges, and power status.  
Optionally tracks an **external USB NVMe/SSD** temperature via `smartctl`.

No root required for basic operation (read-only sysfs + `/proc`).

```bash
# Quick test:
python3 ./python-gui/axb35_monitor_gui.py

# Install system-wide:
sudo install -m 755 ./python-gui/axb35_monitor_gui.py /usr/local/bin/axb35-system-monitor
cp ./python-gui/axb35-monitor.desktop ~/.local/share/applications/
```

#### External SSD temperature (optional)

The monitor can display the temperature of one external USB-attached NVMe/SATA SSD.  
This requires `smartmontools`:

```bash
sudo apt install smartmontools -y
```

By default, reading SMART data from USB devices requires root. To allow the GUI to read it without a password prompt every cycle, add a scoped sudoers rule:

```bash
echo "$USER ALL=(root) NOPASSWD: /usr/sbin/smartctl" | sudo tee /etc/sudoers.d/smartctl
```

> ⚠️ This grants **only** `smartctl` (SMART disk health reads) without a password – it does not give general root access.  
> If you prefer not to use this, the monitor still works fully – the EXT temperature column will simply show "not available".

---

## Contrib / Integrations

- `contrib/ubuntu/` – DKMS + MOK signing setup scripts
- `contrib/netdata/` – Netdata chart integration for fan/RPM monitoring
