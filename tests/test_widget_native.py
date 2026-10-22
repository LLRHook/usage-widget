import importlib
import queue
import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch


# Import the dashboard controller without creating a Tk window or loading a theme.
ctk = types.ModuleType("customtkinter")
ctk.CTk = object
ctk.CTkFrame = object
ctk.set_appearance_mode = lambda mode: None
ctk.set_default_color_theme = lambda theme: None
with patch.dict(sys.modules, {"customtkinter": ctk}):
    native = importlib.import_module("widget_native")


class DashboardRefreshTests(unittest.TestCase):
    def controller(self):
        dashboard = native.Dashboard.__new__(native.Dashboard)
        dashboard._closed = False
        dashboard._refreshing = False
        dashboard._refresh_results = queue.SimpleQueue()
        dashboard._gen = MagicMock()
        dashboard.after = MagicMock()
        dashboard.after_cancel = MagicMock()
        dashboard.destroy = MagicMock()
        dashboard._render = MagicMock()
        return dashboard

    def test_refresh_runs_off_tk_thread_and_coalesces_requests(self):
        dashboard = self.controller()
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        caller = threading.get_ident()
        worker_ids = []

        def query():
            worker_ids.append(threading.get_ident())
            started.set()
            release.wait(timeout=0.2)
            finished.set()
            return {"generated_at": "fixture"}

        with patch.object(native, "query", side_effect=query) as request:
            dashboard._refresh()
            self.assertTrue(started.wait(timeout=1))
            dashboard._refresh()
            release.set()
            self.assertTrue(finished.wait(timeout=1))
            self.assertEqual(request.call_count, 1)
        self.assertNotEqual(worker_ids, [caller])
        dashboard.after.assert_not_called()
        dashboard._render.assert_not_called()
        self.assertEqual(dashboard._refresh_results.get(timeout=1), {"generated_at": "fixture"})

    def test_closed_dashboard_does_not_start_a_query(self):
        dashboard = self.controller()
        dashboard._closed = True
        with patch.object(native, "query", return_value={}) as request:
            dashboard._refresh()
        request.assert_not_called()

    def test_results_are_rendered_by_the_tk_poller(self):
        dashboard = self.controller()
        dashboard._refreshing = True
        dashboard._refresh_results.put({"generated_at": "fixture"})
        dashboard._drain_refresh_results()
        dashboard._render.assert_called_once_with({"generated_at": "fixture"})
        self.assertFalse(dashboard._refreshing)
        dashboard.after.assert_called_once()

    def test_query_returns_error_when_interpreter_is_missing(self):
        with patch.object(native, "find_python", side_effect=FileNotFoundError("fixture-python")):
            self.assertIn("_error", native.query())

    def test_close_cancels_scheduled_callbacks(self):
        dashboard = self.controller()
        dashboard._initial_after = "initial"
        dashboard._tick_after = "tick"
        dashboard._drain_after = "drain"
        dashboard._close()
        self.assertTrue(dashboard._closed)
        self.assertEqual(dashboard.after_cancel.call_count, 3)
        dashboard.destroy.assert_called_once()

    def test_render_error_does_not_stop_the_result_poller(self):
        dashboard = self.controller()
        dashboard._refreshing = True
        dashboard._refresh_results.put({"generated_at": "fixture"})
        dashboard._render.side_effect = ValueError("fixture render failure")
        with self.assertRaises(ValueError):
            dashboard._drain_refresh_results()
        dashboard.after.assert_called_once()
        self.assertFalse(dashboard._refreshing)


if __name__ == "__main__":
    unittest.main()
