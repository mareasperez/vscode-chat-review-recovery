import json
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import tkinter as tk

import app
import recovery


@contextmanager
def writable(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.user_data = self.root / "Code"
        self.host = self.user_data / "agentSessionData" / str(uuid.uuid4())
        self.host.mkdir(parents=True)
        self.database = self.host / "session.db"
        self.file = self.root / "document.md"
        self.file.write_bytes(b"saved document\n")
        self.incidents = self.root / "incidents"
        self.create_database()

    def create_database(self, count=5, case_variants=False):
        self.database.unlink(missing_ok=True)
        with writable(self.database) as connection:
            connection.executescript("""
                PRAGMA user_version=20;
                CREATE TABLE turns(id TEXT PRIMARY KEY, message TEXT);
                CREATE TABLE assistant_response_parts(id TEXT PRIMARY KEY, content BLOB);
                CREATE TABLE tool_result_resources(id TEXT PRIMARY KEY, content BLOB);
                CREATE TABLE session_metadata(key TEXT NOT NULL, value TEXT NOT NULL, turn_id TEXT);
                CREATE UNIQUE INDEX global_metadata ON session_metadata(key) WHERE turn_id IS NULL;
                CREATE UNIQUE INDEX turn_metadata ON session_metadata(turn_id,key) WHERE turn_id IS NOT NULL;
                CREATE TABLE file_edits(
                    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                    tool_call_id TEXT NOT NULL, file_path TEXT NOT NULL,
                    edit_type TEXT NOT NULL DEFAULT 'edit', original_path TEXT,
                    before_content BLOB, after_content BLOB, added_lines INTEGER, removed_lines INTEGER,
                    PRIMARY KEY(tool_call_id,file_path)
                );
                CREATE TABLE reviewed_files(uri TEXT NOT NULL, nonce TEXT NOT NULL, PRIMARY KEY(uri,nonce));
            """)
            connection.execute("INSERT INTO turns VALUES('turn1','original message')")
            connection.execute("INSERT INTO assistant_response_parts VALUES('response',?)", (b"response bytes",))
            connection.execute("INSERT INTO tool_result_resources VALUES('tool',?)", (b"tool bytes",))
            connection.executemany("INSERT INTO session_metadata VALUES(?,?,?)", [
                (recovery.SUMMARY, '{"additions":5,"deletions":1,"files":1}', None),
                ("peerChats", "[]", None),
                ("customTitle", "Fixture conversation", None),
                ("unrelated", "retain this", None),
                (recovery.SUMMARY, '{"files":999}', "turn1"),
            ])
            for index in range(count):
                name = str(self.file)
                if case_variants and index % 2:
                    name = name[0].swapcase() + name[1:]
                connection.execute(
                    "INSERT INTO file_edits VALUES(?,?,?,'edit',NULL,?,?,1,0)",
                    ("turn1", f"tool{index}", name, b"before", b"after"),
                )

    def preview(self):
        return recovery.prepare(self.database, self.user_data, self.incidents)

    def state(self):
        with recovery.read_db(self.database) as connection:
            return recovery.protected_state(connection)

    def change(self, sql, parameters=()):
        with writable(self.database) as connection:
            connection.execute(sql, parameters)

    def operate(self, incident, restoring=False):
        with patch.object(recovery, "ensure_offline"):
            return recovery.operate(incident, restoring)

    def test_variable_counts_and_restore_preserve_new_messages_and_metadata(self):
        for count in (1, 5, 13, 19):
            with self.subTest(count=count):
                self.create_database(count)
                incident = self.preview()
                before_file = recovery.file_hash(self.file)
                original = self.state()
                result = self.operate(incident)
                self.assertEqual(result["records"], count)
                self.change("INSERT INTO turns VALUES('new','newer message')")
                protected = self.state()
                restored = self.operate(incident, True)
                self.assertEqual(restored["status"], "restored")
                with recovery.read_db(self.database) as connection:
                    self.assertEqual(connection.execute("SELECT message FROM turns WHERE id='new'").fetchone(), ("newer message",))
                    self.assertEqual(len(recovery.edits(connection)), count)
                    self.assertEqual(connection.execute("SELECT value FROM session_metadata WHERE key=? AND turn_id='turn1'", (recovery.SUMMARY,)).fetchone(), ('{"files":999}',))
                after = self.state()
                for table in original:
                    if table != "turns":
                        self.assertEqual(after[table], original[table], table)
                self.assertEqual(after["turns"], protected["turns"])
                self.assertEqual(recovery.file_hash(self.file), before_file)

    def test_scan_distinguishes_suspicion_from_ordinary_pending(self):
        item = recovery.scan(self.user_data)[0]
        self.assertIn("not proof", item["status"])
        self.create_database(case_variants=True)
        item = recovery.scan(self.user_data)[0]
        self.assertEqual(item["status"], "Suspected path-case conflict")
        self.assertEqual(item["file_count"], 1)

    def test_empty_history_not_repairable(self):
        self.create_database(0)
        item = recovery.scan(self.user_data)[0]
        self.assertEqual(item["status"], "No pending edits")
        self.assertFalse(item["can_prepare"])
        with self.assertRaisesRegex(recovery.Refused, "No pending"):
            self.preview()

    def test_unsupported_states_are_visible_and_block_preparation(self):
        cases = [
            "PRAGMA user_version=21",
            "UPDATE session_metadata SET value='[\"peer\"]' WHERE key='peerChats'",
            "INSERT INTO session_metadata VALUES('foreignTurns','[]',NULL)",
            "INSERT INTO reviewed_files VALUES('uri','nonce')",
            "UPDATE file_edits SET edit_type='delete'",
            "UPDATE file_edits SET before_content=NULL",
            "CREATE TRIGGER extra AFTER INSERT ON turns BEGIN SELECT 1; END",
        ]
        for sql in cases:
            with self.subTest(sql=sql):
                self.create_database()
                self.change(sql)
                before = self.state()
                item = recovery.scan(self.user_data)[0]
                self.assertEqual(item["status"], "Unsupported / error")
                self.assertFalse(item["can_prepare"])
                self.assertTrue(item["evidence"])
                with self.assertRaises(recovery.Refused):
                    self.preview()
                self.assertEqual(before, self.state())

    def test_new_and_changed_edits_refuse_without_mutation(self):
        for sql in (
            "UPDATE file_edits SET after_content=X'ff' WHERE tool_call_id='tool0'",
            "INSERT INTO file_edits SELECT turn_id,'unexpected',file_path,edit_type,original_path,before_content,after_content,added_lines,removed_lines FROM file_edits LIMIT 1",
        ):
            with self.subTest(sql=sql):
                self.create_database()
                incident = self.preview()
                self.change(sql)
                before = self.state()
                with self.assertRaisesRegex(recovery.Refused, "changed since preview"):
                    self.operate(incident)
                self.assertEqual(before, self.state())

    def test_changed_file_refuses(self):
        incident = self.preview()
        self.file.write_bytes(b"externally changed")
        before = self.state()
        with self.assertRaisesRegex(recovery.Refused, "file changed"):
            self.operate(incident)
        self.assertEqual(before, self.state())

    def test_failed_postcondition_rolls_back(self):
        incident = self.preview()
        before = self.state()
        with patch.object(recovery, "ensure_offline"), patch.object(
            recovery, "integrity", side_effect=[None, None, recovery.Refused("postcondition failed")]
        ):
            with self.assertRaisesRegex(recovery.Refused, "postcondition"):
                recovery.operate(incident)
        self.assertEqual(before, self.state())

    def test_current_messages_added_after_preparation_are_preserved(self):
        incident = self.preview()
        self.change("INSERT INTO turns VALUES('after-preview','retain current')")
        self.operate(incident)
        with recovery.read_db(self.database) as connection:
            self.assertEqual(connection.execute("SELECT message FROM turns WHERE id='after-preview'").fetchone(), ("retain current",))

    def test_restore_refuses_new_reviews(self):
        incident = self.preview()
        self.operate(incident)
        self.change(
            "INSERT INTO file_edits VALUES('turn1','new',?,'edit',NULL,?,?,1,0)",
            (str(self.file), b"x", b"y"),
        )
        before = self.state()
        with self.assertRaisesRegex(recovery.Refused, "Review state changed"):
            self.operate(incident, True)
        self.assertEqual(before, self.state())

    def test_backup_captures_committed_wal(self):
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("INSERT INTO turns VALUES('wal','committed WAL')")
        connection.commit()
        incident = self.preview()
        with recovery.read_db(incident.parent / "prepared.db") as backup:
            self.assertEqual(backup.execute("SELECT message FROM turns WHERE id='wal'").fetchone(), ("committed WAL",))

    def test_process_gate_active_failure_and_timeout(self):
        for completed in (
            subprocess.CompletedProcess([], 0, stdout="123", stderr=""),
            subprocess.CompletedProcess([], 1, stdout="", stderr="denied"),
        ):
            with self.subTest(completed=completed), patch("recovery.subprocess.run", return_value=completed):
                with self.assertRaises(recovery.Refused):
                    recovery.ensure_offline()
        with patch("recovery.subprocess.run", side_effect=subprocess.TimeoutExpired("powershell", 10)):
            with self.assertRaisesRegex(recovery.Refused, "Cannot verify"):
                recovery.ensure_offline()

    def test_active_host_prevents_database_mutation(self):
        incident = self.preview()
        before = self.state()
        completed = subprocess.CompletedProcess([], 0, stdout="123", stderr="")
        with patch("recovery.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(recovery.Refused, "Close all"):
                recovery.operate(incident)
        self.assertEqual(before, self.state())

    def test_backup_tampering_refuses(self):
        incident = self.preview()
        with (incident.parent / "prepared.db").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(recovery.Refused, "hash mismatch"):
            self.operate(incident)

    def test_arbitrary_sdk_or_outside_paths_are_rejected(self):
        other = self.root / "session.db"
        other.write_bytes(b"")
        with self.assertRaises(recovery.Refused):
            recovery.validate_source(other, self.user_data)

    def test_gui_preview_binding_and_safe_cancel(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        ui = app.Application(root, self.user_data)
        ui.populate(recovery.scan(self.user_data))
        self.assertEqual(str(ui.repair_button["state"]), "disabled")
        ui.tree.selection_set("0")
        ui.selected()
        self.assertIn(str(self.database), ui.details.get("1.0", "end"))
        self.assertEqual(str(ui.prepare_button["state"]), "normal")
        ui.incident = self.preview()
        ui.acknowledged.set(True)
        ui.update_buttons()
        self.assertEqual(str(ui.repair_button["state"]), "disabled")
        self.assertEqual(str(ui.restore_button["state"]), "disabled")
        with patch.object(ui, "run") as worker:
            ui.repair()
            ui.restore()
            worker.assert_not_called()
        ui.read_only = False
        ui.update_buttons()
        self.assertEqual(str(ui.repair_button["state"]), "normal")
        before = self.state()
        with patch("app.messagebox.askyesno", return_value=False), patch.object(ui, "run") as worker:
            ui.repair()
            worker.assert_not_called()
        self.assertEqual(before, self.state())
        ui.selected()
        self.assertIsNone(ui.incident)
        self.assertEqual(str(ui.repair_button["state"]), "disabled")

    def test_gui_mode_checks_fail_closed_and_enable_only_offline(self):
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        ui = app.Application(root, self.user_data)

        def immediate(action, callback):
            callback(action())
            ui.update_buttons()

        with patch.object(ui, "run", side_effect=immediate):
            with patch.object(recovery, "ensure_offline", side_effect=recovery.Refused("Code is open")):
                ui.check_mode()
            self.assertTrue(ui.read_only)
            self.assertIn("Code is open", ui.mode.get())
            self.assertEqual(str(ui.restore_button["state"]), "disabled")
            self.assertEqual(str(ui.scan_button["state"]), "normal")
            with patch.object(recovery, "ensure_offline"):
                ui.check_mode()
            self.assertFalse(ui.read_only)
            self.assertEqual(str(ui.restore_button["state"]), "normal")
            with patch.object(recovery, "ensure_offline", side_effect=recovery.Refused("Check unavailable")):
                ui.check_mode()
            self.assertTrue(ui.read_only)
            self.assertEqual(str(ui.restore_button["state"]), "disabled")


if __name__ == "__main__":
    unittest.main()
