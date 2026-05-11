# Twitter/X Launch Draft

Built a tiny Windows tray monitor for Claude Code + Codex usage.

Four tray icons stay visible all day:
- Claude 5h / weekly
- Codex 5h / weekly

Each one shows percent left. Click any icon to open a compact stats dashboard with today, recent activity, model split, projects, tools, and Codex rate-limit windows.

Install:

```powershell
git clone https://github.com/LLRHook/usage-widget.git
cd usage-widget
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe .\tray.py --install-startup
```

GitHub:
https://github.com/LLRHook/usage-widget

Uninstall:

```powershell
.\.venv\Scripts\python.exe .\tray.py --uninstall-startup
```

Suggested media:
- `docs/media/tray-icons.png`: close-up of the four tray indicators.
- `docs/media/dashboard.png`: dashboard after clicking a tray icon.
- `docs/media/tray-demo.gif`: short clip showing tray icon click -> stats window.
