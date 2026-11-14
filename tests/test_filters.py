import base64
import json
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import recovery


class ConversationFilterTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.addCleanup(lambda: [
            self.root.after_cancel(job) for job in self.root.tk.call("after", "info")
        ])
        self.ui = app.Application(self.root, Path(r"C:\Synthetic\UserData"))
        owner = "copilotcli:/abcdef12-abcd-4abc-8abc-abcdefabcdef"
        encoded = base64.urlsafe_b64encode(owner.encode()).decode().rstrip("=")
        chats = {name: f"ahp-chat://{name}/{encoded}" for name in (
            "default", "fork", "sibling", "child", "grandchild", "unknown-origin", "orphan-child",
        )}
        chat_rows = [
            (owner, chats["default"], None, None, None, owner, '{"kind":"user"}'),
            (owner, chats["unknown-origin"], None, None, None, chats["unknown-origin"], None),
        ]
        for name, parent, kind in (
            ("fork", "default", "fork"), ("sibling", "default", "tool"),
            ("child", "fork", "tool"), ("grandchild", "child", "fork"),
            ("orphan-child", "unknown-origin", "tool"),
        ):
            origin = {"kind": kind, "chat": chats[parent]}
            if kind == "fork":
                origin["turnId"] = "anchor"
            chat_rows.append((
                owner, chats[name], chats[parent], owner,
                "anchor" if kind == "fork" else None, chats[name], json.dumps(origin),
            ))
        associations = recovery.catalog_associations([(owner, 0, "copilotcli")], chat_rows)
        self.rows = {}
        for index, name in enumerate(chats):
            identity = recovery.catalog_storage_key(owner if name == "default" else chats[name])
            self.rows[name] = self.row(identity, index, **associations[identity.casefold()])
        self.rows["unknown"] = self.row("unknown", 20, title="fork sibling default")
        self.rows["other-root"] = self.row(
            "other-root", 21, kind="Session", parent_id=None, parent="None (session)",
        )
        self.ui.items = dict(self.rows)
        self.ui.apply_filters()

    @staticmethod
    def row(identity, created, **changes):
        item = {
            "id": identity, "database": str(Path(r"C:\Synthetic") / identity / "session.db"),
            "title": identity, "project": "Project", "folders": [r"C:\SameFolder"],
            "status": "Read-only", "evidence": "Synthetic", "edit_count": 0,
            "can_prepare": False, "identity_warning": False, "modified": created,
            "created": created, "catalog_available": True, "inspection_complete": True,
            "kind": "Unknown", "parent": "Unavailable",
        }
        item.update(changes)
        return item

    def select(self, key):
        self.ui.tree.selection_set(key)
        self.ui.selected()

    def visible(self):
        return set(self.ui.tree.get_children())

    def test_type_filter_composes_with_text_project_and_sort(self):
        for kind in app.CHAT_TYPES:
            with self.subTest(kind=kind):
                self.ui.type_filter.set(kind)
                self.ui.apply_filters()
                self.assertEqual(self.visible(), {
                    key for key, row in self.rows.items() if row["kind"] == kind
                })
        self.ui.type_filter.set("Fork")
        self.ui.search_text.set(self.rows["fork"]["id"])
        self.assertEqual(self.visible(), {"fork"})
        self.ui.project_filter.set("Not this project")
        self.ui.apply_filters()
        self.assertFalse(self.visible())
        self.ui.project_filter.set(self.ui.project_label(self.rows["fork"]))
        self.ui.search_text.set("")
        self.ui.sort_order.set(app.SORT_CREATED_OLD)
        self.ui.sort_changed()
        self.assertEqual(self.ui.tree.get_children(), ("fork", "grandchild"))
        self.assertEqual(self.rows["unknown"]["kind"], "Unknown")
        self.assertFalse(any(row["can_prepare"] for row in self.ui.items.values()))

    def test_verified_family_is_immediate_not_entire_tree(self):
        self.select("fork")
        self.ui.view_family()
        self.assertEqual(self.visible(), {"default", "fork", "sibling", "child"})
        self.assertEqual(self.ui.tree.selection(), ("fork",))
        anchor = self.ui.family_anchor
        self.select("sibling")
        self.assertEqual(self.ui.family_anchor, anchor)
        self.ui.type_filter.set("Fork")
        self.ui.apply_filters()
        self.assertEqual(self.visible(), {"fork"})
        self.ui.clear_family()
        self.assertIsNone(self.ui.family_anchor)
        self.assertEqual(self.ui.type_filter.get(), "Fork")
        self.assertEqual(self.visible(), {"fork", "grandchild"})
        self.assertEqual(str(self.ui.clear_family_button["state"]), "disabled")

    def test_root_and_unknown_parent_do_not_group_unrelated_roots(self):
        self.select("default")
        self.ui.view_family()
        self.assertEqual(self.visible(), {"default", "fork", "sibling"})
        self.ui.clear_family()
        self.select("unknown-origin")
        self.ui.view_family()
        self.assertEqual(self.visible(), {"unknown-origin", "orphan-child"})
        self.assertIn("no siblings inferred", self.ui.family_note.get())
        self.ui.clear_family()
        self.select("unknown")
        self.assertEqual(str(self.ui.family_button["state"]), "disabled")
        self.ui.view_family()
        self.assertIsNone(self.ui.family_anchor)

    def test_missing_parent_keeps_verified_siblings_and_stream_adds_exact_parent(self):
        del self.ui.items["default"]
        self.ui.apply_filters()
        self.select("fork")
        self.ui.view_family()
        self.assertEqual(self.visible(), {"fork", "sibling", "child"})
        self.assertIn("not in verified scan results", self.ui.family_note.get())
        token = self.ui.selection_generation
        self.ui.scanning = True
        self.ui.read_messages.put(("rows", self.ui.scan_generation, ([self.rows["default"]], 8, 8)))
        self.ui.poll()
        self.assertEqual(len(self.visible()), 4)
        self.assertIn(self.rows["default"]["database"], self.visible())
        self.assertEqual(self.ui.tree.selection(), ("fork",))
        self.assertEqual(self.ui.selection_generation, token)
        self.assertNotIn("not in verified scan results", self.ui.family_note.get())
        self.ui.cancel_scan()
        self.ui.read_messages.put(("done", self.ui.scan_generation, True))
        self.ui.poll()
        self.assertTrue(self.ui.scan_cancel.is_set())
        self.assertEqual(len(self.visible()), 4)
        self.assertIsNotNone(self.ui.family_anchor)
        self.assertIn("Scan cancelled", self.ui.status.get())

    def test_unknown_lookalikes_and_duplicate_ids_cannot_supply_family(self):
        self.ui.items["unknown"]["parent_id"] = self.rows["fork"]["parent_id"]
        self.ui.items["case-variant"] = self.row(
            self.rows["fork"]["id"].upper(), 30, kind="Fork",
            parent_id=self.rows["fork"]["parent_id"].upper(),
        )
        self.ui.apply_filters()
        self.select("fork")
        self.ui.view_family()
        self.assertEqual(self.visible(), {"default", "fork", "sibling", "child"})
        self.ui.items["duplicate"] = dict(self.rows["fork"])
        self.ui.refresh_stream()
        self.assertFalse(self.visible())
        self.assertIn("evidence changed", self.ui.family_note.get())
        self.ui.clear_family()
        self.select("fork")
        self.assertEqual(str(self.ui.family_button["state"]), "disabled")

    def test_filter_changes_reject_late_selection_results_and_clear_incident(self):
        self.select("fork")
        token = self.ui.selection_generation
        self.ui.incident = Path("synthetic-incident.json")
        self.ui.acknowledged.set(True)
        self.ui.view_family()
        self.assertIsNone(self.ui.incident)
        self.assertFalse(self.ui.acknowledged.get())
        self.assertGreater(self.ui.selection_generation, token)
        token = self.ui.selection_generation
        self.ui.type_filter.set("Session")
        self.ui.apply_filters()
        self.ui.read_messages.put(("inspect", self.ui.scan_generation, (
            token, "fork", dict(self.rows["fork"], title="STALE INSPECTION"),
        )))
        self.ui.read_messages.put(("preview", self.ui.scan_generation, (token, "fork", "STALE PREVIEW")))
        self.ui.poll()
        self.assertFalse(self.ui.tree.selection())
        self.assertNotIn("STALE", self.ui.details.get("1.0", "end"))
        self.assertNotEqual(self.ui.items["fork"]["title"], "STALE INSPECTION")

    def test_inspection_keeps_catalog_evidence_and_preview_binding(self):
        self.select("fork")
        self.ui.view_family()
        token = self.ui.selection_generation
        inspected = dict(
            self.rows["fork"], kind="Unknown", parent="Unavailable", parent_id=None,
            owner_uri=None, chat_uri=None, storage_resource=None, catalog_available=False,
        )
        self.ui.read_messages.put(("inspect", self.ui.scan_generation, (token, "fork", inspected)))
        self.ui.poll()
        for field in ("kind", "parent", "parent_id", "owner_uri", "chat_uri", "storage_resource"):
            self.assertEqual(self.ui.items["fork"][field], self.rows["fork"][field])
        self.assertEqual(self.visible(), {"default", "fork", "sibling", "child"})
        self.assertTrue(self.ui.items["fork"]["catalog_available"])

    def test_family_filter_cancels_pending_render_batches(self):
        for index in range(250):
            self.ui.items[f"extra-{index}"] = self.row(f"extra-{index}", -index - 50)
        self.ui.apply_filters()
        self.select("fork")
        self.ui.view_family()
        self.root.update()
        self.assertEqual(self.visible(), {"default", "fork", "sibling", "child"})
        self.ui.search_text.set("nothing matches")
        self.root.update()
        self.assertFalse(self.visible())
        self.assertFalse(self.ui.tree.selection())
        self.assertEqual(str(self.ui.prepare_button["state"]), "disabled")

    def test_reset_discards_old_streams_and_new_scan_clears_family_only(self):
        self.select("fork")
        self.ui.view_family()
        old = self.ui.scan_generation
        self.ui.type_filter.set("Fork")
        self.ui.user_data.set(r"C:\Synthetic\OtherUserData")
        self.ui.read_messages.put(("rows", old, ([self.rows["fork"]], 1, 1)))
        self.ui.poll()
        self.assertFalse(self.ui.items)
        self.assertIsNone(self.ui.family_anchor)
        self.assertEqual(self.ui.type_filter.get(), app.ALL_CHAT_TYPES)
        self.assertEqual(self.ui.family_note.get(), "No family filter.")
        self.ui.items = dict(self.rows)
        self.ui.apply_filters()
        self.select("fork")
        self.ui.view_family()
        self.ui.type_filter.set("Fork")
        self.ui.search_text.set("fork")
        with patch.object(app.threading, "Thread") as thread:
            self.ui.scan()
        thread.return_value.start.assert_called_once()
        self.assertIsNone(self.ui.family_anchor)
        self.assertEqual(self.ui.type_filter.get(), "Fork")
        self.assertEqual(self.ui.search_text.get(), "fork")
        self.assertFalse(self.ui.tree.selection())
        self.assertEqual(str(self.ui.family_button["state"]), "disabled")

    def test_busy_operations_disable_family_controls(self):
        self.select("fork")
        self.ui.view_family()
        anchor = self.ui.family_anchor
        self.ui.busy = True
        self.ui.update_buttons()
        for widget in (self.ui.type_box, self.ui.family_button, self.ui.clear_family_button):
            self.assertEqual(str(widget["state"]), "disabled")
        self.ui.clear_family()
        self.assertEqual(self.ui.family_anchor, anchor)


if __name__ == "__main__":
    unittest.main()
