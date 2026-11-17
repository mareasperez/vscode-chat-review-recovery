import threading
import time
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

import app
import recovery
import test_recovery
from qt_support import make_ui, process_events


class StreamingTests(unittest.TestCase):
    setUp = test_recovery.RecoveryTests.setUp
    create_database = test_recovery.RecoveryTests.create_database

    def test_lightweight_scan_never_claims_eligibility_and_strict_api_is_unchanged(self):
        with patch.object(recovery, "check_supported", side_effect=AssertionError("strict scan")):
            rows = list(recovery.stream_scan(self.user_data))
        item, completed, total = rows[0]
        self.assertEqual((completed, total), (1, 1))
        self.assertEqual(item["status"], "Not yet checked")
        self.assertFalse(item["can_prepare"])
        self.assertFalse(item["inspection_complete"])
        self.assertEqual(item["edit_count"], 5)
        self.assertTrue(recovery.scan(self.user_data)[0]["can_prepare"])

    def test_stream_yields_before_later_database_and_cancels(self):
        other = self.host.with_name("zzz-other")
        other.mkdir()
        (other / "session.db").write_bytes(self.database.read_bytes())
        cancel = threading.Event()
        original = recovery.inspect_session
        calls = []

        def inspect(*args, **kwargs):
            calls.append(args[0])
            if len(calls) > 1:
                raise AssertionError("later database read before request")
            return original(*args, **kwargs)

        with patch.object(recovery, "inspect_session", side_effect=inspect):
            stream = recovery.stream_scan(self.user_data, cancel=cancel)
            started = time.monotonic()
            item, completed, total = next(stream)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual((completed, total), (1, 2))
            cancel.set()
            self.assertEqual(list(stream), [])
        self.assertEqual(len(calls), 1)
        self.assertFalse(item["can_prepare"])

    def ui(self):
        return make_ui(self, self.user_data)

    def test_stream_preserves_exact_selection_and_rejects_stale_inspection(self):
        ui = self.ui()
        first = recovery.inspect_session(self.database, self.user_data)
        ui.items = {"selected": first}
        ui.apply_filters()
        ui.tree.selection_set("selected")
        process_events()
        token = ui.selection_generation
        second = dict(first, database="unrelated.db", id="unrelated", title="Other")
        ui.window._scan_item(second, 2, 2, ui.scan_generation)
        self.assertEqual(ui.tree.selection(), ("selected",))
        self.assertEqual(Path(ui.items["selected"]["database"]).resolve(), self.database.resolve())
        self.assertEqual(ui.selection_generation, token)
        ui.search_text.set("not present")
        ui.window._inspection_finished(token, "selected", dict(first, title="Wrong result"))
        self.assertNotEqual(ui.items["selected"]["title"], "Wrong result")
        self.assertIsNone(ui.incident)

    def test_location_change_discards_old_scan_and_allows_restart(self):
        ui = self.ui()
        item = recovery.inspect_session(self.database, self.user_data)
        old = ui.scan_generation
        ui.scanning = True
        cancellation = ui.scan_cancel
        ui.user_data.set(str(self.root))
        ui.window._scan_item(item, 1, 1, old)
        ui.window._scan_finished(old, False)
        self.assertTrue(cancellation.is_set())
        self.assertFalse(ui.items)
        self.assertFalse(ui.scanning)
        self.assertEqual(str(ui.scan_button["state"]), "disabled")

    def test_locked_database_is_visible_and_bounded(self):
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        connection.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        item, _, _ = next(recovery.stream_scan(self.user_data))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(item["status"], "Unreadable database")
        self.assertIn("locked", item["evidence"])
        self.assertFalse(item["can_prepare"])

    def test_provisional_row_cannot_prepare_and_selected_inspection_is_exact(self):
        ui = self.ui()
        provisional = next(recovery.stream_scan(self.user_data))[0]
        ui.populate([provisional])
        window = ui.window
        workers = []
        with patch.object(window.pool, "start", side_effect=lambda worker: workers.append(worker)):
            ui.tree.selection_set("0")
        self.assertEqual(str(ui.prepare_button["state"]), "disabled")
        with patch.object(recovery, "inspect_session", wraps=recovery.inspect_session) as inspect:
            inspected = workers[0].function()
        self.assertEqual(tuple(Path(value).resolve() for value in inspect.call_args.args),
                         (self.database.resolve(), self.user_data.resolve()))
        self.assertEqual(inspect.call_args.kwargs, {"strict": True})
        window._inspection_finished(window.selection_generation, "0", inspected)
        self.assertTrue(ui.items["0"]["inspection_complete"])
        self.assertEqual(str(ui.prepare_button["state"]), "normal")


if __name__ == "__main__":
    unittest.main()
