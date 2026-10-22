import importlib
import json
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# The Windows tray backend is replaced at import time; no GUI or registry is opened.
with patch.dict(sys.modules, {
    "winreg": types.ModuleType("winreg"),
    "pystray": MagicMock(),
    "PIL": MagicMock(),
}):
    tray = importlib.import_module("tray")


class StatsTests(unittest.TestCase):
    def test_missing_codex_percentage_stays_unknown(self):
        data = {"codex": {"rate_limits": {"primary": {"resets_at": 123}}}}
        with patch.object(tray, "find_python", return_value=Path("fixture-python")), patch.object(
            tray.subprocess, "check_output", side_effect=[json.dumps(data), "{}"]
        ):
            result = tray.Stats().refresh()
        self.assertIsNone(result["codex_5h"]["left"])

    def test_non_object_collector_output_preserves_last_snapshot(self):
        stats = tray.Stats()
        stats.snapshot = {"codex_5h": {"left": 70}}
        with patch.object(tray, "find_python", return_value=Path("fixture-python")), patch.object(
            tray.subprocess, "check_output", side_effect=["[]", "{}"]
        ):
            result = stats.refresh()
        self.assertEqual(result["codex_5h"]["left"], 70)
        self.assertIn("error", result)

    def test_missing_interpreter_is_a_recoverable_refresh_error(self):
        stats = tray.Stats()
        stats.snapshot = {"codex_5h": {"left": 70}}
        with patch.object(tray, "find_python", side_effect=FileNotFoundError("fixture-python")):
            result = stats.refresh()
        self.assertEqual(result["codex_5h"]["left"], 70)
        self.assertIn("error", result)


class TrayLifecycleTests(unittest.TestCase):
    def test_quit_from_any_icon_exits_polling_and_stops_all_icons(self):
        icons = []
        waited = []

        def menu_item(text, callback, **kwargs):
            return types.SimpleNamespace(text=text, callback=callback)

        def icon_factory(**kwargs):
            icon = MagicMock(**kwargs)
            icon.menu = kwargs["menu"]
            icons.append(icon)
            return icon

        class PollEvent:
            def __init__(self):
                self.event = threading.Event()

            def set(self):
                self.event.set()

            def clear(self):
                self.event.clear()

            def is_set(self):
                return self.event.is_set()

            def wait(self, timeout=None):
                if timeout == 0.5:
                    return self.event.is_set()
                waited.append(timeout)
                if len(waited) > 1:
                    # Bound the original broken loop so the regression cannot hang.
                    raise KeyboardInterrupt
                quit_item = next(item for item in icons[2].menu if item.text == "Quit")
                quit_item.callback(icons[2], quit_item)
                return self.event.is_set()

        real_event = threading.Event
        # PollEvent's backing event must not recursively use the patched factory.
        def event_factory():
            result = PollEvent.__new__(PollEvent)
            result.event = real_event()
            return result

        stats = MagicMock()
        stats.get.return_value = {}
        with patch.object(tray, "Stats", return_value=stats), patch.object(
            tray.pystray, "MenuItem", side_effect=menu_item
        ), patch.object(tray.pystray, "Menu", side_effect=lambda *items: items), patch.object(
            tray.pystray, "Icon", side_effect=icon_factory
        ), patch.object(tray, "render_icon", return_value=object()), patch.object(
            tray.threading, "Event", side_effect=event_factory
        ), patch.object(tray.threading, "Thread"), patch.object(tray.time, "sleep"):
            self.assertEqual(tray.main_loop(), 0)
        self.assertEqual(len(icons), 4)
        self.assertEqual(stats.refresh.call_count, 1, "Quit must not trigger another poll")
        self.assertEqual(len(waited), 1, "Quit must terminate the polling loop")
        for icon in icons:
            self.assertTrue(icon.stop.called)


if __name__ == "__main__":
    unittest.main()
