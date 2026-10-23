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
import time
import subprocess
import threading
import tkinter as tk


# ─── Konfiguration ────────────────────────────────────────────────────────
BASE_PATH     = "/sys/class/ec_su_axb35"
DRM_DEVICE    = "/sys/class/drm/card1/device"       # amdgpu (Radeon 8060S iGPU)
# ─── Dynamische hwmon-Erkennung (Nummern ändern sich nach Reboot!) ─────
def _discover_hwmons():
    """Findet hwmon-Geräte per name-Datei. Robust gegen Index-Änderungen."""
    result = {'amdgpu': None, 'nvme': []}
    try:
        for i in range(20):
            name_file = f"/sys/class/hwmon/hwmon{i}/name"
            if not os.path.exists(name_file):
                continue
            with open(name_file) as nf:
                name = nf.read().strip()
            base = f"/sys/class/hwmon/hwmon{i}"
            if name == "amdgpu":
                result['amdgpu'] = base
            elif name == "nvme":
                result['nvme'].append(base)
    except OSError:
        pass
    return result

_HWMON = _discover_hwmons()
HWMON_AMDGPU  = _HWMON['amdgpu'] if _HWMON['amdgpu'] else "/sys/class/hwmon/hwmon7"
HWMON_NVME    = _HWMON['nvme'][0] if len(_HWMON['nvme']) > 0 else None
HWMON_NVME2   = _HWMON['nvme'][1] if len(_HWMON['nvme']) > 1 else None
UPDATE_INTERVAL = 1.5          # Sekunden zwischen Aktualisierungen

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
        'zoom_hint': 'Strg +/- : Zoom · Strg 0 : Reset',
    },
    'en': {
        'fan1': 'CPU Fan 1',
        'fan2': 'CPU Fan 2',
        'fan3': 'System Fan',
        'not_connected': 'not connected',
        'zoom_hint': 'Ctrl +/- : Zoom · Ctrl 0 : Reset',
    }
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

        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()

    # ─── Fenster ────────────────────────────────────────────────────────
    def setup_window(self):
        self.root.title("Axb35-monitor")
        self.root.geometry("720x430")
        self.root.minsize(680, 410)
        self.root.configure(bg=C_BG)

        # Layout vor Zentrieren finalisieren (damit Canvas-Breiten korrekt sind)
        self.root.update_idletasks()
        w = self.root.winfo_width() or 640
        h = self.root.winfo_height() or 480
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{x}+{y}")

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
        frame.pack(fill=tk.X, pady=(0, 12))

        inner = tk.Frame(frame, bg=C_SURFACE)
        inner.pack(padx=14, pady=12)

        # CPU Temp (links)
        cpu_col = tk.Frame(inner, bg=C_SURFACE)
        cpu_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))

        _lbl = tk.Label(cpu_col, text="CPU", font=("Segoe UI", 9),
                 bg=C_SURFACE, fg=C_DIM); _lbl.pack()
        self._reg(_lbl, "Segoe UI", 9)
        self.cpu_temp_lbl = tk.Label(
            cpu_col, text="--°C", font=("Segoe UI", 32, "bold"),
            bg=C_SURFACE, fg=C_GREEN)
        self.cpu_temp_lbl.pack()
        self._reg(self.cpu_temp_lbl, "Segoe UI", 32, "bold")
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
            gpu_col, text="--°C", font=("Segoe UI", 32, "bold"),
            bg=C_SURFACE, fg=C_GREEN)
        self.gpu_temp_lbl.pack()
        self._reg(self.gpu_temp_lbl, "Segoe UI", 32, "bold")
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
            nvme_col, text="--°C", font=("Segoe UI", 32, "bold"),
            bg=C_SURFACE, fg=C_GREEN)
        self.nvme_temp_lbl.pack()
        self._reg(self.nvme_temp_lbl, "Segoe UI", 32, "bold")
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
            nvme2_col, text="--°C", font=("Segoe UI", 32, "bold"),
            bg=C_SURFACE, fg=C_DIM)
        self.nvme2_temp_lbl.pack()
        self._reg(self.nvme2_temp_lbl, "Segoe UI", 32, "bold")
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
            ext_col, text="--°C", font=("Segoe UI", 32, "bold"),
            bg=C_SURFACE, fg=C_DIM)
        self.ext_temp_lbl.pack()
        self._reg(self.ext_temp_lbl, "Segoe UI", 32, "bold")
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
        self._add_usage_row(frame, "NVMe", "nvme")
        self._add_usage_row(frame, "EXT",  "ext")

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

        # Wert rechts
        val_lbl = tk.Label(row, text="--", font=("Consolas", 10),
                           bg=C_BG, fg=C_DIM, width=22, anchor="e")
        val_lbl.pack(side=tk.RIGHT)
        self._reg(val_lbl, "Consolas", 10)

        self.usage_w[key] = {
            'canvas': canvas,
            'rect': rect_id,
            'value_lbl': val_lbl,
        }

    # ─── Sektion 3: Lüfter (bestehend) ──────────────────────────────────
    def _build_fans_section(self, parent):
        container = tk.Frame(parent, bg=C_BG)
        container.pack(fill=tk.X, pady=(0, 12))

        names = [T[LANG]['fan1'], T[LANG]['fan2'], T[LANG]['fan3']]
        self.fan_w = []

        for i, name in enumerate(names):
            w = self._create_fan_row(container, i, name)
            self.fan_w.append(w)

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

            # GPU-Temperatur (amdgpu hwmon, m°C → °C) – dynamisch erkannt
            if HWMON_AMDGPU:
                gpu_raw = read_int(f"{HWMON_AMDGPU}/temp1_input")
                if gpu_raw is not None:
                    data['gpu_temp'] = round(gpu_raw / 1000.0)

            # NVMe-Temperatur (intern, hwmon, m°C → °C) – dynamisch erkannt
            if HWMON_NVME:
                nvme_raw = read_int(f"{HWMON_NVME}/temp1_input")
                if nvme_raw is not None:
                    data['nvme_temp'] = round(nvme_raw / 1000.0)

            # NVMe2-Temperatur (intern 2, zeigt -- wenn nicht vorhanden)
            if HWMON_NVME2:
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

            # NVMe-Speicherplatz (intern)
            nvme_disk = get_disk_usage()
            if nvme_disk:
                data['nvme_disk'] = nvme_disk

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

    root = tk.Tk(className="axb35-monitor")
    app = SystemMonitorApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
