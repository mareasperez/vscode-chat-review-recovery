import base64
import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import app
import recovery
from qt_support import make_ui, process_events
import test_recovery


class CatalogPreviewTests(unittest.TestCase):
    setUp = test_recovery.RecoveryTests.setUp
    create_database = test_recovery.RecoveryTests.create_database

    def catalog_fixture(self):
        self.owner = f"copilotcli:/{self.host.name}"
        encoded = base64.urlsafe_b64encode(self.owner.encode()).decode().rstrip("=")
        self.main_chat = f"ahp-chat://default/{encoded}"
        self.fork_chat = f"ahp-chat://{uuid.uuid4()}/{encoded}"
        self.catalog = self.user_data / "User" / "globalStorage" / "agent-host.db"
        self.catalog.parent.mkdir(parents=True)
        with test_recovery.writable(self.catalog) as connection:
            connection.executescript("""
                CREATE TABLE sessions_v2(session_uri TEXT, provider TEXT, start_time);
                CREATE TABLE chats_v2(
                    owner_session_uri,chat_uri,parent_chat,origin_session_uri,
                    inherited_turn_id,storage_resource,origin);
            """)
            connection.execute("INSERT INTO sessions_v2 VALUES(?,?,?)",
                               (self.owner, "copilotcli", 1704067200000))
            connection.executemany("INSERT INTO chats_v2 VALUES(?,?,?,?,?,?,?)", [
                (self.owner, self.main_chat, None, None, None, self.owner, '{"kind":"user"}'),
                (self.owner, self.fork_chat, None, self.owner, "anchor", self.fork_chat,
                 json.dumps({"kind": "fork", "chat": self.main_chat, "turnId": "anchor"})),
            ])
        fork = self.host.with_name(recovery.catalog_storage_key(self.fork_chat))
        fork.mkdir()
        self.fork_database = fork / "session.db"
        self.fork_database.write_bytes(self.database.read_bytes())
        with test_recovery.writable(self.database) as connection:
            connection.execute("UPDATE session_metadata SET value=? WHERE key='peerChats'",
                               (json.dumps([self.fork_chat]),))
        for path in (self.database, self.fork_database):
            with test_recovery.writable(path) as connection:
                connection.execute("""CREATE TABLE local_turns(
                    turn_id TEXT PRIMARY KEY,chat_uri TEXT,anchor_turn_id TEXT,
                    seq INTEGER,payload TEXT,kind TEXT)""")

    def insert_turn(self, database, turn, chat, text, **changes):
        payload = {
            "id": turn, "message": {"text": text, "origin": {"kind": "user"}},
            "responseParts": [
                {"kind": "markdown", "content": "local assistant"},
                {"kind": "toolCall", "content": "DO NOT DISPLAY TOOL"},
            ], "state": "complete",
        }
        payload.update(changes)
        with test_recovery.writable(database) as connection:
            connection.execute("INSERT INTO local_turns VALUES(?,?,NULL,1,?,'local')",
                               (turn, chat, json.dumps(payload)))

    def test_exact_standalone_fork_mapping_authorizes_only_the_fork(self):
        self.catalog_fixture()
        rows = {item["database"]: item for item in recovery.scan(self.user_data)}
        main, fork = rows[str(self.database)], rows[str(self.fork_database)]
        self.assertEqual(main["kind"], "Session")
        self.assertEqual(fork["kind"], "Fork")
        self.assertEqual(fork["parent_id"], self.host.name)
        self.assertFalse(main["can_prepare"])
        self.assertTrue(fork["can_prepare"])
        with self.assertRaises(recovery.Refused):
            recovery.prepare(self.database, self.user_data, self.incidents)
        incident = recovery.prepare(self.fork_database, self.user_data, self.incidents)
        plan, selected = recovery.load_plan(incident)
        self.assertEqual(selected, self.fork_database.resolve())
        self.assertEqual(plan["host_id"], self.fork_database.parent.name)

    def test_missing_ambiguous_foreign_and_malicious_catalog_stay_unknown(self):
        self.catalog_fixture()
        for sql, params in [
            ("INSERT INTO chats_v2 SELECT * FROM chats_v2 WHERE chat_uri=?", (self.fork_chat,)),
            ("UPDATE chats_v2 SET owner_session_uri='other:/foreign' WHERE chat_uri=?", (self.fork_chat,)),
            ("UPDATE chats_v2 SET storage_resource='file:///C:/outside/session.db' WHERE chat_uri=?", (self.fork_chat,)),
            ("UPDATE chats_v2 SET origin='[1,2]' WHERE chat_uri=?", (self.fork_chat,)),
            ("UPDATE chats_v2 SET origin_session_uri='other:/foreign' WHERE chat_uri=?", (self.fork_chat,)),
        ]:
            with self.subTest(sql=sql):
                with test_recovery.writable(self.catalog) as connection:
                    original = connection.execute("SELECT * FROM chats_v2").fetchall()
                    connection.execute(sql, params)
                rows = list(recovery.stream_scan(self.user_data))
                fork = next(item for item, _, _ in rows if item["database"] == str(self.fork_database))
                self.assertEqual(fork["kind"], "Unknown")
                self.assertEqual(fork["parent"], "Unavailable")
                with test_recovery.writable(self.catalog) as connection:
                    connection.execute("DELETE FROM chats_v2")
                    connection.executemany("INSERT INTO chats_v2 VALUES(?,?,?,?,?,?,?)", original)
        self.catalog.unlink()
        fork = next(item for item, _, _ in recovery.stream_scan(self.user_data)
                    if item["database"] == str(self.fork_database))
        self.assertEqual(fork["kind"], "Unknown")

    def test_nested_fork_and_tool_child_use_exact_source_chat_parent(self):
        self.catalog_fixture()
        encoded = self.fork_chat.rsplit("/", 1)[1]
        for origin_kind in ("fork", "tool"):
            with self.subTest(origin_kind=origin_kind):
                chat = f"ahp-chat://{uuid.uuid4()}/{encoded}"
                origin = {"kind": origin_kind, "chat": self.fork_chat}
                if origin_kind == "fork":
                    origin["turnId"] = "nested-anchor"
                else:
                    origin["toolCallId"] = "nested-tool"
                with test_recovery.writable(self.catalog) as connection:
                    connection.execute("INSERT INTO chats_v2 VALUES(?,?,?,?,?,?,?)", (
                        self.owner, chat, self.fork_chat, self.owner,
                        "nested-anchor" if origin_kind == "fork" else None,
                        chat, json.dumps(origin),
                    ))
                storage_key = recovery.catalog_storage_key(chat)
                directory = self.host.with_name(storage_key)
                directory.mkdir()
                database = directory / "session.db"
                database.write_bytes(self.fork_database.read_bytes())
                item = next(item for item, _, _ in recovery.stream_scan(self.user_data)
                            if item["database"] == str(database))
                self.assertEqual(item["kind"], "Fork" if origin_kind == "fork" else "Child chat (tool)")
                self.assertEqual(item["parent_id"], self.fork_database.parent.name)
                self.assertEqual(item["parent"], self.fork_database.parent.name)
                self.assertNotEqual(item["parent_id"], self.host.name)
                self.assertFalse(item["can_prepare"])
                with self.assertRaises(recovery.Refused):
                    recovery.prepare(database, self.user_data, self.incidents)

    def test_nested_parent_source_must_be_unique_and_have_valid_storage_identity(self):
        self.catalog_fixture()
        encoded = self.fork_chat.rsplit("/", 1)[1]
        nested = f"ahp-chat://{uuid.uuid4()}/{encoded}"
        with test_recovery.writable(self.catalog) as connection:
            connection.execute("INSERT INTO chats_v2 VALUES(?,?,?,?,?,?,?)", (
                self.owner, nested, None, self.owner, "nested-anchor", nested,
                json.dumps({"kind": "fork", "chat": self.fork_chat, "turnId": "nested-anchor"}),
            ))
        for sql, params in (
            ("INSERT INTO chats_v2 SELECT * FROM chats_v2 WHERE chat_uri=?", (self.fork_chat,)),
            ("UPDATE chats_v2 SET storage_resource=? WHERE chat_uri=?", (self.owner, self.fork_chat)),
            ("UPDATE chats_v2 SET owner_session_uri='other:/foreign' WHERE chat_uri=?", (self.fork_chat,)),
            ("UPDATE chats_v2 SET origin=? WHERE chat_uri=?", (
                json.dumps({"kind": "fork", "chat": nested, "turnId": "nested-anchor"}), nested,
            )),
        ):
            with self.subTest(sql=sql):
                with test_recovery.writable(self.catalog) as connection:
                    original = connection.execute("SELECT * FROM chats_v2").fetchall()
                    connection.execute(sql, params)
                _, sessions, chats = recovery.catalog_creation_times(self.user_data, include_chats=True)
                association = recovery.catalog_associations(sessions, chats)
                self.assertEqual(association[recovery.catalog_storage_key(nested).casefold()], {})
                with test_recovery.writable(self.catalog) as connection:
                    connection.execute("DELETE FROM chats_v2")
                    connection.executemany("INSERT INTO chats_v2 VALUES(?,?,?,?,?,?,?)", original)
        _, sessions, chats = recovery.catalog_creation_times(self.user_data, include_chats=True)
        foreign_owner = f"copilotcli:/{uuid.uuid4()}"
        foreign_encoded = base64.urlsafe_b64encode(foreign_owner.encode()).decode().rstrip("=")
        invalid_source = f"ahp-chat://{uuid.uuid4()}/{foreign_encoded}"
        altered = []
        for row in chats:
            row = list(row)
            if row[1] == self.fork_chat:
                row[1], row[5] = invalid_source, invalid_source
            elif row[1] == nested:
                row[6] = json.dumps({"kind": "fork", "chat": invalid_source, "turnId": "nested-anchor"})
            altered.append(tuple(row))
        associations = recovery.catalog_associations(sessions, altered)
        self.assertEqual(associations[recovery.catalog_storage_key(nested).casefold()], {})

    def test_preview_selects_exact_database_and_chat_and_never_response_table(self):
        self.catalog_fixture()
        self.insert_turn(self.database, "main", self.main_chat, "MAIN PRIVATE FIXTURE")
        self.insert_turn(self.fork_database, "other", self.main_chat, "OTHER CHAT PRIVATE FIXTURE")
        self.insert_turn(self.fork_database, "fork", self.fork_chat, "SELECTED FIXTURE")
        statements = []
        original = sqlite3.connect

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_authorizer(
                lambda action, table, column, db, trigger:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table in
                ("turns", "assistant_response_parts") else sqlite3.SQLITE_OK
            )
            connection.set_trace_callback(lambda sql: statements.append(sql.split(" FROM ")[-1].split(" ")[0]))
            return connection

        before = recovery.file_hash(self.fork_database)
        with patch.object(recovery.sqlite3, "connect", side_effect=connect):
            text = recovery.conversation_preview(self.fork_database, self.user_data)
        self.assertIn("SELECTED FIXTURE", text)
        self.assertIn("local assistant", text)
        self.assertNotIn("MAIN PRIVATE FIXTURE", text)
        self.assertNotIn("OTHER CHAT PRIVATE FIXTURE", text)
        self.assertNotIn("DO NOT DISPLAY TOOL", text)
        self.assertIn("PARTIAL LOCAL TURNS", text)
        self.assertEqual(recovery.file_hash(self.fork_database), before)

    def test_scans_and_exports_do_not_read_local_payloads(self):
        self.catalog_fixture()
        self.insert_turn(self.fork_database, "private", self.fork_chat, "PRIVATE SCAN SENTINEL")
        original = sqlite3.connect

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_authorizer(
                lambda action, table, column, db, trigger:
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ
                and table == "local_turns" else sqlite3.SQLITE_OK
            )
            return connection

        with patch.object(recovery.sqlite3, "connect", side_effect=connect):
            rows = recovery.scan(self.user_data)
            streamed = [item for item, _, _ in recovery.stream_scan(self.user_data)]
        self.assertNotIn("PRIVATE SCAN SENTINEL", json.dumps(rows + streamed))
        self.assertEqual(len(rows), 2)

    def test_empty_unknown_and_multiroot_preview_are_honest(self):
        self.catalog_fixture()
        text = recovery.conversation_preview(self.fork_database, self.user_data)
        self.assertIn("No independently attributable messages", text)
        self.assertIn("Inherited/history content is not shown", text)
        self.insert_turn(self.fork_database, "bad", self.fork_chat, "HIDDEN",
                         message={"text": "HIDDEN", "origin": {"kind": "unrecognized"}})
        self.assertNotIn("HIDDEN", recovery.conversation_preview(self.fork_database, self.user_data))
        with test_recovery.writable(self.fork_database) as connection:
            connection.execute("INSERT INTO session_metadata VALUES(?,?,NULL)",
                               ("copilot.workingDirectories", '["C:\\\\One","D:\\\\Two"]'))
        self.assertIn("multi-root", recovery.conversation_preview(self.fork_database, self.user_data))

    def test_unknown_nested_origin_is_not_labeled_fork(self):
        self.catalog_fixture()
        with test_recovery.writable(self.catalog) as connection:
            connection.execute("UPDATE chats_v2 SET origin=NULL WHERE chat_uri=?", (self.fork_chat,))
        fork = next(item for item, _, _ in recovery.stream_scan(self.user_data)
                    if item["database"] == str(self.fork_database))
        self.assertEqual(fork["kind"], "Child chat (origin unknown)")
        self.assertIsNone(fork["parent_id"])
        self.assertEqual(fork["parent"], "Unavailable (source unknown)")

    def test_preview_refuses_foreign_markers_unsupported_version_and_oversized_payload(self):
        self.catalog_fixture()
        self.insert_turn(self.fork_database, "large", self.fork_chat, "PRIVATE OVERSIZED " * 5000)
        text = recovery.conversation_preview(self.fork_database, self.user_data)
        self.assertNotIn("PRIVATE OVERSIZED", text)
        self.assertIn("oversized", text)
        with test_recovery.writable(self.fork_database) as connection:
            connection.execute("PRAGMA user_version=999")
        self.assertIn("schema-20", recovery.conversation_preview(self.fork_database, self.user_data))
        with test_recovery.writable(self.fork_database) as connection:
            connection.execute("PRAGMA user_version=20")
            connection.execute("INSERT INTO session_metadata VALUES(?,?,NULL)",
                               ("agentHost.rowOwnerChat", self.main_chat))
        text = recovery.conversation_preview(self.fork_database, self.user_data)
        self.assertIn("preview unavailable", text.lower())
        self.assertNotIn("PRIVATE OVERSIZED", text)

    def test_unrelated_non_uuid_directory_does_not_inherit_owner_from_its_name(self):
        self.catalog_fixture()
        unrelated = self.host.with_name("looks-like-a-fork-" + self.host.name)
        unrelated.mkdir()
        database = unrelated / "session.db"
        database.write_bytes(self.database.read_bytes())
        item = recovery.inspect_session(database, self.user_data)
        self.assertEqual(item["kind"], "Unknown")
        self.assertFalse(item["can_prepare"])
        self.assertIn("preview unavailable", recovery.conversation_preview(database, self.user_data).lower())
        for uri in (
            "copilotcli:" + self.host.name,
            "file:///C:/outside/session.db",
            "ahp-chat://child/../../outside",
            "ahp-chat://child/not%2fcanonical",
            "ahp-chat:/opaque",
        ):
            self.assertIsNone(recovery.catalog_storage_key(uri))

    def test_stale_preview_cannot_replace_different_selection(self):
        ui = make_ui(self, self.user_data)
        item = recovery.inspect_session(self.database, self.user_data)
        ui.populate([item, dict(item, title="Other", id="other")])
        ui.tree.selection_set("0")
        process_events()
        token = ui.selection_generation
        ui.tree.selection_set("1")
        process_events()
        ui.window._history_finished(token, "0", "STALE PRIVATE FIXTURE")
        self.assertNotIn("STALE PRIVATE FIXTURE", ui.window.history_text.toPlainText())


if __name__ == "__main__":
    unittest.main()
