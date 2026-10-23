#!/usr/bin/env python3
"""
AXB35 System Monitor – All-in-One GUI für Lüfter, CPU, GPU, RAM, VRAM & NVMe
Keine Abhängigkeiten außer Python-Standardbibliothek (tkinter).

System: GMKtec Evo-X2 / Strix Halo (AMD Ryzen AI Max+ 395)
GPU:    Radeon 8060S iGPU (amdgpu)
Treiber: ec_su_axb35 (DKMS)

Start:  python3 axb35_monitor_gui.py
"""

import os
import sys
import json
import time
import subprocess
import threading
import tkinter as tk

# ─── Config-Pfad (Zoom, Fensterposition – überlebt Neustarts) ─────────────
CONFIG_DIR  = os.path.expanduser("~/.config/axb35-monitor")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


# ─── Konfiguration ────────────────────────────────────────────────────────
BASE_PATH     = "/sys/class/ec_su_axb35"
DRM_DEVICE    = "/sys/class/drm/card1/device"       # amdgpu (Radeon 8060S iGPU)
UPDATE_INTERVAL = 1.5          # Sekunden zwischen Aktualisierungen

# hwmon-Indizes werden dynamisch per Name aufgelöst (robust gegen Reordering!)
def _find_hwmons():
    """Scannt /sys/class/hwmon/ und liefert Pfade per Gerätename."""
    result = {'amdgpu': None, 'nvme1': None, 'nvme2': None}
    nvme_count = 0
    try:
        for entry in sorted(os.listdir('/sys/class/hwmon')):
            name_file = f'/sys/class/hwmon/{entry}/name'
            try:
                with open(name_file) as f:
                    dev_name = f.read().strip()
                if dev_name == 'amdgpu':
                    result['amdgpu'] = f'/sys/class/hwmon/{entry}'
                elif dev_name == 'nvme':
                    nvme_count += 1
                    if nvme_count == 1:
                        result['nvme1'] = f'/sys/class/hwmon/{entry}'
                    elif nvme_count == 2:
                        result['nvme2'] = f'/sys/class/hwmon/{entry}'
            except (OSError, IOError):
                continue
    except OSError:
        pass
    return result

_HWMONS = _find_hwmons()
HWMON_AMDGPU = _HWMONS.get('amdgpu') or "/sys/class/hwmon/hwmon7"  # Fallback
HWMON_NVME   = _HWMONS.get('nvme1')  or "/sys/class/hwmon/hwmon2"   # Fallback
HWMON_NVME2  = _HWMONS.get('nvme2')  # None wenn nicht vorhanden

# Balken-Maximalwerte (für Skalierung)
MAX_RPM_BAR   = 6000          # RPM bei vollem Balken

# ─── Farben (Catppuccin Mocha) ────────────────────────────────────────────
C_BG      = "#1e1e2e"
C_SURFACE = "#313244"
C_TEXT    = "#cdd6f4"
C_DIM     = "#7f849c"
C_GREEN   = "#a6e3a1"
C_YELLOW  = "#f9e2af"
C_RED     = "#f38ba8"
C_BLUE    = "#89b4fa"
C_CYAN    = "#94e2d5"
C_BAR_BG  = "#45475a"

# ─── Sprache (DE/EN) ─────────────────────────────────────────────────────
def _detect_lang():
    """OS-Sprache erkennen: 'de' oder 'en'."""
    lang = os.environ.get('LANG', '').lower()
    return 'de' if lang.startswith('de') else 'en'

LANG = _detect_lang()

T = {
    'de': {
        'fan1': 'CPU Lüfter 1',
        'fan2': 'CPU Lüfter 2',
        'fan3': 'System-Lüfter',
        'not_connected': 'nicht angeschlossen',
        'zoom_hint': 'Ctrl ± Zoom · Ctrl 0 Reset',
        # Config-GUI
        'cfg_title': 'Lüfter-Config',
        'cfg_cpu_fan': 'CPU-Kühler (Fan 1+2)',
        'cfg_sys_fan': 'System-Lüfter (NVMe-Kühlung)',
        'cfg_mode': 'Modus:',
        'cfg_level': 'Level:',
        'cfg_rampup': 'Ramp Up',
        'cfg_rampdown': 'Ramp Down',
        'cfg_power': 'APU Power Mode:',
        'cfg_apply': 'Apply',
        'cfg_reset_std': 'Standard laden',
        'cfg_applied': '✓ Angewendet',
        'cfg_error': '⚠ Fehler beim Schreiben!',
        'cfg_nvme_ctx': 'NVMe-Temp: --°C',
        'cfg_both_fans': '(beide Lüfter gemeinsam)',
    },
    'en': {
        'fan1': 'CPU Fan 1',
        'fan2': 'CPU Fan 2',
        'fan3': 'System Fan',
        'not_connected': 'not connected',
        'zoom_hint': 'Ctrl ± Zoom · Ctrl 0 Reset',
        # Config-GUI
        'cfg_title': 'Fan Config',
        'cfg_cpu_fan': 'CPU Cooler (Fan 1+2)',
        'cfg_sys_fan': 'System Fan (NVMe Cooling)',
        'cfg_mode': 'Mode:',
        'cfg_level': 'Level:',
        'cfg_rampup': 'Ramp Up',
        'cfg_rampdown': 'Ramp Down',
        'cfg_power': 'APU Power Mode:',
        'cfg_apply': 'Apply',
        'cfg_reset_std': 'Load Standard',
        'cfg_applied': '✓ Applied',
        'cfg_error': '⚠ Write Error!',
        'cfg_nvme_ctx': 'NVMe Temp: --°C',
        'cfg_both_fans': '(both fans together)',
    }
}

# ─── Standard-Kurven (aus Analyse/fancontrol.service) ──────────────────────
STD_CURVES = {
    'cpu_fan': {  # Fan 1 + 2 (gemeinsam)
        'rampup':   [60, 70, 83, 95, 97],
        'rampdown': [40, 50, 80, 94, 96],
    },
    'sys_fan': {  # Fan 3 (NVMe-Kühlung)
        'rampup':   [20, 60, 83, 95, 97],
        'rampdown': [0, 50, 80, 94, 96],
    },
}


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────
def read_sysfs(path):
    """Sicherer sysfs-Read, liefert None bei Fehler."""
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except (OSError, IOError):
        return None


