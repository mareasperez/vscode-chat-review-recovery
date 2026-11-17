import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from qt_support import make_ui, process_events


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.user_data = Path(self.temporary.name) / "Code"
        (self.user_data / "agentSessionData").mkdir(parents=True)
        self.ui = make_ui(self, self.user_data)
        self.window = self.ui.window

    def select(self, **changes):
        item = {
            "database": str(self.user_data / "agentSessionData" / "selected" / "session.db"),
            "id": "selected", "title": "Selected conversation", "kind": "Session",
            "project": "Project", "folders": [], "status": "Supported",
            "evidence": "", "edit_count": 1, "inspection_complete": True,
            "can_prepare": True,
        }
        item.update(changes)
        self.window._scan_item(item, 1, 1, self.window.scan_generation)
        self.window.table.selectRow(self.window.visible_keys.index(item["database"]))
        process_events()
        return item

    def test_startup_schedules_one_host_check_after_ui_is_ready(self):
        workers = []
        with patch.object(app.QThreadPool, "start", side_effect=workers.append):
            window = app.MainWindow(self.user_data, settings=self.window.preferences.settings)
        self.addCleanup(window.close)
        self.assertEqual(len(workers), 1)
        self.assertIs(workers[0].function, app.recovery.ensure_offline)
        self.assertFalse(window.host_verified)
        self.assertIn("Checking", window.host_title.text())
        self.assertFalse(window.repair_button.isEnabled())
        self.assertFalse(window.restore_button.isEnabled())
        workers[0].signals.finished.emit(None)
        process_events()
        self.assertTrue(window.host_verified)
        self.assertTrue(window.restore_button.isEnabled())

    def test_layout_has_clear_hierarchy_history_tab_and_tooltips(self):
        self.assertEqual(self.window.pages.count(), 3)
        self.assertEqual(
            [self.window.diagnosis_tabs.tabText(i) for i in range(self.window.diagnosis_tabs.count())],
            ["Overview", "Conversation history", "Technical evidence"],
        )
        self.assertIn("Select a row above", self.window.selected_meta.text())
        self.assertTrue(self.window.acknowledgement.toolTip())
        self.assertIn("read-only", self.window.scan_button.toolTip().casefold())
        self.assertEqual(self.window.table.columnCount(), 5)

    def test_recheck_immediately_revokes_offline_state_before_worker_finishes(self):
        self.select()
        self.window.incident = Path("synthetic-incident.json")
        self.window.acknowledgement.setChecked(True)
        self.window._set_host_state("closed", "VS Code Closed", "Host closure verified")
        self.assertTrue(self.window.repair_button.isEnabled())
        self.window.incident = Path("synthetic-incident.json")
        self.window.acknowledgement.setChecked(True)
        self.window._update_actions()
        self.assertTrue(self.window.repair_button.isEnabled())

        self.window._set_host_state("checking", "Checking VS Code…", "Writes are disabled during verification")
        self.assertFalse(self.window.host_verified)
        self.assertFalse(self.window.repair_button.isEnabled())
        self.assertFalse(self.window.restore_button.isEnabled())
        with patch.object(app.recovery, "operate") as operate, \
             patch.object(app.QFileDialog, "getOpenFileName") as dialog:
            self.window.repair_selected()
            self.window.restore_incident()
            operate.assert_not_called()
            dialog.assert_not_called()

        self.window._set_host_state("closed", "VS Code Closed", "Host closure verified")
        self.assertTrue(self.window.repair_button.isEnabled())

    def test_offline_unsupported_fork_keeps_exact_reason_and_preview_available(self):
        reason = "Peer conversations are unsupported."
        self.select(kind="Fork", can_prepare=False, evidence=reason)
        self.window._set_host_state("closed", "VS Code Closed", "Host closure verified")

        self.assertTrue(self.window.host_verified)
        self.assertIn("Needs investigation", self.window.diagnosis_title.text())
        self.assertIn(reason, self.window.overview_text.text())
        self.assertFalse(self.window.prepare_button.isEnabled())
        self.assertFalse(self.window.repair_button.isEnabled())
        self.assertTrue(self.window.history_button.isEnabled())
        self.assertTrue(self.window.restore_button.isEnabled())
        self.window.show_history()
        self.assertIn("no verified chat mapping", self.window.history_text.toPlainText().casefold())

    def test_no_selection_and_provisional_inspection_never_claim_eligibility(self):
        self.assertFalse(self.window.prepare_button.isEnabled())
        self.assertFalse(self.window.history_button.isEnabled())
        item = self.select(can_prepare=False, inspection_complete=False, status="Not yet checked")
        self.assertIn("Checking safety", self.window.diagnosis_title.text())
        self.assertFalse(self.window.prepare_button.isEnabled())
        item["inspection_complete"] = True
        item["can_prepare"] = True
        self.window._display_item(item)
        self.window._update_actions()
        self.assertIn("Repairable scope found", self.window.diagnosis_title.text())
        self.assertTrue(self.window.prepare_button.isEnabled())
        self.window.table.clearSelection()
        self.window.table.setCurrentCell(-1, -1)
        process_events()
        self.assertFalse(self.window.prepare_button.isEnabled())

    def test_host_refusal_and_unexpected_failure_fail_closed_without_changing_eligibility(self):
        self.select()
        eligibility = self.window.overview_text.text()
        for error, expected in (
            ("Close all VS Code/Insiders windows normally first", "VS Code Running"),
            ("Code is running", "VS Code Running"),
            ("Check unavailable", "VS Code status unknown"),
        ):
            with self.subTest(error=error):
                self.window._host_check_failed(error)
                self.assertFalse(self.window.host_verified)
                self.assertEqual(self.window.host_title.text(), expected)
                self.assertEqual(self.window.overview_text.text(), eligibility)
                self.assertFalse(self.window.restore_button.isEnabled())
                self.assertFalse(self.window.repair_button.isEnabled())
                self.assertTrue(self.window.prepare_button.isEnabled())
                self.assertTrue(self.window.scan_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
