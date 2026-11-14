import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import recovery


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.addCleanup(lambda: [
            self.root.after_cancel(job) for job in self.root.tk.call("after", "info")
        ])
        self.ui = app.Application(self.root, Path(r"C:\Synthetic\UserData"))

    def select(self, **changes):
        item = {
            "id": "synthetic", "kind": "Session", "can_prepare": True,
            "inspection_complete": True, "evidence": "", "status": "Supported",
        }
        item.update(changes)
        self.ui.items = {"selected": item}
        self.ui.tree.insert("", "end", iid="selected")
        self.ui.tree.selection_set("selected")
        self.ui.update_buttons()

    def complete_check(self, error=None):
        with patch.object(app.threading, "Thread") as worker:
            self.ui.check_mode()
        action = worker.call_args.kwargs["target"]
        with patch.object(recovery, "ensure_offline", side_effect=error):
            action()
        self.ui.poll()

    def test_startup_schedules_one_host_check_after_ui_is_ready(self):
        for job in self.root.tk.call("after", "info"):
            self.root.after_cancel(job)
        root = tk.Tk()
        root.withdraw()
        try:
            with patch.object(app.Application, "check_mode") as check:
                ui = app.Application(root)
                check.assert_not_called()
                self.assertTrue(ui.read_only)
                self.assertIn("CHECKING", ui.mode.get())
                self.assertEqual(str(ui.restore_button["state"]), "disabled")
                root.update_idletasks()
                check.assert_called_once_with()
                self.assertEqual(ui.mode_button["text"], "Recheck VS Code is closed")
        finally:
            for job in root.tk.call("after", "info"):
                root.after_cancel(job)
            root.destroy()

    def test_layout_has_clear_hierarchy_history_tab_and_tooltips(self):
        self.assertTrue(hasattr(self.ui, "source_frame"))
        self.assertTrue(hasattr(self.ui, "status_frame"))
        self.assertTrue(hasattr(self.ui, "conversation_frame"))
        self.assertTrue(hasattr(self.ui, "recovery_frame"))
        self.assertEqual(
            [self.ui.notebook.tab(tab, "text") for tab in self.ui.notebook.tabs()],
            ["Overview", "Conversation history", "Technical evidence"],
        )
        self.assertIn("Select a conversation", self.ui.selected_summary.get())
        tooltip_text = " ".join(tooltip.text for tooltip in self.ui.tooltips)
        self.assertIn("read-only", tooltip_text.casefold())
        self.assertIn("does not make unsupported chats repairable", tooltip_text)
        self.assertIn("verified local history", tooltip_text)
        self.assertIn("Inherited history remains unavailable", tooltip_text)
        self.assertEqual(
            tuple(self.ui.tree["displaycolumns"]),
            ("title", "kind", "project", "state", "edits"),
        )

    def test_recheck_immediately_revokes_offline_state_before_worker_finishes(self):
        self.select()
        self.ui.incident = Path("synthetic-incident.json")
        self.ui.acknowledged.set(True)
        self.ui.read_only = False
        self.ui.mode.set("Host: OFFLINE VERIFIED")
        self.ui.update_buttons()
        self.assertEqual(str(self.ui.repair_button["state"]), "normal")
        with patch.object(app.threading, "Thread") as worker:
            self.ui.check_mode()
            self.assertTrue(self.ui.read_only)
            self.assertIn("CHECKING", self.ui.mode.get())
            self.assertEqual(str(self.ui.repair_button["state"]), "disabled")
            self.assertEqual(str(self.ui.restore_button["state"]), "disabled")
            self.assertEqual(str(self.ui.mode_button["state"]), "disabled")
            with patch.object(recovery, "operate") as operate:
                self.ui.repair()
                self.ui.restore()
                operate.assert_not_called()
            with patch.object(recovery, "ensure_offline"):
                worker.call_args.kwargs["target"]()
            self.assertTrue(self.ui.read_only)
        self.ui.poll()
        self.assertFalse(self.ui.read_only)
        self.assertEqual(str(self.ui.repair_button["state"]), "normal")

    def test_offline_unsupported_fork_keeps_exact_reason_and_preview_available(self):
        reason = "The host conversation directory is not a session UUID."
        self.select(kind="Fork", can_prepare=False, evidence=reason)
        self.complete_check()
        self.assertFalse(self.ui.read_only)
        self.assertIn("OFFLINE VERIFIED", self.ui.mode.get())
        self.assertIn("REPAIR UNAVAILABLE", self.ui.eligibility.get())
        self.assertIn("Host closure does not make this conversation repairable", self.ui.eligibility.get())
        self.assertIn(reason, self.ui.eligibility.get())
        self.assertEqual(str(self.ui.prepare_button["state"]), "disabled")
        self.assertEqual(str(self.ui.repair_button["state"]), "disabled")
        self.assertEqual(str(self.ui.preview_button["state"]), "normal")
        self.assertEqual(str(self.ui.restore_button["state"]), "normal")
        self.ui.show("Partial local history")
        self.ui.status.set("Scan complete.")
        self.assertIn(reason, self.ui.eligibility.get())
        self.assertIn("OFFLINE VERIFIED", self.ui.mode.get())

    def test_no_selection_and_provisional_inspection_never_claim_eligibility(self):
        self.complete_check()
        self.assertIn("NONE", self.ui.eligibility.get())
        self.assertEqual(str(self.ui.prepare_button["state"]), "disabled")
        self.assertEqual(str(self.ui.preview_button["state"]), "disabled")
        self.select(can_prepare=False, inspection_complete=False)
        self.assertIn("NOT YET CHECKED", self.ui.eligibility.get())
        self.assertIn("inspection pending", self.ui.eligibility.get())
        self.assertEqual(str(self.ui.prepare_button["state"]), "disabled")
        self.assertEqual(str(self.ui.repair_button["state"]), "disabled")
        self.ui.items["selected"]["inspection_complete"] = True
        self.ui.items["selected"]["can_prepare"] = True
        self.ui.update_buttons()
        self.assertIn("ELIGIBLE FOR PREPARATION", self.ui.eligibility.get())
        self.assertEqual(str(self.ui.prepare_button["state"]), "normal")
        self.ui.tree.selection_remove("selected")
        self.ui.update_buttons()
        self.assertIn("NONE", self.ui.eligibility.get())

    def test_host_refusal_and_unexpected_failure_fail_closed_without_changing_eligibility(self):
        self.select()
        eligibility = self.ui.eligibility.get()
        for error in (recovery.Refused("Code is open"), RuntimeError("Check unavailable")):
            with self.subTest(error=error):
                self.ui.read_only = False
                self.ui.mode.set("Host: OFFLINE VERIFIED")
                self.complete_check(error)
                self.assertTrue(self.ui.read_only)
                self.assertIn("READ-ONLY", self.ui.mode.get())
                self.assertIn(str(error), self.ui.mode.get())
                self.assertEqual(self.ui.eligibility.get(), eligibility)
                self.assertEqual(str(self.ui.restore_button["state"]), "disabled")
                self.assertEqual(str(self.ui.repair_button["state"]), "disabled")
                self.assertEqual(str(self.ui.prepare_button["state"]), "normal")
                self.assertEqual(str(self.ui.scan_button["state"]), "normal")
                self.assertEqual(str(self.ui.mode_button["state"]), "normal")


if __name__ == "__main__":
    unittest.main()