def read_int(path):
    """sysfs-Wert als int lesen."""
    raw = read_sysfs(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def get_cpu_usage(prev_stat=None):
    """CPU-Auslastung in % (Delta aus /proc/stat). Liefert (pct, new_stat)."""
    try:
        with open('/proc/stat') as f:
            parts = f.readline().split()[1:]  # 'cpu' überspringen
        values = list(map(int, parts))
        idle  = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
    except (OSError, ValueError, IndexError):
        return None, prev_stat

    new_stat = (idle, total)
    if prev_stat is not None:
        d_idle  = idle - prev_stat[0]
        d_total = total - prev_stat[1]
        if d_total > 0:
            usage = round(100.0 * (1.0 - d_idle / d_total))
            return max(0, min(100, usage)), new_stat

    return None, new_stat


def get_disk_usage():
    """Speicherplatz-Belegung von '/' in % + GB."""
    try:
        st = os.statvfs('/')
        frsize  = st.f_frsize
        total   = st.f_blocks * frsize
        free    = st.f_bavail * frsize
        used    = total - free
        pct     = int(100 * used / total) if total > 0 else 0
        return {'pct': pct, 'used_gb': round(used/(1024**3), 1),
                'total_gb': round(total/(1024**3), 1)}
    except OSError:
        return None


def get_ram_usage():
    """RAM-Belegung aus /proc/meminfo."""
    info = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                key = line.split(':')[0].strip()
                if key in ('MemTotal', 'MemAvailable'):
                    info[key] = int(line.split()[1])  # kB
    except (OSError, ValueError):
        return None

    if 'MemTotal' not in info or 'MemAvailable' not in info:
        return None

    total_kb = info['MemTotal']
    used_kb  = total_kb - info['MemAvailable']
    pct      = int(100 * used_kb / total_kb) if total_kb > 0 else 0
    return {'pct': pct, 'used_gb': round(used_kb/(1024**2), 1),
            'total_gb': round(total_kb/(1024**2), 1)}


def get_vram_usage():
    """VRAM-Belegung aus amdgpu sysfs."""
    used_raw  = read_int(f"{DRM_DEVICE}/mem_info_vram_used")
    total_raw = read_int(f"{DRM_DEVICE}/mem_info_vram_total")
    if used_raw is None or total_raw is None:
        return None
    pct = int(100 * used_raw / total_raw) if total_raw > 0 else 0
    return {'pct': pct, 'used_gb': round(used_raw/(1024**3), 1),
            'total_gb': round(total_raw/(1024**3), 1)}


def find_ext_mountpoint():
    """Mountpunkt der externen SSD (/dev/sda*) aus /proc/mounts finden. Dekodiert \\040 etc."""
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and (parts[0] == '/dev/sda' or parts[0].startswith('/dev/sda')):
                    # /proc/mounts kodiert Sonderzeichen als Oktal (\040 = Leerzeichen)
                    mp = parts[1]
                    mp = mp.replace('\\040', ' ')
                    return mp
    except OSError:
        pass
    return None


def find_nvme2_mountpoint():
    """Mountpunkt der 2. internen NVMe finden (nicht Root, nicht USB/sda*)."""
    try:
        with open('/proc/mounts') as f:
            root_dev = None
            nvme2_mp = None
            # Ersten Pass: Root-Gerät identifizieren
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == '/':
                    root_dev = parts[0]
                    break
            # Zweiten Pass: NVMe-Partition finden die NICHT Root ist
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith('/dev/nvme'):
                    if root_dev is None or parts[0] != root_dev:
                        mp = parts[1].replace('\\040', ' ')
                        # Skip tmpfs, squashfs etc.
                        if not any(skip in line for skip in ('tmpfs', 'squashfs', 'udev')):
                            nvme2_mp = mp
                            break
            return nvme2_mp
    except OSError:
        pass
    return None


def get_nvme2_disk_usage():
    """Speicherplatz der 2. internen NVMe (falls vorhanden)."""
    mp = find_nvme2_mountpoint()
    if mp is None:
        return None
    try:
        st = os.statvfs(mp)
        frsize  = st.f_frsize
        total   = st.f_blocks * frsize
        free    = st.f_bavail * frsize
        used    = total - free
        pct     = int(100 * used / total) if total > 0 else 0
        return {'pct': pct, 'used_gb': round(used/(1024**3), 1),
                'total_gb': round(total/(1024**3), 1)}
    except OSError:
        return None


def get_ext_disk_usage():
    """Speicherplatz der externen SSD (via statvfs auf Mountpunkt)."""
    mp = find_ext_mountpoint()
    if mp is None:
        return None
    try:
        st = os.statvfs(mp)
        frsize  = st.f_frsize
        total   = st.f_blocks * frsize
        free    = st.f_bavail * frsize
        used    = total - free
        pct     = int(100 * used / total) if total > 0 else 0
        return {'pct': pct, 'used_gb': round(used/(1024**3), 1),
                'total_gb': round(total/(1024**3), 1)}
    except OSError:
        return None


def get_ext_nvme():
    """Externe NVMe-SSD (USB) – Temperatur + Modell via smartctl. Liefert None wenn nicht angeschlossen."""
    try:
        result = subprocess.run(
            ['sudo', '-n', 'smartctl', '-a', '/dev/sda'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None

        temp  = None
        model = None
        for line in result.stdout.splitlines():
            # "Temperature:                        36 Celsius"
            if 'Temperature:' in line and 'Sensor' not in line and 'Comp.' not in line:
                parts = line.split()
                for i, p in enumerate(parts):
                    if p == 'Celsius':
                        temp = int(parts[i - 1])
                        break
            # "Model Number:                       CT2000P310SSD2"
            elif 'Model Number:' in line:
                model = line.split(':', 1)[1].strip()

        if temp is None:
            return None
        return {'temp': temp, 'model': model}
    except (subprocess.TimeoutExpired, OSError):
        return None


def load_config():
    """Config laden (Zoom, Fensterposition). Liefert dict oder leeres {}."""
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(scale, win_x=None, win_y=None):
    """Aktuelle Zoom-Stufe + Fensterposition speichern."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        cfg = load_config()  # bestehende Werte beibehalten
        cfg['scale'] = scale
        if win_x is not None:
            cfg['win_x'] = int(win_x)
        if win_y is not None:
            cfg['win_y'] = int(win_y)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass  # Config-Save ist nice-to-have, kein Hard-Fail


def _sysfs_write(path, value):
    """Sicheren sysfs-Write via sudo (NOPASSWD). Liefert True bei Erfolg."""
    cmd = f'echo "{value}" > {path}'
    try:
        r = subprocess.run(['sudo', '-n', 'sh', '-c', cmd],
                          capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _sysfs_read_curve(path):
    """Curve aus sysfs lesen. Liefert List[5 ints] oder None."""
    raw = read_sysfs(path)
    if not raw:
        return None
    try:
        vals = [int(x.strip()) for x in raw.split(',')]
        if len(vals) == 5:
            return vals
    except ValueError:
        pass
    return None


# ─── Config-GUI (Toplevel im Monitor) ─────────────────────────────────────
class FanConfigWindow:
    """Separates Toplevel-Fenster zur Lüfter-Configuration.

    Enthält: CPU-Kühler (Fan 1+2 gemeinsam), System-Lüfter (Fan 3), APU Power Mode.
    Kurven als horizontale Slider (0–100°C). Standard-Werte aus fancontrol.service.
    """

    def __init__(self, parent_root):
        self.parent = parent_root
        self.win = tk.Toplevel(parent_root)
        self.win.title(T[LANG]['cfg_title'])
        self.win.configure(bg=C_BG)
        self.win.resizable(False, False)

        # State-Variablen
        self._cpu_mode   = tk.StringVar(value="curve")
        self._sys_mode   = tk.StringVar(value="curve")
        self._cpu_level  = tk.IntVar(value=3)
        self._sys_level  = tk.IntVar(value=3)
        self._power_mode = tk.StringVar(value="balanced")

        # Kurven-Werte (werden beim Laden aktualisiert)
        self._cpu_rampup   = list(STD_CURVES['cpu_fan']['rampup'])
        self._cpu_rampdown = list(STD_CURVES['cpu_fan']['rampdown'])
        self._sys_rampup   = list(STD_CURVES['sys_fan']['rampup'])
        self._sys_rampdown = list(STD_CURVES['sys_fan']['rampdown'])

        # Widget-Referenzen (dynamisch beim Bauen gefüllt)
        self._cpu_level_frame  = None
        self._cpu_curve_frame  = None
        self._sys_level_frame  = None
        self._sys_curve_frame  = None
        self._nvme_ctx_lbl     = None
        self._status_lbl       = None

        # Slider-Referenzen: {key: [scale_widget, ...]} und Labels
        self._sliders = {}
        self._slider_labels = {}

        # Zuerst aktuelle Werte laden (damit UI mit echten Defaults startet)
        self._load_current_values()

        # Dann UI bauen
        self._build_ui()

        # Position: rechts neben dem Monitor (oder links wenn nicht genug Platz)
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        px = parent_root.winfo_x() + 740
        if px + 520 > sw:
            px = max(10, parent_root.winfo_x() - 530)
        py = parent_root.winfo_y()
        self.win.geometry(f"+{px}+{py}")

    # ─── UI-Aufbau ──────────────────────────────────────────────────────
    def _build_ui(self):
        main = tk.Frame(self.win, bg=C_BG)
        main.pack(fill=tk.BOTH, expand=True, padx=16, pady=12)

        # ── Header: Titel + Status + Apply-Button ──
        header = tk.Frame(main, bg=C_BG)
        header.pack(fill=tk.X, pady=(0, 12))

        title_lbl = tk.Label(header, text=f"⚙️  {T[LANG]['cfg_title']}",
                            font=("Segoe UI", 13, "bold"), bg=C_BG, fg=C_TEXT)
        title_lbl.pack(side=tk.LEFT)

        self._status_lbl = tk.Label(header, text="", font=("Segoe UI", 9),
                                    bg=C_BG, fg=C_GREEN)
        self._status_lbl.pack(side=tk.RIGHT, padx=(8, 0))

        apply_btn = tk.Button(header, text=T[LANG]['cfg_apply'],
                             command=self._apply_all, width=10,
                             bg=C_BLUE, fg="#1e1e2e", activebackground=C_CYAN,
                             relief=tk.FLAT, font=("Segoe UI", 10, "bold"),
                             cursor="hand2", bd=0)
        apply_btn.pack(side=tk.RIGHT)

        # ── CPU-Kühler Block (Fan 1+2) ──
        self._build_fan_block(main, 'cpu')

        # Separator
        tk.Frame(main, bg=C_BAR_BG, height=1).pack(fill=tk.X, pady=8)

        # ── System-Lüfter Block (Fan 3) ──
        self._build_fan_block(main, 'sys')

        # Separator
        tk.Frame(main, bg=C_BAR_BG, height=1).pack(fill=tk.X, pady=8)

        # ── APU Power Mode ──
        pw_frame = tk.Frame(main, bg=C_SURFACE)
        pw_frame.pack(fill=tk.X, pady=(0, 12))
        inner_pw = tk.Frame(pw_frame, bg=C_SURFACE)
        inner_pw.pack(fill=tk.X, padx=12, pady=8)

        pw_lbl = tk.Label(inner_pw, text=T[LANG]['cfg_power'],
                         font=("Segoe UI", 10), bg=C_SURFACE, fg=C_TEXT)
        pw_lbl.pack(side=tk.LEFT)

        pw_menu = tk.OptionMenu(inner_pw, self._power_mode, 'quiet', 'balanced', 'performance')
        pw_menu.config(bg=C_SURFACE, fg=C_TEXT, activebackground=C_BAR_BG,
                       activeforeground=C_TEXT, relief=tk.FLAT, bd=0,
                       highlightthickness=0, font=("Segoe UI", 10), width=12)
        pw_menu['menu'].config(bg=C_SURFACE, fg=C_TEXT,
                               activebackground=C_BLUE,
                               activeforeground="#1e1e2e",
                               relief=tk.FLAT, bd=1)
        pw_menu.pack(side=tk.RIGHT)

        # ── Bottom: Reset-Standard ──
        bottom = tk.Frame(main, bg=C_BG)
        bottom.pack(fill=tk.X, pady=(4, 0))

        reset_btn = tk.Button(bottom, text=f"↺ {T[LANG]['cfg_reset_std']}",
                             command=self._reset_standard, width=16,
                             bg=C_SURFACE, fg=C_TEXT, activebackground=C_BAR_BG,
                             relief=tk.FLAT, font=("Segoe UI", 9), cursor="hand2", bd=0)
        reset_btn.pack(side=tk.LEFT)

        # ── Initiale Sichtbarkeit je nach geladenem Modus ──
        self._on_mode_change('cpu')
        self._on_mode_change('sys')

        # ─── Live-Temperatur-Update im Config-Fenster (alle 2s) ───
        self._update_ctx_temps()
        self.win.after(2000, self._ctx_temp_timer)

    def _ctx_temp_timer(self):
        """Periodisches Aktualisieren der Kontext-Temperaturen."""
        try:
            if not self.win.winfo_exists():
                return  # Fenster geschlossen → Timer stoppen
            self._update_ctx_temps()
            self.win.after(2000, self._ctx_temp_timer)
        except tk.TclError:
            pass

    def _update_ctx_temps(self):
        """CPU-Temp (Kühler-Block) + NVMe-Temp (System-Lüfter-Block) aktualisieren."""
        # CPU Temp für CPU-Kühler-Block
        if hasattr(self, '_cpu_ctx_lbl'):
            cpu_t = read_int(f"{BASE_PATH}/temp1/temp")
            if cpu_t is not None:
                color = self._apu_temp_color(cpu_t)
                self._cpu_ctx_lbl.config(text=f"CPU: {cpu_t}°C", fg=color)

        # NVMe Temp für System-Lüfter-Block
        if hasattr(self, '_nvme_ctx_lbl'):
            nvme_t = None
            if HWMON_NVME:
                raw = read_int(f"{HWMON_NVME}/temp1_input")
                if raw is not None:
                    nvme_t = round(raw / 1000.0)
            if nvme_t is not None:
                color = self._ssd_temp_color(nvme_t)
                prefix = "NVMe-Temp:" if LANG == 'de' else "NVMe Temp:"
                self._nvme_ctx_lbl.config(text=f"{prefix} {nvme_t}°C", fg=color)

    @staticmethod
    def _apu_temp_color(t):
        if t < 60:   return C_GREEN
        elif t < 85: return C_YELLOW
        else:        return C_RED

    @staticmethod
    def _ssd_temp_color(t):
        if t < 50:   return C_GREEN
        elif t < 70: return C_YELLOW
        else:        return C_RED

    def _build_fan_block(self, parent, which):
        """Einen Lüfter-Config-Block bauen (cpu = Fan1+2, sys = Fan3)."""
        is_cpu = (which == 'cpu')
        title_key = 'cfg_cpu_fan' if is_cpu else 'cfg_sys_fan'

        frame = tk.Frame(parent, bg=C_SURFACE)
        frame.pack(fill=tk.X, pady=(0, 2))
        inner = tk.Frame(frame, bg=C_SURFACE)
        inner.pack(fill=tk.X, padx=12, pady=8)

        # Titel + Kontext
        title_row = tk.Frame(inner, bg=C_SURFACE)
        title_row.pack(fill=tk.X, pady=(0, 4))

        title_lbl = tk.Label(title_row, text=T[LANG][title_key],
                            font=("Segoe UI", 11, "bold"), bg=C_SURFACE, fg=C_TEXT)
        title_lbl.pack(side=tk.LEFT)

        if is_cpu:
            hint = tk.Label(title_row, text=f" {T[LANG]['cfg_both_fans']}",
                           font=("Segoe UI", 8), bg=C_SURFACE, fg=C_DIM)
            hint.pack(side=tk.LEFT, padx=(6, 0))
            # CPU-Temp als Kontext (rechts)
            self._cpu_ctx_lbl = tk.Label(title_row, text="CPU: --°C",
                                         font=("Segoe UI", 9), bg=C_SURFACE, fg=C_DIM)
            self._cpu_ctx_lbl.pack(side=tk.RIGHT)
        else:
            self._nvme_ctx_lbl = tk.Label(title_row, text=T[LANG]['cfg_nvme_ctx'],
                                         font=("Segoe UI", 9), bg=C_SURFACE, fg=C_DIM)
            self._nvme_ctx_lbl.pack(side=tk.RIGHT)

        # ── Modus-Zeile ──
        mode_row = tk.Frame(inner, bg=C_SURFACE)
        mode_row.pack(fill=tk.X, pady=2)

        mode_lbl = tk.Label(mode_row, text=T[LANG]['cfg_mode'],
                           font=("Segoe UI", 10), bg=C_SURFACE, fg=C_TEXT)
        mode_lbl.pack(side=tk.LEFT)

        var = self._cpu_mode if is_cpu else self._sys_mode
        mode_menu = tk.OptionMenu(mode_row, var, 'auto', 'fixed', 'curve')
        mode_menu.config(bg=C_SURFACE, fg=C_TEXT, activebackground=C_BAR_BG,
                         activeforeground=C_TEXT, relief=tk.FLAT, bd=0,
                         highlightthickness=0, font=("Segoe UI", 10), width=8)
        mode_menu['menu'].config(bg=C_SURFACE, fg=C_TEXT,
                                 activebackground=C_BLUE,
                                 activeforeground="#1e1e2e",
                                 relief=tk.FLAT, bd=1)
        mode_menu.pack(side=tk.RIGHT)
        # Track changes (OptionMenu hat kein bind-Event → poll via trace)
        var.trace_add('write', lambda *a: self._on_mode_change(which))

        # ── Level-Zeile (nur bei "fixed" sichtbar) ──
        level_frame = tk.Frame(inner, bg=C_SURFACE)
        if is_cpu:
            self._cpu_level_frame = level_frame
        else:
            self._sys_level_frame = level_frame

        lv_lbl = tk.Label(level_frame, text=T[LANG]['cfg_level'],
                         font=("Segoe UI", 10), bg=C_SURFACE, fg=C_TEXT)
        lv_lbl.pack(side=tk.LEFT)

        lv_var = self._cpu_level if is_cpu else self._sys_level
        level_scale = tk.Scale(level_frame, from_=0, to=5, orient=tk.HORIZONTAL,
                              variable=lv_var, bg=C_SURFACE, fg=C_TEXT,
                              troughcolor=C_BAR_BG, highlightthickness=0,
                              length=220, sliderlength=18, showvalue=True,
                              font=("Consolas", 9), resolution=1)
        level_scale.pack(side=tk.RIGHT)

        # ── Kurven-Sektion (nur bei "curve" sichtbar) ──
        curve_frame = tk.Frame(inner, bg=C_SURFACE)
        if is_cpu:
            self._cpu_curve_frame = curve_frame
        else:
            self._sys_curve_frame = curve_frame

        self._build_curve_sliders(curve_frame, which, 'rampup')
        self._build_curve_sliders(curve_frame, which, 'rampdown')

    def _build_curve_sliders(self, parent, which, direction):
        """5 horizontale Slider für eine Kurven-Richtung (RampUp oder RampDown)."""
        is_cpu = (which == 'cpu')
        dir_text = T[LANG]['cfg_rampup'] if direction == 'rampup' else T[LANG]['cfg_rampdown']

        # Sektions-Header
        hdr = tk.Label(parent, text=dir_text, font=("Segoe UI", 9, "bold"),
                      bg=C_SURFACE, fg=C_DIM)
        hdr.pack(anchor=tk.W, pady=(6, 2), padx=4)

        # Werte-Liste holen (aktuelle State-Werte)
        if is_cpu:
            val_list = self._cpu_rampup if direction == 'rampup' else self._cpu_rampdown
        else:
            val_list = self._sys_rampup if direction == 'rampup' else self._sys_rampdown

        key = f"{which}_{direction}"
        scales = []
        labels = []

        for i in range(5):
            row = tk.Frame(parent, bg=C_SURFACE)
            row.pack(fill=tk.X, padx=8, pady=1)

            # Level-Label (L1–L5)
            lv_lbl = tk.Label(row, text=f"L{i+1}", font=("Consolas", 9),
                             bg=C_SURFACE, fg=C_DIM, width=3)
            lv_lbl.pack(side=tk.LEFT)

            # Horizontaler Slider (0–100°C)
            sv = tk.IntVar(value=val_list[i])
            slider = tk.Scale(row, from_=0, to=100, orient=tk.HORIZONTAL,
                             variable=sv, bg=C_SURFACE, fg=C_TEXT,
                             troughcolor=C_BAR_BG, highlightthickness=0,
                             length=280, sliderlength=14, showvalue=False,
                             resolution=1)
            slider.pack(side=tk.LEFT, padx=(4, 6))

            # Wert-Label rechts
            val_lbl = tk.Label(row, text=f"{val_list[i]}°", font=("Consolas", 9),
                              bg=C_SURFACE, fg=C_TEXT, width=5)
            val_lbl.pack(side=tk.RIGHT)

            # Live-Update des Labels – via Var-trace (zuverlässiger als command!)
            def _upd(*_a, _sv=sv, _lbl=val_lbl):
                try:
                    v = int(_sv.get())
                    _lbl.config(text=f"{v}°")
                except tk.TclError:
                    pass
            sv.trace_add('write', _upd)

            scales.append(slider)
            labels.append(val_lbl)

        self._sliders[key] = scales
        self._slider_labels[key] = labels

    # ─── Logik ──────────────────────────────────────────────────────────
    def _on_mode_change(self, which):
        """Level-/Kurven-Sektionen ein-/ausblenden je nach Modus."""
        is_cpu = (which == 'cpu')
        if is_cpu:
            mode = self._cpu_mode.get()
            lv_frame = self._cpu_level_frame
            cv_frame = self._cpu_curve_frame
        else:
            mode = self._sys_mode.get()
            lv_frame = self._sys_level_frame
            cv_frame = self._sys_curve_frame

        if lv_frame is None or cv_frame is None:
            return  # noch nicht gebaut

        if mode == 'fixed':
            lv_frame.pack(fill=tk.X, pady=2)
            cv_frame.pack_forget()
        elif mode == 'curve':
            lv_frame.pack_forget()
            cv_frame.pack(anchor=tk.W)
        else:  # auto – keine weiteren Einstellungen nötig
            lv_frame.pack_forget()
            cv_frame.pack_forget()

    def _load_current_values(self):
        """Aktuelle sysfs-Werte lesen und in State setzen."""
        # Fan 1 repräsentiert CPU-Kühler (Fan 1+2 gemeinsam)
        mode = read_sysfs(f"{BASE_PATH}/fan1/mode")
        if mode in ('auto', 'fixed', 'curve'):
            self._cpu_mode.set(mode)

        level = read_int(f"{BASE_PATH}/fan1/level")
        if level is not None:
            self._cpu_level.set(level)

        cu = _sysfs_read_curve(f"{BASE_PATH}/fan1/rampup_curve")
        if cu:
            self._cpu_rampup = cu
        cd = _sysfs_read_curve(f"{BASE_PATH}/fan1/rampdown_curve")
        if cd:
            self._cpu_rampdown = cd

        # Fan 3 = System-Lüfter
        mode3 = read_sysfs(f"{BASE_PATH}/fan3/mode")
        if mode3 in ('auto', 'fixed', 'curve'):
            self._sys_mode.set(mode3)

        level3 = read_int(f"{BASE_PATH}/fan3/level")
        if level3 is not None:
            self._sys_level.set(level3)

        cu3 = _sysfs_read_curve(f"{BASE_PATH}/fan3/rampup_curve")
        if cu3:
            self._sys_rampup = cu3
        cd3 = _sysfs_read_curve(f"{BASE_PATH}/fan3/rampdown_curve")
        if cd3:
            self._sys_rampdown = cd3

        # APU Power Mode
        pw = read_sysfs(f"{BASE_PATH}/apu/power_mode")
        if pw in ('quiet', 'balanced', 'performance'):
            self._power_mode.set(pw)

    def _reset_standard(self):
        """Alle Kurven auf Standard-Werte (aus fancontrol.service) zurücksetzen."""
        self._cpu_rampup   = list(STD_CURVES['cpu_fan']['rampup'])
        self._cpu_rampdown = list(STD_CURVES['cpu_fan']['rampdown'])
        self._sys_rampup   = list(STD_CURVES['sys_fan']['rampup'])
        self._sys_rampdown = list(STD_CURVES['sys_fan']['rampdown'])

        # Slider + Labels in der UI synchronisieren
        for key, vals in [('cpu_rampup', self._cpu_rampup),
                          ('cpu_rampdown', self._cpu_rampdown),
                          ('sys_rampup', self._sys_rampup),
                          ('sys_rampdown', self._sys_rampdown)]:
            if key in self._sliders:
                for i, (scale, lbl) in enumerate(zip(self._sliders[key],
                                                      self._slider_labels[key])):
                    try:
                        scale.set(vals[i])
                        lbl.config(text=f"{vals[i]}°")
                    except tk.TclError:
                        pass

        if self._status_lbl:
            self._status_lbl.config(
                text=("↺ Standard geladen" if LANG == 'de' else "↺ Standard loaded"),
                fg=C_CYAN)

    def _apply_all(self):
        """Alle Einstellungen an sysfs schreiben + in fancontrol.service persistieren."""
        ok = True

        # ── CPU-Kühler: Fan 1 + Fan 2 gemeinsam ──
        cpu_mode = self._cpu_mode.get()
        for fan in (1, 2):
            if cpu_mode == 'curve':
                up_str = ','.join(str(v) for v in self._get_slider_values('cpu_rampup'))
                dn_str = ','.join(str(v) for v in self._get_slider_values('cpu_rampdown'))
                ok &= _sysfs_write(f"{BASE_PATH}/fan{fan}/rampup_curve", up_str)
                ok &= _sysfs_write(f"{BASE_PATH}/fan{fan}/rampdown_curve", dn_str)
            elif cpu_mode == 'fixed':
                lv = self._cpu_level.get()
                ok &= _sysfs_write(f"{BASE_PATH}/fan{fan}/level", str(lv))

            # Mode zuletzt setzen (triggert die Aktivierung)
            ok &= _sysfs_write(f"{BASE_PATH}/fan{fan}/mode", cpu_mode)

        # ── System-Lüfter: Fan 3 ──
        sys_mode = self._sys_mode.get()
        if sys_mode == 'curve':
            up_str = ','.join(str(v) for v in self._get_slider_values('sys_rampup'))
            dn_str = ','.join(str(v) for v in self._get_slider_values('sys_rampdown'))
            ok &= _sysfs_write(f"{BASE_PATH}/fan3/rampup_curve", up_str)
            ok &= _sysfs_write(f"{BASE_PATH}/fan3/rampdown_curve", dn_str)
        elif sys_mode == 'fixed':
            lv = self._sys_level.get()
            ok &= _sysfs_write(f"{BASE_PATH}/fan3/level", str(lv))

        ok &= _sysfs_write(f"{BASE_PATH}/fan3/mode", sys_mode)

        # ── APU Power Mode ──
        pw = self._power_mode.get()
        ok &= _sysfs_write(f"{BASE_PATH}/apu/power_mode", pw)

        # ── Persistieren: fancontrol.service aktualisieren/erstellen ──
        svc_ok = self._persist_to_service(cpu_mode, sys_mode, pw)
        
        # Status anzeigen
        if self._status_lbl:
            if ok and svc_ok:
                txt = T[LANG]['cfg_applied'] + " + Service"
                self._status_lbl.config(text=txt, fg=C_GREEN)
            elif ok:
                self._status_lbl.config(text=T[LANG]['cfg_applied'], fg=C_YELLOW)
            else:
                self._status_lbl.config(text=T[LANG]['cfg_error'], fg=C_RED)

    def _persist_to_service(self, cpu_mode, sys_mode, power_mode):
        """Erstellt oder aktualisiert /etc/systemd/system/fancontrol.service.
        
        Wenn der Service nicht existiert → wird automatisch installiert + enabled.
        Liefert True bei Erfolg.
        """
        # Aktuelle Werte sammeln (Slider haben Vorrang vor State)
        cpu_up = self._get_slider_values('cpu_rampup')
        cpu_dn = self._get_slider_values('cpu_rampdown')
        sys_up = self._get_slider_values('sys_rampup')
        sys_dn = self._get_slider_values('sys_rampdown')

        # Service-Inhalt generieren
        lines = [
            "[Unit]",
            "Description=Set Strix Halo fan curves (managed by AXB35 Monitor)",
            "After=multi-user.target",
            "",
            "[Service]",
            "Type=oneshot",
            "RemainAfterExit=yes",
            "",
        ]

        def _fan_lines(fan, mode, up_vals, dn_vals, level):
            """ExecStart-Zeilen für einen Lüfter generieren."""
            out = []
            if mode == 'curve':
                up_str = ','.join(str(v) for v in up_vals)
                dn_str = ','.join(str(v) for v in dn_vals)
                out.append(f"ExecStart=/bin/sh -c 'echo \"{up_str}\" > /sys/devices/virtual/ec_su_axb35/fan{fan}/rampup_curve'")
                out.append(f"ExecStart=/bin/sh -c 'echo \"{dn_str}\" > /sys/devices/virtual/ec_su_axb35/fan{fan}/rampdown_curve'")
            elif mode == 'fixed':
                out.append(f"ExecStart=/bin/sh -c 'echo \"{level}\" > /sys/devices/virtual/ec_su_axb35/fan{fan}/level'")
            # Mode-Zeile (immer, außer "auto" – dann skippt der Treiber ohnehin)
            if mode != 'auto':
                out.append(f"ExecStart=/bin/sh -c 'echo \"{mode}\" > /sys/devices/virtual/ec_su_axb35/fan{fan}/mode'")
            return out

        # CPU-Kühler (Fan 1 + Fan 2)
        cpu_lv = self._cpu_level.get()
        lines.extend(_fan_lines(1, cpu_mode, cpu_up, cpu_dn, cpu_lv))
        lines.append("")
        lines.extend(_fan_lines(2, cpu_mode, cpu_up, cpu_dn, cpu_lv))
        lines.append("")

        # System-Lüfter (Fan 3)
        sys_lv = self._sys_level.get()
        lines.extend(_fan_lines(3, sys_mode, sys_up, sys_dn, sys_lv))
        lines.append("")

        # Power Mode
        if power_mode != 'balanced':  # nur schreiben wenn nicht Default
            lines.append(f"ExecStart=/bin/sh -c 'echo \"{power_mode}\" > /sys/devices/virtual/ec_su_axb35/apu/power_mode'")
            lines.append("")

        lines.extend(["[Install]", "WantedBy=multi-user.target", ""])

        service_content = '\n'.join(lines)

        # Per sudo schreiben + daemon-reload (+ enable falls neu)
        svc_path = "/etc/systemd/system/fancontrol.service"
        
        # Prüfen ob Service schon existiert (für enable-Logik)
        exists_check = subprocess.run(
            ['test', '-f', svc_path], capture_output=True
        )
        needs_enable = (exists_check.returncode != 0)

        # Alles in einem sudo-Aufruf: schreiben + reload (+ optional enable)
        cmd_parts = [f'cat > {svc_path} << \'SVC_EOF\'']
        cmd_parts.append(service_content)
        cmd_parts.append('SVC_EOF')
        cmd_parts.append('systemctl daemon-reload')
        if needs_enable:
            cmd_parts.append('systemctl enable fancontrol 2>/dev/null || true')

        full_cmd = '\n'.join(cmd_parts)
        
        try:
            r = subprocess.run(['sudo', '-n', 'sh', '-c', full_cmd],
                             capture_output=True, text=True, timeout=10)
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _get_slider_values(self, key):
        """Aktuelle Werte aus den Slider-Widgets lesen (falls vorhanden)."""
        if key in self._sliders:
            try:
                return [int(s.get()) for s in self._sliders[key]]
            except tk.TclError:
                pass
        # Fallback auf State-Liste
        fallback = {
            'cpu_rampup': self._cpu_rampup, 'cpu_rampdown': self._cpu_rampdown,
            'sys_rampup': self._sys_rampup, 'sys_rampdown': self._sys_rampdown,
        }
        return list(fallback.get(key, [0] * 5))

    def close(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass


# ─── Haupt-App ────────────────────────────────────────────────────────────
class SystemMonitorApp:

    def __init__(self, root):
        self.root = root
        self.running = True
        self._prev_cpu_stat = None

        # Zoom / Skalierung (Ctrl+= / Ctrl+- / Ctrl+0)
        self.scale = 1.0
        self._zoom_targets = []   # list of (widget, family, base_size, style)

        # Widget-Referenzen für Auslastungsbalken (werden beim Bauen gefüllt)
        self.usage_w = {}   # key → {'canvas', 'rect', 'value_lbl'}

        self.setup_window()
        self.create_widgets()

        # Gespeicherte Zoom-Stufe anwenden (Widgets müssen existieren)
        if self.scale != 1.0:
            self._apply_zoom()

        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()

    # ─── Fenster ────────────────────────────────────────────────────────
    def setup_window(self):
        self.root.title("Axb35-monitor")
        self.root.configure(bg=C_BG)

        # Gespeicherte Config laden (Zoom + Position aus letzter Session)
        cfg = load_config()
        saved_scale = cfg.get('scale', 1.0)
        saved_x     = cfg.get('win_x')
        saved_y     = cfg.get('win_y')

        # Fenstergröße basierend auf gespeicherter Zoom-Stufe
        base_w, base_h = 720, 430
        w = max(360, int(base_w * saved_scale))
        h = max(240, int(base_h * saved_scale))
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(max(320, int(500 * saved_scale)), max(200, int(320 * saved_scale)))

        # Position: gespeichert oder zentriert
        if saved_x is not None and saved_y is not None:
            x, y = saved_x, saved_y
        else:
            self.root.update_idletasks()
            w = self.root.winfo_width() or 720
            h = self.root.winfo_height() or 430
            x = (self.root.winfo_screenwidth() - w) // 2
            y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{x}+{y}")

        # Gespeicherte Zoom-Stufe anwenden (nach Widget-Erstellung)
        if saved_scale != 1.0:
            self.scale = round(saved_scale, 1)

        # Zoom-Bindings: Ctrl+= / Ctrl+- / Ctrl+0 (inkl. Numpad)
        self.root.bind("<Control-plus>", lambda e: self._zoom(0.1))
        self.root.bind("<Control-equal>", lambda e: self._zoom(0.1))
        self.root.bind("<Control-KP_Add>", lambda e: self._zoom(0.1))
        self.root.bind("<Control-minus>", lambda e: self._zoom(-0.1))
        self.root.bind("<Control-KP_Subtract>", lambda e: self._zoom(-0.1))
        self.root.bind("<Control-0>", lambda e: self._zoom_reset())
        self.root.bind("<Control-KP_0>", lambda e: self._zoom_reset())

    # ─── Widgets bauen ──────────────────────────────────────────────────
    def create_widgets(self):
        main = tk.Frame(self.root, bg=C_BG)
        main.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        self._build_temp_section(main)
        self._build_usage_section(main)
        self._build_fans_section(main)
        self._build_bottom_bar(main)

    # ─── Sektion 1: Temperaturen (CPU + GPU + NVMe nebeneinander) ──────
    def _build_temp_section(self, parent):
        frame = tk.Frame(parent, bg=C_SURFACE)
        frame.pack(fill=tk.X, pady=(0, 8))

        inner = tk.Frame(frame, bg=C_SURFACE)
        inner.pack(padx=12, pady=6)

        # CPU Temp (links)
        cpu_col = tk.Frame(inner, bg=C_SURFACE)
        cpu_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))

        _lbl = tk.Label(cpu_col, text="CPU", font=("Segoe UI", 9),
                 bg=C_SURFACE, fg=C_DIM); _lbl.pack()
        self._reg(_lbl, "Segoe UI", 9)
        self.cpu_temp_lbl = tk.Label(
            cpu_col, text="--°C", font=("Segoe UI", 26, "bold"),
            bg=C_SURFACE, fg=C_GREEN)
        self.cpu_temp_lbl.pack()
        self._reg(self.cpu_temp_lbl, "Segoe UI", 26, "bold")
        self.cpu_temp_sub = tk.Label(
            cpu_col, text="Min: --°  Max: --°", font=("Segoe UI", 9),
            bg=C_SURFACE, fg=C_DIM)
        self.cpu_temp_sub.pack(pady=(2, 0))
        self._reg(self.cpu_temp_sub, "Segoe UI", 9)

        # Trennlinie 1
        tk.Frame(inner, bg=C_BAR_BG, width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=6, pady=4)

        # GPU Temp (Mitte)
        gpu_col = tk.Frame(inner, bg=C_SURFACE)
        gpu_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))

        _lbl = tk.Label(gpu_col, text="GPU", font=("Segoe UI", 9),
                 bg=C_SURFACE, fg=C_DIM); _lbl.pack()
        self._reg(_lbl, "Segoe UI", 9)
        self.gpu_temp_lbl = tk.Label(
            gpu_col, text="--°C", font=("Segoe UI", 26, "bold"),
            bg=C_SURFACE, fg=C_GREEN)
        self.gpu_temp_lbl.pack()
        self._reg(self.gpu_temp_lbl, "Segoe UI", 26, "bold")
        self.gpu_temp_sub = tk.Label(
            gpu_col, text="Radeon 8060S", font=("Segoe UI", 9),
            bg=C_SURFACE, fg=C_DIM)
        self.gpu_temp_sub.pack(pady=(2, 0))
        self._reg(self.gpu_temp_sub, "Segoe UI", 9)

        # Trennlinie 2
        tk.Frame(inner, bg=C_BAR_BG, width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=6, pady=4)

        # NVMe Temp (rechts)
        nvme_col = tk.Frame(inner, bg=C_SURFACE)
        nvme_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))

        _lbl = tk.Label(nvme_col, text="NVMe", font=("Segoe UI", 9),
                 bg=C_SURFACE, fg=C_DIM); _lbl.pack()
        self._reg(_lbl, "Segoe UI", 9)
        self.nvme_temp_lbl = tk.Label(
            nvme_col, text="--°C", font=("Segoe UI", 26, "bold"),
            bg=C_SURFACE, fg=C_GREEN)
        self.nvme_temp_lbl.pack()
        self._reg(self.nvme_temp_lbl, "Segoe UI", 26, "bold")
        self.nvme_temp_sub = tk.Label(
            nvme_col, text="SSD", font=("Segoe UI", 9),
            bg=C_SURFACE, fg=C_DIM)
        self.nvme_temp_sub.pack(pady=(2, 0))
        self._reg(self.nvme_temp_sub, "Segoe UI", 9)

        # Trennlinie 3
        tk.Frame(inner, bg=C_BAR_BG, width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=6, pady=4)

        # NVMe 2 (intern – kommt demnächst)
        nvme2_col = tk.Frame(inner, bg=C_SURFACE)
        nvme2_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))

        _lbl = tk.Label(nvme2_col, text="NVMe2", font=("Segoe UI", 9),
                 bg=C_SURFACE, fg=C_DIM); _lbl.pack()
        self._reg(_lbl, "Segoe UI", 9)
        self.nvme2_temp_lbl = tk.Label(
            nvme2_col, text="--°C", font=("Segoe UI", 26, "bold"),
            bg=C_SURFACE, fg=C_DIM)
        self.nvme2_temp_lbl.pack()
        self._reg(self.nvme2_temp_lbl, "Segoe UI", 26, "bold")
        self.nvme2_temp_sub = tk.Label(
            nvme2_col, text="SSD", font=("Segoe UI", 9),
            bg=C_SURFACE, fg=C_DIM)
        self.nvme2_temp_sub.pack(pady=(2, 0))
        self._reg(self.nvme2_temp_sub, "Segoe UI", 9)

        # Trennlinie 4
        tk.Frame(inner, bg=C_BAR_BG, width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=6, pady=4)

        # Ext. NVMe (rechts, USB-SSD – nur wenn angeschlossen)
        ext_col = tk.Frame(inner, bg=C_SURFACE)
        ext_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))

        _lbl = tk.Label(ext_col, text="EXT", font=("Segoe UI", 9),
                 bg=C_SURFACE, fg=C_DIM); _lbl.pack()
        self._reg(_lbl, "Segoe UI", 9)
        self.ext_temp_lbl = tk.Label(
            ext_col, text="--°C", font=("Segoe UI", 26, "bold"),
            bg=C_SURFACE, fg=C_DIM)
        self.ext_temp_lbl.pack()
        self._reg(self.ext_temp_lbl, "Segoe UI", 26, "bold")
        self.ext_temp_sub = tk.Label(
            ext_col, text="USB-SSD", font=("Segoe UI", 9),
            bg=C_SURFACE, fg=C_DIM)
        self.ext_temp_sub.pack(pady=(2, 0))
        self._reg(self.ext_temp_sub, "Segoe UI", 9)

    # ─── Sektion 2: Auslastung (CPU%, GPU%, RAM, VRAM, NVMe) ──────────
    def _build_usage_section(self, parent):
        frame = tk.Frame(parent, bg=C_BG)
        frame.pack(fill=tk.X, pady=(0, 12))

        self._add_usage_row(frame, "CPU",  "cpu")
        self._add_usage_row(frame, "GPU",  "gpu")
        self._add_usage_row(frame, "RAM",  "ram")
        self._add_usage_row(frame, "VRAM", "vram")
        self._add_usage_row(frame, "NVMe",  "nvme")
        self._add_usage_row(frame, "NVMe2", "nvme2")
        self._add_usage_row(frame, "EXT",   "ext")

    def _add_usage_row(self, parent, label_text, key):
        row = tk.Frame(parent, bg=C_BG)
        row.pack(fill=tk.X, pady=1)

        # Label links
        _lbl = tk.Label(row, text=label_text, font=("Segoe UI", 10),
                 bg=C_BG, fg=C_TEXT, width=6, anchor="w"); _lbl.pack(side=tk.LEFT)
        self._reg(_lbl, "Segoe UI", 10)

        # Balken (Canvas)
        canvas = tk.Canvas(row, height=8, bg=C_BAR_BG, bd=0, highlightthickness=0)
        canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6), pady=2)
        rect_id = canvas.create_rectangle(0, 0, 2, 8, fill=C_BLUE, outline="")

        # Wert rechts (width=16 reicht für "919.0/1831.7 GB" + gibt Balken mehr Platz)
        val_lbl = tk.Label(row, text="--", font=("Consolas", 10),
                           bg=C_BG, fg=C_DIM, width=16, anchor="e")
        val_lbl.pack(side=tk.RIGHT)
        self._reg(val_lbl, "Consolas", 10)

        self.usage_w[key] = {
            'canvas': canvas,
            'rect': rect_id,
            'value_lbl': val_lbl,
        }

    # ─── Sektion 3: Lüfter (bestehend + ⚙️ Config-Button) ──────────────
    def _build_fans_section(self, parent):
        container = tk.Frame(parent, bg=C_BG)
        container.pack(fill=tk.X, pady=(0, 12))

        # ⚙️ Config-Icon (rechts oben in der Lüfter-Sektion)
        self._config_win = None  # Referenz auf FanConfigWindow
        gear_btn = tk.Button(container, text="⚙", font=("Segoe UI", 14),
                            command=self._open_config_gui,
                            bg=C_BG, fg=C_DIM, activebackground=C_SURFACE,
                            activeforeground=C_TEXT, relief=tk.FLAT, bd=0,
                            highlightthickness=0, cursor="hand2", padx=6)
        gear_btn.pack(side=tk.RIGHT, pady=(0, 4))

        names = [T[LANG]['fan1'], T[LANG]['fan2'], T[LANG]['fan3']]
        self.fan_w = []

        for i, name in enumerate(names):
            w = self._create_fan_row(container, i, name)
            self.fan_w.append(w)

    def _open_config_gui(self):
        """Config-Fenster öffnen (oder fokussieren wenn schon offen)."""
        if self._config_win is not None:
            try:
                if self._config_win.win.winfo_exists():
                    self._config_win.win.lift()
                    self._config_win.win.focus_force()
                    return
            except tk.TclError:
                pass
        self._config_win = FanConfigWindow(self.root)

    def _create_fan_row(self, parent, idx, name):
        row = tk.Frame(parent, bg=C_BG)
        row.pack(fill=tk.X, pady=1)

        # Links: Name + Modus-Badge
        left = tk.Frame(row, bg=C_BG)
        left.pack(side=tk.LEFT)

        _lbl = tk.Label(left, text=name, font=("Segoe UI", 10),
                 bg=C_BG, fg=C_TEXT, width=14, anchor="w"); _lbl.pack(side=tk.LEFT)
        self._reg(_lbl, "Segoe UI", 10)

        mode_badge = tk.Label(left, text="[ -- ]", font=("Consolas", 9, "bold"),
                              bg=C_BG, fg=C_CYAN, padx=3)
        mode_badge.pack(side=tk.LEFT, padx=(6, 0))
        self._reg(mode_badge, "Consolas", 9, "bold")

        # Rechts: RPM-Wert
        _lbl = tk.Label(row, text="RPM", font=("Segoe UI", 9),
                 bg=C_BG, fg=C_DIM); _lbl.pack(side=tk.RIGHT, padx=(8, 0))
        self._reg(_lbl, "Segoe UI", 9)

        rpm_lbl = tk.Label(row, text="----", font=("Consolas", 12, "bold"),
                           bg=C_BG, fg=C_TEXT, width=5, anchor="e")
        rpm_lbl.pack(side=tk.RIGHT)
        self._reg(rpm_lbl, "Consolas", 12, "bold")

        # Mitte: Balken
        bar_canvas = tk.Canvas(row, height=8, bg=C_BAR_BG, bd=0, highlightthickness=0)
        bar_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 10), pady=2)
        bar_rect = bar_canvas.create_rectangle(0, 0, 2, 8, fill=C_BLUE, outline="")

        return {
            'mode_badge': mode_badge,
            'rpm_lbl': rpm_lbl,
            'bar_canvas': bar_canvas,
            'bar_rect': bar_rect,
        }

    # ─── Sektion 4: Bottom Bar (Power links + Zoom-Legende rechts) ──────
    def _build_bottom_bar(self, parent):
        frame = tk.Frame(parent, bg=C_SURFACE)
        frame.pack(fill=tk.X)

        inner = tk.Frame(frame, bg=C_SURFACE)
        inner.pack(fill=tk.X, padx=14, pady=6)

        # Links: Power + Status
        self.power_lbl = tk.Label(
            inner, text="Power: --", font=("Segoe UI", 9),
            bg=C_SURFACE, fg=C_DIM)
        self.power_lbl.pack(side=tk.LEFT)
        self._reg(self.power_lbl, "Segoe UI", 9)

        self.status_dot = tk.Label(inner, text="●", font=("Segoe UI", 9),
                                   bg=C_SURFACE, fg=C_RED)
        self.status_dot.pack(side=tk.LEFT, padx=(6, 0))
        self._reg(self.status_dot, "Segoe UI", 9)

        self.status_text = tk.Label(inner, text="offline", font=("Segoe UI", 9),
                                    bg=C_SURFACE, fg=C_DIM)
        self.status_text.pack(side=tk.LEFT, padx=(4, 0))
        self._reg(self.status_text, "Segoe UI", 9)

        # Rechts: Zoom-Legende + aktueller %
        self.zoom_pct_lbl = tk.Label(inner, text="100%", font=("Consolas", 9, "bold"),
                                     bg=C_SURFACE, fg=C_TEXT)
        self.zoom_pct_lbl.pack(side=tk.RIGHT)
        self._reg(self.zoom_pct_lbl, "Consolas", 9, "bold")

        zoom_hint = tk.Label(inner, text=T[LANG]['zoom_hint'], font=("Segoe UI", 8),
                             bg=C_SURFACE, fg=C_DIM)
        zoom_hint.pack(side=tk.RIGHT, padx=(0, 12))
        self._reg(zoom_hint, "Segoe UI", 8)

    # ─── Zoom / Skalierung ──────────────────────────────────────────────
    def _reg(self, widget, family, size, style=""):
        """Registriert ein Label für die Zoom-Skalierung."""
        self._zoom_targets.append((widget, family, size, style))

    def _zoom(self, delta):
        """Zoom in/out um delta (z.B. +0.1 oder -0.1)."""
        new_scale = max(0.5, min(2.0, self.scale + delta))
        if new_scale != self.scale:
            # Runden auf 1 Dezimalstelle um Float-Drift zu vermeiden
            self.scale = round(new_scale, 1)
            self._apply_zoom()

    def _zoom_reset(self):
        """Zoom zurücksetzen auf 100%."""
        if self.scale != 1.0:
            self.scale = 1.0
            self._apply_zoom()

    def _apply_zoom(self):
        """Aktuelle Skalierung auf alle registrierten Widgets anwenden + Fenstergröße anpassen."""
        for widget, family, base_size, style in self._zoom_targets:
            new_size = max(4, int(base_size * self.scale))
            font = (family, new_size) if not style else (family, new_size, style)
            try:
                widget.config(font=font)
            except tk.TclError:
                pass  # Widget nicht mehr vorhanden

        # Fenstergröße proportional skalieren
        base_w, base_h = 720, 430
        w = max(360, int(base_w * self.scale))
        h = max(240, int(base_h * self.scale))
        self.root.geometry(f"{w}x{h}")

        # Zoom-% in der Bottom-Bar aktualisieren
        try:
            self.zoom_pct_lbl.config(text=f"{int(self.scale * 100)}%")
        except tk.TclError:
            pass

    # ─── Farben / Logik ─────────────────────────────────────────────────
    @staticmethod
    def temp_color(t):
        """NVMe/SSD – konservativ (70°C ist für SSDs schon warm)."""
        if t < 50:   return C_GREEN
        elif t < 70: return C_YELLOW
        else:        return C_RED

    @staticmethod
    def apu_temp_color(t):
        """CPU & GPU – Strix Halo TJmax = 100°C. Grün=Idle, Gelb=Norm, Rot=kritisch."""
        if t < 60:   return C_GREEN
        elif t < 85: return C_YELLOW
        else:        return C_RED

    @staticmethod
    def pct_color(pct):
        if pct < 60:   return C_GREEN
        elif pct < 85: return C_YELLOW
        else:          return C_RED

    @staticmethod
    def rpm_color(rpm):
        if rpm < 1500:   return C_GREEN
        elif rpm < 3500: return C_YELLOW
        else:            return C_RED

    @staticmethod
    def mode_color(mode):
        return {'auto': C_CYAN, 'fixed': C_YELLOW, 'curve': C_BLUE}.get(mode, C_DIM)

    @staticmethod
    def power_color(mode):
        return {'quiet': C_GREEN, 'balanced': C_YELLOW, 'performance': C_RED}.get(mode, C_DIM)

    # ─── UI-Update (Main-Thread!) ──────────────────────────────────────
    def update_display(self, data):
        # Temperaturen
        if data.get('cpu_temp') is not None:
            t = data['cpu_temp']
            self.cpu_temp_lbl.config(text=f"{t}°C", fg=self.apu_temp_color(t))

        mn = data.get('cpu_temp_min', '--')
        mx = data.get('cpu_temp_max', '--')
        if isinstance(mn, int) or isinstance(mx, int):
            self.cpu_temp_sub.config(
                text=f"Min: {mn}°  Max: {mx}°")

        if data.get('gpu_temp') is not None:
            t = data['gpu_temp']
            self.gpu_temp_lbl.config(text=f"{t}°C", fg=self.apu_temp_color(t))

        if data.get('nvme_temp') is not None:
            t = data['nvme_temp']
            self.nvme_temp_lbl.config(text=f"{t}°C", fg=self.temp_color(t))

        # NVMe 2 (intern – zeigt -- wenn nicht installiert)
        nvme2_t = data.get('nvme2_temp')
        if nvme2_t is not None:
            self.nvme2_temp_lbl.config(text=f"{nvme2_t}°C", fg=self.temp_color(nvme2_t))
            self.nvme2_temp_sub.config(text="SSD")
        else:
            self.nvme2_temp_lbl.config(text="--°C", fg=C_DIM)
            self.nvme2_temp_sub.config(text="–")

        # Externe NVMe (USB) – zeigt nur wenn angeschlossen
        ext_t = data.get('ext_temp')
        if ext_t is not None:
            self.ext_temp_lbl.config(text=f"{ext_t}°C", fg=self.temp_color(ext_t))
            self.ext_temp_sub.config(text="SSD")
        else:
            self.ext_temp_lbl.config(text="--°C", fg=C_DIM)
            self.ext_temp_sub.config(text=T[LANG]['not_connected'])

        # Auslastungsbalken
        self._update_bar(self.usage_w['cpu'],  data.get('cpu_pct'),
                         f"{data.get('cpu_pct', '--')}%")
        self._update_bar(self.usage_w['gpu'],  data.get('gpu_pct'),
                         f"{data.get('gpu_pct', '--')}%")

        if data.get('ram_pct') is not None:
            txt = (f"{data.get('ram_used_gb', '?')} / "
                   f"{data.get('ram_total_gb', '?')} GB")
            self._update_bar(self.usage_w['ram'], data['ram_pct'], txt)

        if data.get('vram_pct') is not None:
            txt = (f"{data.get('vram_used_gb', '?')} / "
                   f"{data.get('vram_total_gb', '?')} GB")
            self._update_bar(self.usage_w['vram'], data['vram_pct'], txt)

        # NVMe: nur Speicherplatz-Balken (Temp ist jetzt oben)
        nvme_disk = data.get('nvme_disk')
        if nvme_disk is not None:
            txt = f"{nvme_disk['used_gb']}/{nvme_disk['total_gb']} GB"
            self._update_bar(self.usage_w['nvme'], nvme_disk['pct'], txt)

        # NVMe2: Speicherplatz-Balken (Temp ist oben, zeigt -- wenn nicht da)
        nvme2_disk = data.get('nvme2_disk')
        if nvme2_disk is not None:
            txt = f"{nvme2_disk['used_gb']}/{nvme2_disk['total_gb']} GB"
            self._update_bar(self.usage_w['nvme2'], nvme2_disk['pct'], txt)
        else:
            # Nicht installiert → Balken leer + "--"
            bar_w = self.usage_w['nvme2']
            canvas = bar_w['canvas']
            if canvas.winfo_width() > 10:
                canvas.coords(bar_w['rect'], 0, 0, 0, 8)
            bar_w['value_lbl'].config(text="--", fg=C_DIM)

        # Externe SSD: Speicherplatz-Balken (Temp ist oben in der 4. Spalte)
        ext_disk = data.get('ext_disk')
        if ext_disk is not None:
            txt = f"{ext_disk['used_gb']}/{ext_disk['total_gb']} GB"
            self._update_bar(self.usage_w['ext'], ext_disk['pct'], txt)
        else:
            # SSD abgezogen → Balken + Label zurücksetzen
            bar_w = self.usage_w['ext']
            canvas = bar_w['canvas']
            if canvas.winfo_width() > 10:
                canvas.coords(bar_w['rect'], 0, 0, 0, 8)
            bar_w['value_lbl'].config(text="--", fg=C_DIM)

        # Lüfter
        for i in range(3):
            w = self.fan_w[i]
            rpm  = data.get(f'fan{i+1}_rpm')
            mode = data.get(f'fan{i+1}_mode', '--')

            if rpm is not None:
                color = self.rpm_color(rpm)
                w['rpm_lbl'].config(text=str(rpm), fg=color)
                canvas = w['bar_canvas']
                total_w = canvas.winfo_width()
                if total_w > 10:
                    ratio = min(rpm / MAX_RPM_BAR, 1.0)
                    fill_w = max(int(total_w * ratio), 3)
                    canvas.coords(w['bar_rect'], 0, 0, fill_w, 8)
                canvas.itemconfig(w['bar_rect'], fill=color)

            if mode:
                w['mode_badge'].config(text=f"[ {mode} ]", fg=self.mode_color(mode))

        # Power-Modus + Status
        power = data.get('power_mode')
        if power:
            self.power_lbl.config(text=f"Power: {power}", fg=self.power_color(power))

        has_data = (data.get('cpu_temp') is not None or
                    any(data.get(f'fan{i}_rpm') is not None for i in range(1, 4)))
        if has_data:
            self.status_dot.config(fg=C_GREEN)
            self.status_text.config(text="online", fg=C_DIM)

    def _update_bar(self, bar_w, pct, value_text):
        """Aktualisiert einen Auslastungsbalken + Wert-Label."""
        if pct is None:
            return
        color = self.pct_color(pct)
        canvas = bar_w['canvas']
        total_w = canvas.winfo_width()
        if total_w > 10:
            fill_w = max(int(total_w * (pct / 100.0)), 3)
            canvas.coords(bar_w['rect'], 0, 0, fill_w, 8)
        canvas.itemconfig(bar_w['rect'], fill=color)
        if value_text:
            bar_w['value_lbl'].config(text=value_text, fg=C_TEXT)

    # ─── Hintergrund-Thread ─────────────────────────────────────────────
    def monitor_loop(self):
        while self.running:
            data = {}

            # CPU-Temperatur (Treiber)
            data['cpu_temp']     = read_int(f"{BASE_PATH}/temp1/temp")
            data['cpu_temp_min'] = read_int(f"{BASE_PATH}/temp1/min")
            data['cpu_temp_max'] = read_int(f"{BASE_PATH}/temp1/max")

            # GPU-Temperatur (amdgpu hwmon, m°C → °C)
            gpu_raw = read_int(f"{HWMON_AMDGPU}/temp1_input")
            if gpu_raw is not None:
                data['gpu_temp'] = round(gpu_raw / 1000.0)

            # NVMe-Temperatur (intern 1, hwmon, m°C → °C)
            nvme_raw = read_int(f"{HWMON_NVME}/temp1_input")
            if nvme_raw is not None:
                data['nvme_temp'] = round(nvme_raw / 1000.0)

            # NVMe-Temperatur (intern 2 – kommt demnächst, zeigt -- wenn nicht da)
            nvme2_raw = read_int(f"{HWMON_NVME2}/temp1_input")
            if nvme2_raw is not None:
                data['nvme2_temp'] = round(nvme2_raw / 1000.0)

            # Externe NVMe (USB, via smartctl – nur wenn angeschlossen)
            ext = get_ext_nvme()
            if ext:
                data['ext_temp']  = ext['temp']
                data['ext_model'] = ext.get('model', 'Ext. SSD')

            # CPU-Auslastung (Delta aus /proc/stat)
            cpu_pct, self._prev_cpu_stat = get_cpu_usage(self._prev_cpu_stat)
            if cpu_pct is not None:
                data['cpu_pct'] = cpu_pct

            # GPU-Auslastung
            gpu_busy = read_int(f"{DRM_DEVICE}/gpu_busy_percent")
            if gpu_busy is not None:
                data['gpu_pct'] = gpu_busy

            # RAM
            ram = get_ram_usage()
            if ram:
                data.update(ram)
                data['ram_pct']      = ram['pct']
                data['ram_used_gb']  = ram['used_gb']
                data['ram_total_gb'] = ram['total_gb']

            # VRAM
            vram = get_vram_usage()
            if vram:
                data['vram_pct']      = vram['pct']
                data['vram_used_gb']  = vram['used_gb']
                data['vram_total_gb'] = vram['total_gb']

            # NVMe-Speicherplatz (intern 1)
            nvme_disk = get_disk_usage()
            if nvme_disk:
                data['nvme_disk'] = nvme_disk

            # NVMe2-Speicherplatz (intern 2 – falls vorhanden)
            nvme2_disk = get_nvme2_disk_usage()
            if nvme2_disk:
                data['nvme2_disk'] = nvme2_disk

            # Externe SSD – Speicherplatz
            ext_disk = get_ext_disk_usage()
            if ext_disk:
                data['ext_disk'] = ext_disk

            # Lüfter (Treiber)
            for i in range(1, 4):
                rpm_raw = read_int(f"{BASE_PATH}/fan{i}/rpm")
                if rpm_raw is not None:
                    data[f'fan{i}_rpm'] = rpm_raw

                mode_raw = read_sysfs(f"{BASE_PATH}/fan{i}/mode")
                if mode_raw:
                    data[f'fan{i}_mode'] = mode_raw

            # Power-Modus
            power_raw = read_sysfs(f"{BASE_PATH}/apu/power_mode")
            if power_raw:
                data['power_mode'] = power_raw

            # Thread-sicheres UI-Update
            try:
                self.root.after(0, lambda d=data: self.update_display(d))
            except tk.TclError:
                break  # Fenster geschlossen

            time.sleep(UPDATE_INTERVAL)

    # ─── Aufräumen ─────────────────────────────────────────────────────
    def on_closing(self):
        self.running = False
        try:
            self.monitor_thread.join(timeout=2.0)
        except RuntimeError:
            pass
        # Config-Fenster mit schließen (falls offen)
        if hasattr(self, '_config_win') and self._config_win is not None:
            self._config_win.close()

        # Zoom + Fensterposition speichern für nächste Session
        try:
            save_config(
                scale=self.scale,
                win_x=self.root.winfo_x(),
                win_y=self.root.winfo_y()
            )
        except Exception:
            pass

        self.root.destroy()


# ─── Einstiegspunkt ──────────────────────────────────────────────────────
def main():
    if not os.path.isdir(BASE_PATH):
        print("╔══════════════════════════════════════════════════╗")
        print("║  ⚠️  Treiber nicht gefunden!                   ║")
        print(f"║  Erwartet: {BASE_PATH}   ║")
        print("║                                            ║")
        print("║  Prüfe: lsmod | grep ec_su_axb35          ║")
        print("╚══════════════════════════════════════════════════╝")
        sys.exit(1)

    root = tk.Tk(className="Axb35-monitor")
    app = SystemMonitorApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
