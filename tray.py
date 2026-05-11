"""
System-tray daemon. Renders 4 always-visible icons in the notification area:
Claude 5h, Claude 7d, Codex 5h, Codex 7d. Each icon shows the percentage
remaining inside a donut.

Click any icon to open the native stats dashboard.
Right-click any icon for: Refresh now / Open dashboard / Quit.

Designed to run via pythonw at logon through the current user's Run key.

Usage:
    pythonw tray.py
    python  tray.py --install-startup
    python  tray.py --uninstall-startup
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import winreg
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import pystray

HERE = Path(__file__).resolve().parent
WIDGET_PY = HERE / "widget.py"
PROBE_PY  = HERE / "probe_claude.py"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "AI Usage Tray"
_dashboard_proc: subprocess.Popen | None = None
_dashboard_lock = threading.Lock()

ICON_SIZE = 64           # rendered, downscaled by tray to 16x16/32x32 as needed
POLL_SECONDS = 60        # usage doesn't move fast enough to refresh more often

ACCENT = {
    "claude": (217, 119, 87, 255),
    "codex":  (16, 163, 127, 255),
}
BG = (24, 24, 28, 0)        # transparent — system applies its own background
TEXT_FG = (245, 245, 245, 255)


def find_python(name: str) -> Path:
    current = Path(sys.executable)
    candidates = [
        current if current.name.lower() == name.lower() else current.with_name(name),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python312" / name,
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python311" / name,
    ]
    for c in candidates:
        if c.exists():
            return c
    raise FileNotFoundError(name)


def load_font(px: int) -> ImageFont.ImageFont:
    """Try Segoe UI Bold for crisp Win11 look; fall back to default."""
    for p in (
        r"C:\Windows\Fonts\seguibl.ttf",   # Segoe UI Black
        r"C:\Windows\Fonts\seguisb.ttf",   # Segoe UI Semibold
        r"C:\Windows\Fonts\segoeui.ttf",
    ):
        try:
            return ImageFont.truetype(p, px)
        except OSError:
            continue
    return ImageFont.load_default()


def render_icon(value: str, accent: tuple, utilization: float | None) -> Image.Image:
    """Tray icon: solid disk in accent colour with a dark pie slice for the
    portion of the window already used. Text sits on top of the fill.

    At 64x64 source -> 16-32 px in tray; a filled disk is far more visible
    than a thin donut ring.
    """
    size = ICON_SIZE
    img = Image.new("RGBA", (size, size), BG)
    d = ImageDraw.Draw(img)

    pad = 2
    box = (pad, pad, size - pad - 1, size - pad - 1)

    # 1) Filled accent disk (this is the "remaining capacity").
    d.ellipse(box, fill=accent)

    # 2) Dark "used" pie slice on top, sized to the utilization. Drawn from
    # 12 o'clock clockwise so a small slice ~= a little burned, a big slice
    # ~= nearly out.
    if utilization is not None and utilization > 0.5:
        used_sweep = max(0.0, min(360.0, float(utilization) * 3.6))
        used_color = (28, 28, 32, 235)
        d.pieslice(box, start=-90, end=-90 + used_sweep, fill=used_color)

    # 3) Subtle border so the disk reads cleanly against the taskbar BG
    border = (255, 255, 255, 35)
    d.ellipse(box, outline=border, width=1)

    # 4) Big number on top — scale down if it gets cramped.
    text = value
    for px in (34, 30, 26, 22, 18):
        font = load_font(px)
        l, t, r, b = d.textbbox((0, 0), text, font=font)
        tw, th = r - l, b - t
        if tw <= size - 10 and th <= size - 10:
            break
    x = (size - tw) // 2 - l
    y = (size - th) // 2 - t
    # Slight shadow for legibility regardless of fill behind it.
    shadow = (0, 0, 0, 180)
    d.text((x + 1, y + 1), text, font=font, fill=shadow)
    d.text((x, y), text, font=font, fill=TEXT_FG)

    return img


def fmt_pct(x: float | None) -> str:
    return f"{x:.0f}%" if x is not None else "—"


# ----------------------------- data acquisition ---------------------------------

class Stats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.snapshot: dict = {}

    def refresh(self) -> dict:
        py = find_python("python.exe")
        data_error = None
        try:
            raw = subprocess.check_output(
                [str(py), str(WIDGET_PY), "--json"],
                stderr=subprocess.DEVNULL, encoding="utf-8", timeout=15,
                creationflags=NO_WINDOW,
            )
            data = json.loads(raw)
        except Exception as e:
            data_error = str(e)
            data = None

        try:
            raw = subprocess.check_output(
                [str(py), str(PROBE_PY)],
                stderr=subprocess.DEVNULL, encoding="utf-8", timeout=15,
                creationflags=NO_WINDOW,
            )
            plan = json.loads(raw or "{}")
        except Exception:
            plan = {}

        if data is None:
            with self._lock:
                if self.snapshot:
                    self.snapshot = dict(self.snapshot)
                    self.snapshot["error"] = data_error
                    return self.snapshot
            data = {}

        def util(key: str) -> float | None:
            try:
                return float(plan[key]) * 100
            except (KeyError, TypeError, ValueError):
                return None

        claude_5h_used = util("anthropic-ratelimit-unified-5h-utilization")
        claude_7d_used = util("anthropic-ratelimit-unified-7d-utilization")

        def left(u): return None if u is None else max(0.0, 100 - u)

        def codex_snapshot(bucket):
            if not bucket:
                return {"used": None, "left": None, "tokens": None, "fallback": False}
            try:
                used = float(bucket.get("used_percent", 0) or 0)
            except (TypeError, ValueError):
                used = None
            return {"used": used, "left": left(used), "tokens": None, "fallback": False}

        # Fallback for Claude: when the plan probe isn't available, surface
        # tokens-used-in-window so the icons still show actual data instead
        # of "—". We render those as raw counts (e.g. "57M").
        windows = data.get("windows", {}) or {}
        claude_5h_tokens = windows.get("claude_5h_tokens")
        claude_7d_tokens = windows.get("claude_week_tokens")

        def claude_snapshot(used_pct, fallback_tokens):
            if used_pct is not None:
                return {"used": used_pct, "left": left(used_pct), "tokens": None, "fallback": False}
            return {"used": None, "left": None, "tokens": fallback_tokens, "fallback": True}

        rl = (data.get("codex") or {}).get("rate_limits") or {}

        with self._lock:
            self.snapshot = {
                "claude_5h": claude_snapshot(claude_5h_used, claude_5h_tokens),
                "claude_7d": claude_snapshot(claude_7d_used, claude_7d_tokens),
                "codex_5h":  codex_snapshot(rl.get("primary") or {}),
                "codex_7d":  codex_snapshot(rl.get("secondary") or {}),
                "generated_at": data.get("generated_at", time.strftime("%a %b %d · %I:%M %p")),
            }
            return self.snapshot

    def get(self) -> dict:
        with self._lock:
            return dict(self.snapshot)


# ----------------------------- tray glue ----------------------------------------

ICONS = [
    ("claude_5h", "Claude 5h", ACCENT["claude"]),
    ("claude_7d", "Claude 7d", ACCENT["claude"]),
    ("codex_5h",  "Codex 5h",  ACCENT["codex"]),
    ("codex_7d",  "Codex 7d",  ACCENT["codex"]),
]


def open_dashboard(_icon=None, _item=None) -> None:
    global _dashboard_proc
    with _dashboard_lock:
        if _dashboard_proc and _dashboard_proc.poll() is None:
            return

    py = find_python("pythonw.exe")
    native = HERE / "widget_native.py"
    _dashboard_proc = subprocess.Popen(
        [str(py), str(native)],
        creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0),
    )


def build_icon(slug: str, label: str, accent: tuple, stats: Stats,
               force_refresh) -> pystray.Icon:
    snap = stats.get().get(slug, {})
    value = fmt_pct(snap.get("left"))
    util  = snap.get("used")
    image = render_icon(value, accent, util)

    def click(icon, item=None):
        open_dashboard()

    def refresh_clicked(icon, item):
        force_refresh()

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", click, default=True),
        pystray.MenuItem("Refresh now",    refresh_clicked),
        pystray.MenuItem("Quit",           lambda i, _: i.stop_all() if hasattr(i, 'stop_all') else i.stop()),
    )
    title = f"{label}: {value} left"
    return pystray.Icon(name=f"ai-usage-{slug}",
                        icon=image, title=title, menu=menu)


def main_loop() -> int:
    stats = Stats()
    stats.refresh()

    # Build all four icons.
    icons: list[pystray.Icon] = []
    threads: list[threading.Thread] = []
    refresh_event = threading.Event()

    def force_refresh():
        refresh_event.set()

    for slug, label, accent in ICONS:
        ico = build_icon(slug, label, accent, stats, force_refresh)
        icons.append(ico)
        t = threading.Thread(target=ico.run, daemon=True, name=f"tray-{slug}")
        t.start()
        threads.append(t)

    # Wait briefly for icons to appear in the tray before our first update push.
    time.sleep(0.5)

    def fmt_tok(n):
        n = float(n or 0)
        if n >= 1e9:  return f"{n/1e9:.1f}B"
        if n >= 1e6:  return f"{n/1e6:.0f}M"
        if n >= 1e3:  return f"{n/1e3:.0f}k"
        return f"{int(n)}"

    def update_all() -> None:
        snap = stats.get()
        for ico, (slug, label, accent) in zip(icons, ICONS):
            s = snap.get(slug, {})
            if s.get("fallback"):
                value = fmt_tok(s.get("tokens"))
                util  = None
                title = f"{label}: {value} tokens used (plan % unavailable)"
            else:
                value = fmt_pct(s.get("left"))
                util  = s.get("used")
                title = f"{label}: {value} left"
            try:
                ico.icon  = render_icon(value, accent, util)
                ico.title = title
            except Exception:
                pass

    update_all()

    # Refresh once a minute. The "force_refresh" event lets the right-click
    # menu's "Refresh now" cut in early.
    try:
        while True:
            if refresh_event.wait(timeout=POLL_SECONDS):
                refresh_event.clear()
            stats.refresh()
            update_all()
    except KeyboardInterrupt:
        pass
    finally:
        for ico in icons:
            try: ico.stop()
            except Exception: pass

    return 0


# ----------------------------- startup install ----------------------------------

def cleanup_legacy_startup_shortcuts() -> None:
    startup_dir = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    for name in ("AI Usage Tray.lnk", "AI Usage Widget.lnk", "AI Usage Live Refresh.lnk"):
        p = startup_dir / name
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def startup_command(pyw: Path) -> str:
    return f'"{pyw}" "{Path(__file__).resolve()}"'


def install_startup() -> None:
    pyw = find_python("pythonw.exe")
    cleanup_legacy_startup_shortcuts()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command(pyw))
    print(f"Installed startup Run entry: {RUN_VALUE}")
    print("Launching tray now (background)...")
    subprocess.Popen([str(pyw), str(Path(__file__).resolve())],
                     creationflags=NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0))


def uninstall_startup() -> None:
    cleanup_legacy_startup_shortcuts()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
        print(f"Removed startup Run entry: {RUN_VALUE}")
    except FileNotFoundError:
        print(f"No startup Run entry named {RUN_VALUE}.")
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
         "Where-Object { $_.CommandLine -match 'tray.py' } | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        check=False, capture_output=True, creationflags=NO_WINDOW,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-startup",   action="store_true")
    ap.add_argument("--uninstall-startup", action="store_true")
    args = ap.parse_args()

    if args.uninstall_startup:
        uninstall_startup(); return 0
    if args.install_startup:
        install_startup(); return 0
    return main_loop()


if __name__ == "__main__":
    sys.exit(main())
