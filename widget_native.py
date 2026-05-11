"""
Native Tk-based dashboard for AI Usage.

Uses customtkinter for a modern dark Win11-ish look. Reads data via the same
widget.py --json pipeline as the tray.

Usage:
    python widget_native.py
    pythonw widget_native.py    (silent launch, recommended)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import threading
import tkinter as tk
from pathlib import Path
from datetime import datetime

import customtkinter as ctk

HERE      = Path(__file__).resolve().parent
WIDGET_PY = HERE / "widget.py"
ICON_ICO  = HERE / "assets" / "AIUsage.ico"
ICON_PNG  = HERE / "assets" / "logo-256.png"
APP_ID    = "VictorIvanov.AIUsageTray.Dashboard"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
REFRESH_SECONDS = 60

ACCENT_CLAUDE = "#d97757"
ACCENT_CODEX  = "#10a37f"
ACCENT_BLUE   = "#4cc2ff"
ACCENT_PURPLE = "#a85cd4"
BG_PANEL  = "#1c1c24"
BG_PANEL2 = "#23232d"
BG_BASE   = "#13131a"
TEXT      = "#f3f3f5"
MUTED     = "#9094a3"
MUTED_2   = "#6a6e7a"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def set_windows_app_id() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


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


def fmt_num(n, digits=1):
    n = float(n or 0)
    if n >= 1e12: return f"{n/1e12:.{digits}f}T"
    if n >= 1e9:  return f"{n/1e9:.{digits}f}B"
    if n >= 1e6:  return f"{n/1e6:.{digits}f}M"
    if n >= 1e3:  return f"{n/1e3:.0f}k"
    return f"{int(n)}"


def fmt_money(n):
    n = float(n or 0)
    if n >= 1000: return f"${n:,.0f}"
    return f"${n:.2f}"


def fmt_resets_in(unix_sec) -> str:
    if not unix_sec:
        return ""
    try:
        delta = float(unix_sec) - time.time()
    except (TypeError, ValueError):
        return ""
    if delta <= 0:
        return "resets now"
    m = int(delta / 60)
    if m < 60:  return f"resets in {m}m"
    if m < 60*24: return f"resets in {m//60}h"
    return f"resets in {m//60//24}d"


def query() -> dict:
    """Run widget.py --json and parse the result."""
    py = find_python("python.exe")
    try:
        raw = subprocess.check_output(
            [str(py), str(WIDGET_PY), "--json"],
            stderr=subprocess.DEVNULL, encoding="utf-8", timeout=30,
            creationflags=NO_WINDOW,
        )
        return json.loads(raw)
    except Exception as e:
        return {"_error": str(e)}


# ----------------------------------------------------------------------
# UI building blocks
# ----------------------------------------------------------------------

class Gauge(ctk.CTkFrame):
    """Horizontal progress gauge with name, big % left, sub-text, accent bar."""
    def __init__(self, master, name: str, accent: str):
        super().__init__(master, fg_color=BG_PANEL, corner_radius=14)
        self._accent = accent

        # Top accent strip
        self._strip = ctk.CTkFrame(self, fg_color=accent, height=2, corner_radius=0)
        self._strip.pack(fill="x", side="top")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=(10, 12))

        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        self._name = ctk.CTkLabel(top, text=name, font=("Segoe UI", 11, "bold"), text_color=TEXT, anchor="w")
        self._name.pack(side="left")
        self._used = ctk.CTkLabel(top, text="—", font=("Segoe UI", 11), text_color=MUTED, anchor="e")
        self._used.pack(side="right")

        big = ctk.CTkFrame(body, fg_color="transparent")
        big.pack(fill="x", pady=(6, 6))
        self._pct = ctk.CTkLabel(big, text="—", font=("Segoe UI Semibold", 30), text_color=TEXT, anchor="w")
        self._pct.pack(side="left")
        ctk.CTkLabel(big, text=" left", font=("Segoe UI", 11), text_color=MUTED, anchor="w").pack(side="left", pady=(10, 0))

        self._bar = ctk.CTkProgressBar(body, height=6, corner_radius=4,
                                       fg_color="#2a2a35", progress_color=accent)
        self._bar.pack(fill="x")
        self._bar.set(0)

        self._reset = ctk.CTkLabel(body, text="", font=("Segoe UI", 9), text_color=MUTED_2, anchor="w")
        self._reset.pack(fill="x", pady=(4, 0))

    def set_value(self, used_pct: float | None, reset_unix=None):
        if used_pct is None:
            self._pct.configure(text="—")
            self._used.configure(text="—")
            self._bar.set(0)
            self._reset.configure(text="")
            return
        left = max(0.0, 100 - used_pct)
        self._pct.configure(text=f"{left:.0f}%")
        self._used.configure(text=f"{used_pct:.0f}% used")
        self._bar.set(left / 100)
        self._reset.configure(text=fmt_resets_in(reset_unix))


class StatCard(ctk.CTkFrame):
    """Single stat: big number on top, label, optional detail line."""
    def __init__(self, master, label: str, accent: str = BG_PANEL2):
        super().__init__(master, fg_color=accent, corner_radius=14)
        pad = ctk.CTkFrame(self, fg_color="transparent")
        pad.pack(fill="both", expand=True, padx=14, pady=12)
        self._n = ctk.CTkLabel(pad, text="—", font=("Segoe UI Semibold", 22), text_color=TEXT, anchor="w")
        self._n.pack(fill="x")
        self._l = ctk.CTkLabel(pad, text=label, font=("Segoe UI", 10), text_color=MUTED, anchor="w")
        self._l.pack(fill="x", pady=(2, 0))
        self._d = ctk.CTkLabel(pad, text="", font=("Segoe UI", 10), text_color=MUTED_2, anchor="w")
        self._d.pack(fill="x", pady=(2, 0))

    def set(self, big: str, detail: str = ""):
        self._n.configure(text=big)
        self._d.configure(text=detail)


class BarList(ctk.CTkFrame):
    """Horizontal-bar list with name | bar | value, used for projects & tools."""
    def __init__(self, master, title: str, color: str):
        super().__init__(master, fg_color=BG_PANEL, corner_radius=14)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=14, pady=(12, 8))
        ctk.CTkLabel(head, text=title, font=("Segoe UI Semibold", 12), text_color=TEXT, anchor="w").pack(side="left")
        self._body = ctk.CTkFrame(self, fg_color="transparent")
        self._body.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self._color = color

    def set_rows(self, rows: list):
        for child in self._body.winfo_children():
            child.destroy()
        if not rows:
            ctk.CTkLabel(self._body, text="(no data)", text_color=MUTED).pack(anchor="w")
            return
        max_w = max((r["weight"] for r in rows), default=1) or 1
        for r in rows:
            line = ctk.CTkFrame(self._body, fg_color="transparent")
            line.pack(fill="x", pady=2)
            name = ctk.CTkLabel(line, text=r["name"][:38], font=("Segoe UI", 11),
                                text_color=TEXT, anchor="w", width=180)
            name.pack(side="left")
            track = ctk.CTkProgressBar(line, height=8, corner_radius=999,
                                       fg_color="#2a2a35", progress_color=self._color)
            track.pack(side="left", fill="x", expand=True, padx=(8, 8))
            track.set(min(1.0, r["weight"] / max_w))
            val = ctk.CTkLabel(line, text=r["label"], font=("Segoe UI", 11),
                               text_color=MUTED, anchor="e", width=70)
            val.pack(side="right")


class Heatmap(tk.Canvas):
    """7d x 24h grid drawn on a tk.Canvas (customtkinter has no canvas wrapper)."""
    def __init__(self, master, height=140):
        super().__init__(master, bg=BG_PANEL, height=height, highlightthickness=0, bd=0)

    def render(self, grid: list[list[int]]):
        self.delete("all")
        if not grid:
            return
        w = self.winfo_width() or 800
        h = self.winfo_height() or 140
        rows, cols = 7, 24
        pad_left, pad_top = 50, 18
        cell_w = (w - pad_left - 8) / cols
        cell_h = (h - pad_top - 4) / rows

        flat = [v for row in grid for v in row]
        mx = max(flat) if flat else 1

        # Hour labels along the top
        for hr in range(0, 24, 4):
            x = pad_left + hr * cell_w + cell_w / 2
            self.create_text(x, 8, text=str(hr), fill=MUTED_2, font=("Segoe UI", 8))

        today = datetime.now().date()
        for d in range(rows):
            day_date = today
            try:
                from datetime import timedelta
                day_date = today - timedelta(days=d)
            except Exception:
                pass
            self.create_text(pad_left - 8, pad_top + d * cell_h + cell_h / 2,
                             text=day_date.strftime("%a"), fill=MUTED_2,
                             font=("Segoe UI", 8), anchor="e")
            for hr in range(cols):
                v = grid[d][hr] if grid[d] and hr < len(grid[d]) else 0
                if v <= 0:
                    fill = "#1a1a22"
                else:
                    intensity = v / mx
                    # blend BG -> claude
                    alpha = 0.12 + intensity * 0.78
                    r1, g1, b1 = 26, 26, 34
                    r2, g2, b2 = 217, 119, 87
                    r = int(r1 + (r2 - r1) * alpha)
                    g = int(g1 + (g2 - g1) * alpha)
                    b = int(b1 + (b2 - b1) * alpha)
                    fill = f"#{r:02x}{g:02x}{b:02x}"
                x0 = pad_left + hr * cell_w + 1
                y0 = pad_top + d * cell_h + 1
                x1 = x0 + cell_w - 2
                y1 = y0 + cell_h - 2
                self.create_rectangle(x0, y0, x1, y1, fill=fill, outline="")


class ModelDonut(tk.Canvas):
    """Three-segment donut for Opus/Sonnet/Haiku."""
    def __init__(self, master, size=140):
        super().__init__(master, bg=BG_PANEL, width=size, height=size,
                         highlightthickness=0, bd=0)
        self._size = size

    def render(self, slices: list[dict], centre_label: str = ""):
        self.delete("all")
        if not slices:
            return
        total = sum(s["value"] for s in slices) or 1
        s = self._size
        margin = 8
        x0, y0, x1, y1 = margin, margin, s - margin, s - margin
        start = 90  # 12 o'clock
        for sl in slices:
            extent = -(sl["value"] / total) * 360
            self.create_arc(x0, y0, x1, y1, start=start, extent=extent,
                            style="arc", outline=sl["color"], width=14)
            start += extent
        # inner hole — already an arc with width so transparent middle is natural
        self.create_text(s/2, s/2 - 6, text=centre_label, fill=TEXT,
                         font=("Segoe UI Semibold", 16))
        self.create_text(s/2, s/2 + 12, text="API VALUE", fill=MUTED_2,
                         font=("Segoe UI", 7))


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------

class Dashboard(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("AI Usage")
        self.geometry("1080x760")
        self.minsize(900, 660)
        self.configure(fg_color=BG_BASE)

        # Try to load icon
        try:
            self.iconbitmap(default=str(ICON_ICO))
            self._icon_photo = tk.PhotoImage(file=str(ICON_PNG))
            self.iconphoto(True, self._icon_photo)
        except Exception:
            pass

        # --- Header ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(22, 14))
        left = ctk.CTkFrame(header, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(left, text="AI USAGE", font=("Segoe UI Semibold", 18),
                     text_color=TEXT).pack(anchor="w")
        self._gen = ctk.CTkLabel(left, text="—", font=("Segoe UI", 11),
                                 text_color=MUTED)
        self._gen.pack(anchor="w", pady=(2, 0))

        refresh_btn = ctk.CTkButton(header, text="↻ Refresh", width=120, height=32,
                                    corner_radius=999, command=self._user_refresh,
                                    fg_color="#262630", hover_color="#34343f",
                                    text_color=TEXT)
        refresh_btn.pack(side="right")

        # --- Scrollable body ---
        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        body._parent_canvas.bind(
            "<Configure>",
            lambda e: body._parent_canvas.itemconfigure(body._create_window_id, width=e.width),
            add="+",
        )

        # Gauges
        gauges = ctk.CTkFrame(body, fg_color="transparent")
        gauges.pack(fill="x", padx=8, pady=(4, 14))
        for i in range(2):
            gauges.grid_columnconfigure(i, weight=1, uniform="g")
        self._g_c5h = Gauge(gauges, "Claude · 5h", ACCENT_CLAUDE); self._g_c5h.grid(row=0, column=0, padx=4, pady=(0, 8), sticky="nsew")
        self._g_c7d = Gauge(gauges, "Claude · 7d", ACCENT_CLAUDE); self._g_c7d.grid(row=0, column=1, padx=4, pady=(0, 8), sticky="nsew")
        self._g_x5h = Gauge(gauges, "Codex · 5h",  ACCENT_CODEX);  self._g_x5h.grid(row=1, column=0, padx=4, sticky="nsew")
        self._g_x7d = Gauge(gauges, "Codex · 7d",  ACCENT_CODEX);  self._g_x7d.grid(row=1, column=1, padx=4, sticky="nsew")

        # Snapshot row
        snap = ctk.CTkFrame(body, fg_color="transparent")
        snap.pack(fill="x", padx=8, pady=(0, 14))
        for i in range(3):
            snap.grid_columnconfigure(i, weight=1, uniform="s")
        self._tok    = StatCard(snap, "Claude tokens today"); self._tok.grid(row=0, column=0, padx=4, pady=(0, 8), sticky="nsew")
        self._cost   = StatCard(snap, "Claude API value today"); self._cost.grid(row=0, column=1, padx=4, pady=(0, 8), sticky="nsew")
        self._codex_tok = StatCard(snap, "Codex tokens today"); self._codex_tok.grid(row=0, column=2, padx=4, pady=(0, 8), sticky="nsew")
        self._codex_cost = StatCard(snap, "Codex API value today"); self._codex_cost.grid(row=1, column=0, padx=4, sticky="nsew")
        self._cache  = StatCard(snap, "Cache hit ratio"); self._cache.grid(row=1, column=1, padx=4, sticky="nsew")
        self._burn   = StatCard(snap, "Burn rate (last 60 min)"); self._burn.grid(row=1, column=2, padx=4, sticky="nsew")

        # Model donut + Activity heatmap
        row3 = ctk.CTkFrame(body, fg_color="transparent")
        row3.pack(fill="x", padx=8, pady=(0, 14))
        row3.grid_columnconfigure(0, weight=1, uniform="r3")
        row3.grid_columnconfigure(1, weight=2, uniform="r3")
        model_card = ctk.CTkFrame(row3, fg_color=BG_PANEL, corner_radius=14)
        model_card.grid(row=0, column=0, padx=(0,6), sticky="nsew")
        ctk.CTkLabel(model_card, text="Claude model value (all-time)", font=("Segoe UI Semibold", 12),
                     text_color=TEXT).pack(anchor="w", padx=14, pady=(12, 6))
        mc_body = ctk.CTkFrame(model_card, fg_color="transparent")
        mc_body.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self._donut = ModelDonut(mc_body, size=140); self._donut.pack(side="left")
        self._legend = ctk.CTkFrame(mc_body, fg_color="transparent")
        self._legend.pack(side="left", fill="both", expand=True, padx=(12, 0))

        heat_card = ctk.CTkFrame(row3, fg_color=BG_PANEL, corner_radius=14)
        heat_card.grid(row=0, column=1, padx=(6,0), sticky="nsew")
        ctk.CTkLabel(heat_card, text="Activity heatmap · last 7 days × 24 hours",
                     font=("Segoe UI Semibold", 12), text_color=TEXT).pack(anchor="w", padx=14, pady=(12, 0))
        self._heat = Heatmap(heat_card)
        self._heat.pack(fill="both", expand=True, padx=14, pady=(4, 14))

        self._codex_models = BarList(body, "Codex models · by API value", ACCENT_CODEX)
        self._codex_models.pack(fill="x", padx=8, pady=(0, 14))

        # Projects + Tools
        row4 = ctk.CTkFrame(body, fg_color="transparent")
        row4.pack(fill="x", padx=8, pady=(0, 14))
        row4.grid_columnconfigure(0, weight=1, uniform="r4")
        row4.grid_columnconfigure(1, weight=1, uniform="r4")
        self._projects = BarList(row4, "Top projects · by tokens", ACCENT_CLAUDE)
        self._projects.grid(row=0, column=0, padx=(0,6), sticky="nsew")
        self._tools = BarList(row4, "Top tools used · by call count", "#6e8be0")
        self._tools.grid(row=0, column=1, padx=(6,0), sticky="nsew")

        # Daily table
        table_card = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=14)
        table_card.pack(fill="x", padx=8, pady=(0, 14))
        ctk.CTkLabel(table_card, text="Last 7 days · Claude", font=("Segoe UI Semibold", 12),
                     text_color=TEXT).pack(anchor="w", padx=14, pady=(12, 6))
        self._table = ctk.CTkFrame(table_card, fg_color="transparent")
        self._table.pack(fill="x", padx=14, pady=(0, 14))

        codex_table_card = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=14)
        codex_table_card.pack(fill="x", padx=8, pady=(0, 14))
        ctk.CTkLabel(codex_table_card, text="Last 7 days · Codex", font=("Segoe UI Semibold", 12),
                     text_color=TEXT).pack(anchor="w", padx=14, pady=(12, 6))
        self._codex_table = ctk.CTkFrame(codex_table_card, fg_color="transparent")
        self._codex_table.pack(fill="x", padx=14, pady=(0, 14))

        self.after(100, self._refresh)
        self.after(REFRESH_SECONDS * 1000, self._tick)

    def _user_refresh(self):
        self._gen.configure(text="refreshing...")
        threading.Thread(target=self._refresh, daemon=True).start()

    def _tick(self):
        threading.Thread(target=self._refresh, daemon=True).start()
        self.after(REFRESH_SECONDS * 1000, self._tick)

    def _refresh(self):
        data = query()
        # Bounce back to Tk thread
        self.after(0, lambda: self._render(data))

    def _render(self, d: dict):
        if not d or d.get("_error"):
            self._gen.configure(text=f"error: {d.get('_error', 'no data')}")
            return
        self._gen.configure(text=d.get("generated_at", ""))

        plan  = (d.get("windows") or {}).get("claude_plan") or {}
        codex = d.get("codex", {}) or {}
        rl    = codex.get("rate_limits") or {}

        def util(key):
            v = plan.get(key)
            if v is None: return None
            try: return float(v) * 100
            except (TypeError, ValueError): return None

        self._g_c5h.set_value(util("5h-utilization"), plan.get("5h-reset"))
        self._g_c7d.set_value(util("7d-utilization"), plan.get("7d-reset"))
        self._g_x5h.set_value(float((rl.get("primary")   or {}).get("used_percent", 0) or 0),
                              (rl.get("primary")   or {}).get("resets_at"))
        self._g_x7d.set_value(float((rl.get("secondary") or {}).get("used_percent", 0) or 0),
                              (rl.get("secondary") or {}).get("resets_at"))

        c = d.get("claude", {}) or {}
        today = c.get("today", {}) or {}
        totals = c.get("totals", {}) or {}

        self._tok.set(fmt_num(today.get("tokens", 0)),
                      f"{int(today.get('messages', 0) or 0):,} messages")
        self._cost.set(fmt_money(today.get("cost", 0)),
                       f"7d: {fmt_money(c.get('week_cost', 0))}")
        codex_today = codex.get("today", {}) or {}
        self._codex_tok.set(fmt_num(codex_today.get("tokens", 0)),
                            f"{int(codex_today.get('sessions', 0) or 0):,} sessions")
        pricing_model = (codex.get("totals", {}) or {}).get("pricing_model")
        pricing_detail = f"7d: {fmt_money(codex.get('week_cost', 0))}"
        if pricing_model:
            pricing_detail = f"{pricing_detail} · {pricing_model}"
        self._codex_cost.set(fmt_money(codex_today.get("cost", 0)), pricing_detail)

        total_input = (totals.get("input", 0) + totals.get("cache_w", 0) + totals.get("cache_r", 0))
        ratio = (totals.get("cache_r", 0) / total_input * 100) if total_input else 0
        savings = totals.get("cache_r", 0) * 0.0000135
        self._cache.set(f"{ratio:.0f}%", f"≈ {fmt_money(savings)} saved via cache reads")

        burn = c.get("burn", {}) or {}
        self._burn.set(f"{fmt_num(burn.get('tokens_last_60min', 0))} tk/h",
                       f"{fmt_money(burn.get('cost_last_60min', 0))} in the last hour")

        # Model donut
        by_model = c.get("by_model", {}) or {}
        order = [k for k in ("opus", "sonnet", "haiku") if k in by_model]
        slices, legend_rows = [], []
        colors = {"opus": ACCENT_CLAUDE, "sonnet": ACCENT_PURPLE, "haiku": ACCENT_BLUE}
        total_cost = 0
        for k in order:
            m = by_model[k]
            tok = (m.get("input",0)+m.get("output",0)+m.get("cache_w",0)+m.get("cache_r",0))
            cost = m.get("cost", 0)
            total_cost += cost
            slices.append({"value": tok, "color": colors[k], "name": k, "cost": cost, "tok": tok})
            legend_rows.append((k, colors[k], tok, cost))
        self._donut.render(slices, fmt_money(total_cost))

        for child in self._legend.winfo_children():
            child.destroy()
        for name, col, tok, cost in legend_rows:
            row = ctk.CTkFrame(self._legend, fg_color="transparent")
            row.pack(fill="x", pady=3)
            swatch = ctk.CTkFrame(row, fg_color=col, width=10, height=10, corner_radius=3)
            swatch.pack(side="left")
            ctk.CTkLabel(row, text=" " + name.capitalize(), font=("Segoe UI", 11),
                         text_color=TEXT, anchor="w").pack(side="left", padx=(6, 0))
            ctk.CTkLabel(row, text=f"{fmt_num(tok)} · {fmt_money(cost)}",
                         font=("Segoe UI", 10), text_color=MUTED, anchor="e").pack(side="right")

        codex_models = []
        for name, m in (codex.get("by_model", {}) or {}).items():
            cost = float(m.get("cost", 0) or 0)
            tokens = float(m.get("tokens", 0) or 0)
            codex_models.append({
                "name": f"{name} · {fmt_num(tokens)} tokens",
                "weight": cost or tokens,
                "label": fmt_money(cost),
            })
        codex_models.sort(key=lambda r: r["weight"], reverse=True)
        self._codex_models.set_rows(codex_models)

        # Heatmap (deferred until canvas has a size)
        self.update_idletasks()
        self._heat.render(c.get("heatmap", []))

        # Projects
        self._projects.set_rows([
            {"name": p["name"], "weight": p["tokens"], "label": fmt_num(p["tokens"])}
            for p in (c.get("projects", []) or [])
        ])

        # Tools
        self._tools.set_rows([
            {"name": t["name"], "weight": t["count"], "label": f"{t['count']:,}"}
            for t in (c.get("tools", []) or [])
        ])

        # Daily table
        for child in self._table.winfo_children():
            child.destroy()
        cols = ("Date", "Tokens", "Input", "Output", "Cache R", "Cost", "Msgs")
        widths = (110, 90, 80, 80, 110, 80, 70)
        header = ctk.CTkFrame(self._table, fg_color="transparent")
        header.pack(fill="x")
        for i, name in enumerate(cols):
            anchor = "w" if i == 0 else "e"
            ctk.CTkLabel(header, text=name.upper(), font=("Segoe UI", 9),
                         text_color=MUTED_2, anchor=anchor, width=widths[i]).pack(side="left", padx=2)
        today_label = d.get("today_label")
        for r in (c.get("daily", []) or []):
            row = ctk.CTkFrame(self._table, fg_color="transparent")
            row.pack(fill="x", pady=1)
            is_today = r.get("date") == today_label
            vals = (
                r.get("date", ""),
                fmt_num(r.get("tokens", 0)),
                fmt_num(r.get("input", 0)),
                fmt_num(r.get("output", 0)),
                fmt_num(r.get("cache_r", 0)),
                fmt_money(r.get("cost", 0)),
                f"{r.get('messages', 0):,}",
            )
            for i, v in enumerate(vals):
                anchor = "w" if i == 0 else "e"
                color = ACCENT_CLAUDE if is_today and i == 0 else (TEXT if i == 0 else MUTED)
                ctk.CTkLabel(row, text=v, font=("Segoe UI", 11),
                             text_color=color, anchor=anchor, width=widths[i]).pack(side="left", padx=2)

        for child in self._codex_table.winfo_children():
            child.destroy()
        codex_cols = ("Date", "Tokens", "Input", "Output", "Cached", "Reasoning", "Cost", "Sessions")
        codex_widths = (110, 90, 90, 80, 100, 100, 80, 80)
        codex_header = ctk.CTkFrame(self._codex_table, fg_color="transparent")
        codex_header.pack(fill="x")
        for i, name in enumerate(codex_cols):
            anchor = "w" if i == 0 else "e"
            ctk.CTkLabel(codex_header, text=name.upper(), font=("Segoe UI", 9),
                         text_color=MUTED_2, anchor=anchor, width=codex_widths[i]).pack(side="left", padx=2)
        for r in (codex.get("daily", []) or []):
            row = ctk.CTkFrame(self._codex_table, fg_color="transparent")
            row.pack(fill="x", pady=1)
            is_today = r.get("date") == today_label
            vals = (
                r.get("date", ""),
                fmt_num(r.get("tokens", 0)),
                fmt_num(r.get("input", 0)),
                fmt_num(r.get("output", 0)),
                fmt_num(r.get("cached", 0)),
                fmt_num(r.get("reasoning", 0)),
                fmt_money(r.get("cost", 0)),
                f"{r.get('sessions', 0):,}",
            )
            for i, v in enumerate(vals):
                anchor = "w" if i == 0 else "e"
                color = ACCENT_CODEX if is_today and i == 0 else (TEXT if i == 0 else MUTED)
                ctk.CTkLabel(row, text=v, font=("Segoe UI", 11),
                             text_color=color, anchor=anchor, width=codex_widths[i]).pack(side="left", padx=2)


def main():
    set_windows_app_id()
    app = Dashboard()
    app.mainloop()


if __name__ == "__main__":
    main()
