**ec-su_axb35-linux**  
Linux driver for the embedded controller on the Sixunited AXB35-02 board.  
Vendors using that board:  
- GMKtec EVO-X2  
- Bosgame M5  
- FEVM FA-EX9  
- Peladn YO1  
- NIMO AI MiniPC  
- Corsair AI Workstation 300  
An update to date list can be found [here](https://strixhalo-homelab.d7.wtf/Hardware/Boards/Sixunited-AXB35 "https://strixhalo-homelab.d7.wtf/Hardware/Boards/Sixunited-AXB35")  
For more details, please have a look at the desevens wiki: [https://strixhalo-homelab.d7.wtf/Guides/Power-Mode-and-Fan-Control](https://strixhalo-homelab.d7.wtf/Guides/Power-Mode-and-Fan-Control "https://strixhalo-homelab.d7.wtf/Guides/Power-Mode-and-Fan-Control")  

## Build instructions  
```bash
$ make  
$ sudo make install  
$ sudo insmod ec_su_axb35  
```

## Devices  
### Fan devices  
- `/sys/class/ec_su_axb35/fan1/` - CPU fan 1  
- `/sys/class/ec_su_axb35/fan2/` - CPU fan 2  
- `/sys/class/ec_su_axb35/fan3/` - System fan  
- `/sys/class/ec_su_axb35/fanX/rpm` (RO) - current speed in rpm  
- `/sys/class/ec_su_axb35/fanX/mode` (RW) - [auto, fixed, curve]  
- `/sys/class/ec_su_axb35/fanX/level` (RW) - [0-5] (0=0%, 1=20%, ..., 5=100%)  
- `/sys/class/ec_su_axb35/fanX/rampup_curve` (RW) - 5 values (°C thresholds for level 1-5)  
- `/sys/class/ec_su_axb35/fanX/rampdown_curve` (RW) - 5 values (°C thresholds for level 1-5)  
  
### Temperature device  
- `/sys/class/ec_su_axb35/temp1/` - CPU temperature in °C  
- `/sys/class/ec_su_axb35/temp1/temp` (RO) - current  
- `/sys/class/ec_su_axb35/temp1/min` (RO) - min temp measured since driver load  
- `/sys/class/ec_su_axb35/temp1/max` (RO) - max temp measured since driver load  
  
### APU device  
- `/sys/class/ec_su_axb35/apu/power_mode` (RW) - [quiet, balanced, performance]  

---

## Advanced Python GUI (AXB35 System-Monitor)  

Inside the `python-gui/` directory, you will find a modern, comprehensive **AXB35 System-Monitor**. It tracks fan speeds, CPU temperatures, power modes, and includes internal/external NVMe/SSD temperature tracking (optimized for 1 internal and 1 external drive).

### Prerequisites  

The GUI requires Python 3 and **Tkinter**. Since Tkinter is not installed by default on Ubuntu, install it using:

```bash  
sudo apt update && sudo apt install python3-tk -y  
```

### Installation & Usage  

To test the monitor directly:

```bash  
python3 ./python-gui/axb35_monitor_gui.py  
```

To install it system-wide:

```bash  
sudo install -m 755 ./python-gui/axb35_monitor_gui.py /usr/local/bin/axb35-system-monitor  
cp ./python-gui/axb35-monitor.desktop ~/.local/share/applications/  
```

> **Note for External SSD Tracking:** Ensure `smartctl` (`smartmontools`) is installed on your system. To allow the GUI to read external drive temperatures without requiring a manual password prompt, configure a passwordless `sudoers` exception for `smartctl`:
> ```text
> your_username ALL=(root) NOPASSWD: /usr/sbin/smartctl
