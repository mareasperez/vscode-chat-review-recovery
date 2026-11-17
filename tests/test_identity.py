import unittest
from datetime import datetime
from pathlib import Path
import time
import uuid
from unittest.mock import patch

import app
import recovery
import test_recovery
from qt_support import make_ui, process_events


class IdentityTests(unittest.TestCase):
    setUp = test_recovery.RecoveryTests.setUp
    create_database = test_recovery.RecoveryTests.create_database
    preview = test_recovery.RecoveryTests.preview

    def old_schema(self, version):
        with test_recovery.writable(self.database) as connection:
            connection.execute(f"PRAGMA user_version={version}")
            connection.execute("DELETE FROM session_metadata WHERE turn_id IS NOT NULL")
            connection.execute("ALTER TABLE session_metadata RENAME TO old_metadata")
            connection.execute("CREATE TABLE session_metadata(key TEXT, value TEXT)")
            connection.execute("INSERT INTO session_metadata SELECT key,value FROM old_metadata")
            connection.execute("DROP TABLE old_metadata")

    def test_identity_across_versions_without_relaxing_repair(self):
        for version in (15, 16, 18, 20):
            with self.subTest(version=version):
                self.create_database()
                if version != 20:
                    self.old_schema(version)
                with test_recovery.writable(self.database) as connection:
                    values = [
                        ("copilot.project.displayName", "Project"),
                        ("copilot.project.uri", "file:///C:/Work/Some%20Folder"),
                        ("copilot.workingDirectories", '["C:\\\\Work\\\\Some Folder", "D:\\\\Other"]'),
                    ]
                    placeholders = "?,?,NULL" if version == 20 else "?,?"
                    connection.executemany(f"INSERT INTO session_metadata VALUES({placeholders})", values)
                item = recovery.scan(self.user_data)[0]
                self.assertEqual(item["title"], "Fixture conversation")
                self.assertEqual(item["project"], "Project")
                self.assertEqual(item["folders"], [r"C:\Work\Some Folder", r"D:\Other"])
                self.assertEqual(item["edit_count"], 5)
                if version != 20:
                    self.assertIn(f"schema {version}", item["status"])
                    self.assertFalse(item["can_prepare"])
                    with self.assertRaises(recovery.Refused):
                        self.preview()
                else:
                    self.assertTrue(item["can_prepare"])

    def test_missing_and_malformed_metadata_and_unreadable_files(self):
        self.old_schema(16)
        with test_recovery.writable(self.database) as connection:
            connection.execute("DELETE FROM session_metadata WHERE key='customTitle'")
            connection.execute("INSERT INTO session_metadata VALUES('copilot.workingDirectories','[broken')")
        item = recovery.scan(self.user_data)[0]
        self.assertEqual(item["title"], self.host.name)
        self.assertEqual(item["project"], "Unknown project")
        self.assertTrue(item["identity_warning"])
        self.assertEqual(item["folders"], [])
        self.database.write_bytes(b"not a database")
        item = recovery.scan(self.user_data)[0]
        self.assertEqual(item["status"], "Unreadable database")
        self.assertIsNone(item["edit_count"])

    def test_empty_and_unknown_edit_layout_have_no_fabricated_count(self):
        self.old_schema(15)
        with test_recovery.writable(self.database) as connection:
            connection.execute("DROP TABLE file_edits")
        item = recovery.scan(self.user_data)[0]
        self.assertIsNone(item["edit_count"])
        self.assertIn("schema 15", item["status"])
        self.database.unlink()
        with test_recovery.writable(self.database):
            pass
        item = recovery.scan(self.user_data)[0]
        self.assertEqual(item["status"], "Empty or unrecognized database")
        self.assertIsNone(item["edit_count"])

    def test_reject_remote_bad_and_malformed_folder_values(self):
        for value in ("file://server/share/folder", "file:///C:/bad%zz", "relative", "\\\\server\\share", 5):
            self.assertIsNone(recovery.local_folder(value))
        name, folders, warning = recovery.project_identity({
            "copilot.workingDirectories": '["C:\\\\Good", 4, "file://server/path"]'
        })
        self.assertEqual(folders, [r"C:\Good"])
        self.assertTrue(warning)

    def test_non_uuid_host_is_readable_but_not_repairable(self):
        destination = self.host.with_name("historical-host")
        self.host.rename(destination)
        database = destination / "session.db"
        item = recovery.inspect_session(database, self.user_data)
        self.assertEqual(item["title"], "Fixture conversation")
        self.assertEqual(item["edit_count"], 5)
        self.assertFalse(item["can_prepare"])
        self.assertIn("not a session UUID", item["evidence"])
        with self.assertRaises(recovery.Refused):
            recovery.prepare(database, self.user_data, self.incidents)

    def test_date_format_is_local_and_handles_invalid_values(self):
        self.assertEqual(
            recovery.format_file_time(1704067200),
            datetime.fromtimestamp(1704067200).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
        with patch.object(recovery, "datetime") as clock:
            clock.fromtimestamp.side_effect = OverflowError
            self.assertEqual(recovery.format_file_time(0), "Unavailable")

    def catalog(self):
        path = self.user_data / "User" / "globalStorage" / "agent-host.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        with test_recovery.writable(path) as connection:
            connection.execute("CREATE TABLE sessions_v2(session_uri TEXT, provider TEXT, start_time)")
        return path

    def test_creation_catalog_exact_sparse_and_ambiguous_matches(self):
        catalog = self.catalog()
        other = str(uuid.uuid4())
        with test_recovery.writable(catalog) as connection:
            connection.executemany("INSERT INTO sessions_v2 VALUES(?,?,?)", [
                (f"copilotcli:/{self.host.name}", "copilotcli", 1704067200123),
                (f"copilotcli:/{other}", "copilotcli", 1704067200000),
                (f"copilotcli:/{other}", "copilotcli", 1704067200001),
                (f"copilotcli:/prefix-{self.host.name}", "copilotcli", 1704067200000),
                (f"other:/{self.host.name}", "other", 1704067200000),
                (f"copilotcli:/{self.host.name.upper()}", "copilotcli", 1704067200000),
            ])
        with patch.object(recovery, "read_db", wraps=recovery.read_db) as reader:
            item = recovery.scan(self.user_data)[0]
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(item["created"], 1704067200.123)
        self.assertTrue(item["catalog_available"])
        self.assertNotIn(other, recovery.catalog_creation_times(self.user_data))
        with test_recovery.writable(catalog) as connection:
            connection.execute("INSERT INTO sessions_v2 VALUES(?,?,?)",
                               (f"copilotcli:/{self.host.name}", "copilotcli", None))
        self.assertIsNone(recovery.scan(self.user_data)[0]["created"])

    def test_catalog_absent_bad_schema_and_invalid_times_are_unavailable(self):
        self.assertFalse(recovery.scan(self.user_data)[0]["catalog_available"])
        catalog = self.catalog()
        for value in (None, "1704067200000", -1, float("inf"), 9223372036854775807):
            with self.subTest(value=value), test_recovery.writable(catalog) as connection:
                connection.execute("DELETE FROM sessions_v2")
                connection.execute("INSERT INTO sessions_v2 VALUES(?,?,?)",
                                   (f"copilotcli:/{self.host.name}", "copilotcli", value))
            self.assertIsNone(recovery.scan(self.user_data)[0]["created"])
        with test_recovery.writable(catalog) as connection:
            connection.execute("DELETE FROM sessions_v2")
            connection.execute("INSERT INTO sessions_v2 VALUES(?,?,?)",
                               (f"copilotcli:/{self.host.name}", "copilotcli", 1704067200123.0))
        self.assertEqual(recovery.scan(self.user_data)[0]["created"], 1704067200.123)
        with test_recovery.writable(catalog) as connection:
            connection.execute("DROP TABLE sessions_v2")
            connection.execute("CREATE TABLE sessions_v2(session_uri TEXT)")
        item = recovery.scan(self.user_data)[0]
        self.assertFalse(item["catalog_available"])
        self.assertEqual(item["title"], "Fixture conversation")
        catalog.write_bytes(b"invalid sqlite")
        self.assertFalse(recovery.scan(self.user_data)[0]["catalog_available"])

    def test_sort_order_selection_and_detail_are_safe(self):
        ui = make_ui(self, self.user_data)
        base = recovery.scan(self.user_data)[0]
        rows = [
            dict(base, title="Z", id="z", created=None, modified=None, catalog_available=True),
            dict(base, title="B", id="b", created=10, modified=30, catalog_available=True),
            dict(base, title="A", id="a", created=10, modified=20, catalog_available=True),
            dict(base, title="C", id="c", created=5, modified=20, catalog_available=True),
        ]
        ui.populate(rows)
        self.assertEqual(ui.tree.get_children(), ("2", "1", "3", "0"))
        self.assertEqual(ui.tree.item("2")["values"][0], "A")
        self.assertIn("Technical evidence appears after selection", ui.details.get())
        ui.tree.selection_set("2")
        process_events()
        self.assertIn("Created: " + recovery.format_file_time(10),
                      ui.details.get("1.0", "end"))
        ui.incident = Path("incident.json")
        ui.acknowledged.set(True)
        for order, expected in (
            (app.SORTS[1], ("3", "2", "1", "0")),
            (app.SORTS[2], ("1", "2", "3", "0")),
            (app.SORTS[3], ("2", "3", "1", "0")),
            (app.SORTS[4], ("2", "1", "3", "0")),
        ):
            ui.sort_order.set(order)
            ui.sort_changed()
            self.assertEqual(ui.tree.get_children(), expected)
        self.assertIsNone(ui.incident)
        self.assertFalse(ui.acknowledged.get())
        self.assertFalse(ui.tree.selection())
        ui.sort_order.set("Message")
        ui.sort_changed()
        self.assertEqual(ui.sort_order.get(), app.SORTS[4])
        ui.populate(rows)
        ui.project_filter.set(ui.project_label(rows[0]))
        self.assertEqual(ui.project_filter.get(), ui.project_label(rows[0]))

    def test_large_list_project_filter_renders_all_rows(self):
        ui = make_ui(self, self.user_data)
        base = recovery.scan(self.user_data)[0]
        rows = [dict(base, title=f"Item {i}", project="Alpha" if i % 2 else "Beta",
                     folders=[r"C:\Folder", r"D:\Other"]) for i in range(1101)]
        ui.populate(rows)
        self.assertEqual(len(ui.tree.get_children()), 1101)
        ui.project_filter.set(ui.project_label(rows[1]))
        ui.apply_filters()
        process_events()
        self.assertEqual(len(ui.tree.get_children()), 550)
        self.assertIn(r"D:\Other", ui.project_label(rows[1]))

    def test_filter_clears_incident_and_preview_never_loads_message(self):
        ui = make_ui(self, self.user_data)
        ui.populate(recovery.scan(self.user_data))
        ui.tree.selection_set("0")
        process_events()
        ui.incident = Path("incident.json")
        ui.acknowledged.set(True)
        ui.search_text.set("not present")
        self.assertIsNone(ui.incident)
        self.assertFalse(ui.acknowledged.get())
        self.assertFalse(ui.tree.selection())
        ui.search_text.set("")
        ui.tree.selection_set("0")
        with patch.object(recovery, "read_db", side_effect=AssertionError("preview read")):
            ui.window.show_history()
        self.assertIn("no verified chat mapping", ui.window.history_text.toPlainText().lower())
        self.assertNotIn("original message", ui.window.history_text.toPlainText())
        ui.search_text.set("not present")
        ui.window._prepared("0", Path("unexpected.db"), Path("unexpected-incident.json"))
        self.assertIsNone(ui.incident)
        self.assertIn("Selection changed", ui.status.get())
        ui.user_data.set(str(self.root))
        self.assertFalse(ui.items)


if __name__ == "__main__":
    unittest.main()
