import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

import app


class QtUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)

    def tearDown(self):
        self.settings.clear()
        self.settings.sync()
        self.temporary.cleanup()

    def make_window(self):
        window = app.MainWindow(self.root, settings=self.settings, auto_host_check=False)
        def close_window():
            window.busy = False
            window.close()
        self.addCleanup(close_window)
        self.app.processEvents()
        return window

    def test_main_window_builds_modern_navigation_without_enabling_unimplemented_history(self):
        window = self.make_window()

        self.assertEqual(window.pages.count(), 3)
        self.assertTrue(window.recovery_nav.isChecked())
        self.assertFalse(window.history_nav.isEnabled())
        self.assertFalse(window.repair_button.isEnabled())
        self.assertFalse(window.host_verified)

    def test_preferences_persist_directory_only_when_remember_is_enabled(self):
        preferences = app.Preferences(self.settings)
        chosen = self.root / "Code"

        preferences.save_directory(chosen)
        self.assertEqual(preferences.initial_directory(), chosen)

        preferences.set_remember_directory(False)
        self.assertEqual(preferences.initial_directory(), app.recovery.default_user_data())

    def test_valid_directory_requires_agent_session_data_and_enables_scan(self):
        window = self.make_window()
        self.assertFalse(window.scan_button.isEnabled())

        (self.root / "agentSessionData").mkdir()
        window._sync_directory_ui(invalidate=True)

        self.assertTrue(window.scan_button.isEnabled())
        self.assertIn("Valid VS Code user-data directory", window.directory_validation.text())

    def test_closed_host_does_not_make_an_unsupported_conversation_repairable(self):
        (self.root / "agentSessionData").mkdir()
        window = self.make_window()
        database = self.root / "agentSessionData" / "blocked" / "session.db"
        item = {
            "database": str(database),
            "id": "blocked",
            "title": "Unsupported parent session",
            "kind": "Session",
            "project": "example",
            "folders": [],
            "status": "Read-only: repair safety check refused",
            "evidence": "Peer conversations are unsupported.",
            "edit_count": 107,
            "inspection_complete": True,
            "can_prepare": False,
        }

        window._scan_item(item, 1, 1, window.scan_generation)
        window.table.selectRow(0)
        window._set_host_state("closed", "VS Code Closed", "Host closure verified")
        self.app.processEvents()

        self.assertTrue(window.host_verified)
        self.assertFalse(window.prepare_button.isEnabled())
        self.assertFalse(window.repair_button.isEnabled())
        self.assertEqual(window.diagnosis_title.text(), "Needs investigation")
        self.assertIn("Peer conversations are unsupported", window.overview_text.text())

    def test_repair_requires_prepared_incident_acknowledgement_and_closed_host(self):
        (self.root / "agentSessionData").mkdir()
        window = self.make_window()
        database = self.root / "agentSessionData" / "eligible" / "session.db"
        item = {
            "database": str(database),
            "id": "eligible",
            "title": "Eligible conversation",
            "kind": "Session",
            "project": "example",
            "folders": [],
            "status": "Repair preview available",
            "evidence": "Exact repair scope verified.",
            "edit_count": 3,
            "inspection_complete": True,
            "can_prepare": True,
        }

        window._scan_item(item, 1, 1, window.scan_generation)
        window.table.selectRow(0)
        window.incident = self.root / "incident" / "incident.json"
        window.acknowledgement.setChecked(True)
        window._update_actions()
        self.assertFalse(window.repair_button.isEnabled())

        window._set_host_state("closed", "VS Code Closed", "Host closure verified")
        self.assertTrue(window.repair_button.isEnabled())

    def test_host_refusal_is_rendered_as_running_not_unknown(self):
        window = self.make_window()

        window._host_check_failed(
            "Traceback (most recent call last):\n"
            "recovery.Refused: Close all VS Code/Insiders windows normally first. "
            "Do not terminate another user's processes."
        )

        self.assertEqual(window.host_title.text(), "VS Code Running")
        self.assertIn("Close VS Code normally", window.host_detail.text())
        self.assertFalse(window.host_verified)

    def test_busy_operation_disables_filters_and_family_actions(self):
        window = self.make_window()
        window.busy = True
        window._update_actions()

        for widget in (
            window.search_input, window.project_filter, window.type_filter,
            window.sort_filter, window.family_button, window.clear_family_button,
            window.table, window.host_refresh,
        ):
            self.assertFalse(widget.isEnabled())

    def test_filtering_out_selection_invalidates_stale_callbacks(self):
        window = self.make_window()
        item = {
            "database": str(self.root / "session.db"), "id": "session",
            "title": "Session", "kind": "Session", "project": "Project",
            "folders": [], "status": "Read-only", "evidence": "Synthetic",
            "edit_count": 0, "can_prepare": False,
            "inspection_complete": True, "catalog_available": True,
        }
        window._scan_item(item, 1, 1, window.scan_generation)
        window.table.selectRow(0)
        self.app.processEvents()
        old_generation = window.selection_generation

        window.search_input.setText("no match")
        self.app.processEvents()

        self.assertIsNone(window.current_key())
        self.assertGreater(window.selection_generation, old_generation)
        window._history_finished(old_generation, str(self.root / "session.db"), "STALE")
        self.assertNotIn("STALE", window.history_text.toPlainText())

    def test_scan_snapshot_round_trip_revokes_cached_repair_eligibility(self):
        (self.root / "agentSessionData").mkdir()
        window = self.make_window()
        item = {
            "database": str(self.root / "agentSessionData" / "cached" / "session.db"),
            "id": "cached",
            "title": "Cached conversation",
            "kind": "Fork",
            "project": "Cached project",
            "folders": [],
            "status": "Repair preview available",
            "evidence": "Synthetic eligible inspection",
            "edit_count": 3,
            "inspection_complete": True,
            "can_prepare": True,
        }
        window._scan_item(item, 1, 1, window.scan_generation)
        window.last_scan = datetime(2026, 11, 17, 14, 55)

        self.assertTrue(window._persist_scan_cache())

        restored = app.MainWindow(self.root, settings=self.settings, auto_host_check=False)
        self.addCleanup(restored.close)
        self.app.processEvents()

        self.assertEqual(len(restored.items), 1)
        restored_item = next(iter(restored.items.values()))
        self.assertEqual(restored_item["title"], "Cached conversation")
        self.assertEqual(restored_item["status"], "Not yet checked")
        self.assertFalse(restored_item["inspection_complete"])
        self.assertFalse(restored_item["can_prepare"])
        self.assertIn("re-run exact safety inspection", restored_item["evidence"])
        self.assertIn("Last scan", restored.settings_last_scan.text())

    def test_close_or_cancel_persists_partial_scan_records(self):
        (self.root / "agentSessionData").mkdir()
        window = self.make_window()
        item = {
            "database": str(self.root / "agentSessionData" / "partial" / "session.db"),
            "id": "partial",
            "title": "Partial conversation",
            "kind": "Session",
            "project": "Partial project",
            "folders": [],
            "status": "Not yet checked",
            "evidence": "",
            "edit_count": 2,
            "inspection_complete": False,
            "can_prepare": False,
        }
        window._scan_item(item, 1, 2, window.scan_generation)
        window.close()
        self.app.processEvents()

        cache_path = window._scan_cache_path()
        self.assertTrue(cache_path.is_file())



if __name__ == "__main__":
    unittest.main()
