"""Conservative, local-only recovery of schema-20 VS Code chat reviews."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SUMMARY = "agentHost.changes"
ZERO = '{"additions":0,"deletions":0,"files":0}'
EDIT_COLUMNS = (
    "turn_id", "tool_call_id", "file_path", "edit_type", "original_path",
    "before_content", "after_content", "added_lines", "removed_lines",
)


class Refused(RuntimeError):
    """The operation cannot safely continue."""


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def digest(value) -> str:
    def encode(item):
        if isinstance(item, bytes):
            return {"blob": item.hex()}
        raise TypeError(f"Unsupported value: {type(item).__name__}")

    return hashlib.sha256(
        json.dumps(value, default=encode, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def atomic_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def read_db(path: Path):
    connection = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def validate_source(path: Path, user_data: Path) -> Path:
    user_data = user_data.resolve(strict=True)
    sessions = user_data / "agentSessionData"
    candidate = path.resolve(strict=True)
    if (
        path.is_symlink() or path.parent.is_symlink() or sessions.is_symlink()
        or candidate.name != "session.db"
        or candidate.parent.parent != sessions
    ):
        raise Refused("Select a direct host session.db under this user-data directory.")
    try:
        uuid.UUID(candidate.parent.name)
    except ValueError as error:
        raise Refused("The host conversation directory is not a session UUID.") from error
    return candidate


def tables(connection):
    return [
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def schema(connection) -> str:
    return digest((
        connection.execute("PRAGMA user_version").fetchone()[0],
        connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall(),
    ))


def integrity(connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise Refused("SQLite integrity check failed.")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise Refused("SQLite foreign-key check failed.")


def metadata(connection) -> dict[str, str]:
    result = {}
    for key, value in connection.execute(
        "SELECT key,value FROM session_metadata WHERE turn_id IS NULL"
    ):
        if key in result:
            raise Refused("Duplicate global metadata keys.")
        result[key] = value
    return result


def edits(connection):
    return connection.execute(
        "SELECT " + ",".join(EDIT_COLUMNS) + " FROM file_edits ORDER BY tool_call_id,file_path"
    ).fetchall()


def protected_state(connection, exclude_review=False):
    result = {"schema": schema(connection)}
    for name in tables(connection):
        rows = connection.execute("SELECT * FROM " + quoted(name)).fetchall()
        if exclude_review and name == "file_edits":
            continue
        if exclude_review and name == "session_metadata":
            rows = [row for row in rows if not (row[0] == SUMMARY and row[2] is None)]
        result[name] = digest(sorted(digest(row) for row in rows))
    return result


def check_supported(connection) -> None:
    if connection.execute("PRAGMA user_version").fetchone()[0] != 20:
        raise Refused("Read-only: only schema version 20 is supported for repair.")
    expected = {
        "file_edits": EDIT_COLUMNS,
        "session_metadata": ("key", "value", "turn_id"),
        "reviewed_files": ("uri", "nonce"),
    }
    for table, columns in expected.items():
        if tuple(r[1] for r in connection.execute("PRAGMA table_info(" + quoted(table) + ")")) != columns:
            raise Refused(f"Unsupported {table} layout.")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone():
        raise Refused("Database triggers require separate investigation.")
    pk = [
        row[1] for row in sorted(
            connection.execute("PRAGMA table_info(file_edits)").fetchall(),
            key=lambda row: row[5],
        ) if row[5]
    ]
    if pk != ["tool_call_id", "file_path"]:
        raise Refused("Unsupported edit primary key.")
    fk = connection.execute("PRAGMA foreign_key_list(file_edits)").fetchall()
    if len(fk) != 1 or (fk[0][2], fk[0][3], fk[0][4], fk[0][6]) != (
        "turns", "turn_id", "id", "CASCADE"
    ):
        raise Refused("Unsupported edit ownership relationship.")
    for table in tables(connection):
        if any(row[2] == "file_edits" for row in connection.execute(
            "PRAGMA foreign_key_list(" + quoted(table) + ")"
        )):
            raise Refused("Other data references edit rows.")
    meta = metadata(connection)
    if json.loads(meta.get("peerChats", "null")) != []:
        raise Refused("Peer conversations are unsupported.")
    if connection.execute(
        "SELECT 1 FROM session_metadata WHERE lower(key) LIKE '%foreign%' OR lower(key) LIKE '%owner%'"
    ).fetchone():
        raise Refused("Shared or foreign conversation ownership is unsupported.")
    if connection.execute("SELECT COUNT(*) FROM reviewed_files").fetchone()[0]:
        raise Refused("Nonempty reviewed-file state requires separate investigation.")
    summary = json.loads(meta.get(SUMMARY, "null"))
    if (
        not isinstance(summary, dict) or set(summary) != {"additions", "deletions", "files"}
        or any(type(value) is not int or value < 0 for value in summary.values())
    ):
        raise Refused("Unsupported or missing global changes summary.")
    for kind, original, before_type, after_type in connection.execute(
        "SELECT edit_type,original_path,typeof(before_content),typeof(after_content) FROM file_edits"
    ):
        if kind not in {"create", "edit"} or original is not None:
            raise Refused("Only ordinary create/edit histories are supported; no renames or accept records.")
        if before_type != "blob" or after_type != "blob":
            raise Refused("Missing edit snapshots require separate investigation.")


def check_local_file(path: Path) -> None:
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise Refused("Only absolute local file paths are supported.")
    if not path.is_file() or path.is_symlink():
        raise Refused(f"Affected file is missing, not regular, or a link: {path}")
    if any(parent.is_symlink() for parent in path.parents):
        raise Refused("Linked affected-file directories are unsupported.")


def file_guards(rows):
    result = {}
    for row in rows:
        path = Path(row[2])
        check_local_file(path)
        resolved = str(path.resolve())
        result[resolved] = file_hash(path)
    return result


def verify_files(guards) -> None:
    for name, expected in guards.items():
        path = Path(name)
        check_local_file(path)
        if file_hash(path) != expected:
            raise Refused(f"Affected file changed since preview: {path}")


def index_guards(files):
    result = {}
    for name in files:
        for parent in Path(name).parents:
            marker = parent / ".git"
            if marker.exists():
                if not marker.is_dir():
                    raise Refused("Linked Git worktrees/submodules need separate index verification.")
                index = marker / "index"
                if index.is_file():
                    result[str(index)] = file_hash(index)
                break
    return result


def ensure_offline() -> None:
    if sys.platform != "win32":
        raise Refused("Repair and restore currently support Windows only.")
    command = (
        "$ErrorActionPreference='Stop'; "
        "Get-Process -ErrorAction Stop | Where-Object { "
        "$_.ProcessName -in @('Code','Code-insiders','Code - Insiders','VSCodium') "
        "} | ForEach-Object { $_.Id }"
    )
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        completed = subprocess.run(
            [str(executable), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise Refused(f"Cannot verify host processes: {error}") from error
    if completed.returncode or completed.stderr.strip():
        raise Refused("Host process verification failed. Nothing was repaired.")
    if completed.stdout.strip():
        raise Refused("Close all VS Code/Insiders windows normally first. Do not terminate another user's processes.")


def snapshot(source: Path, destination: Path) -> str:
    with destination.open("xb"):
        pass
    with read_db(source) as src:
        dst = sqlite3.connect(destination)
        try:
            deadline = time.monotonic() + 30

            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise Refused("SQLite backup timed out; source was not modified.")

            src.backup(dst, pages=128, progress=progress)
            integrity(dst)
        finally:
            dst.close()
    return file_hash(destination)


def default_user_data() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Code"


def default_incidents() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "VSCodeChatRecovery" / "incidents"


def inspect_session(path: Path, user_data: Path):
    item = {
        "database": str(path), "id": path.parent.name, "title": path.parent.name,
        "status": "Unsupported / error", "evidence": "", "edit_count": 0,
        "file_count": 0, "can_prepare": False,
    }
    try:
        path = validate_source(path, user_data)
        with read_db(path) as connection:
            check_supported(connection)
            meta = metadata(connection)
            title = meta.get("customTitle") or meta.get("title") or path.parent.name
            item["title"] = title[:200]
            rows = connection.execute("SELECT file_path FROM file_edits").fetchall()
            grouped = {}
            for row in rows:
                grouped.setdefault(row[0].replace("/", "\\").casefold(), set()).add(row[0])
            conflicts = sum(len(values) > 1 for values in grouped.values())
            item.update(
                edit_count=len(rows), file_count=len(grouped),
                status="Suspected path-case conflict" if conflicts else (
                    "Pending review (not proof of damage)" if rows else "No pending edits"
                ),
                evidence=f"{conflicts} case-variant file identities. A spinner cannot be detected from this database.",
                can_prepare=bool(rows),
                paths=sorted({row[0] for row in rows}),
            )
    except (OSError, sqlite3.Error, ValueError, Refused) as error:
        item["evidence"] = str(error)
    return item


def scan(user_data: Path):
    sessions = user_data / "agentSessionData"
    if not sessions.is_dir():
        raise Refused(f"No host conversations directory: {sessions}")
    return [
        inspect_session(path / "session.db", user_data)
        for path in sorted(sessions.iterdir())
        if path.is_dir() and (path / "session.db").is_file()
    ]


def prepare(database: Path, user_data: Path, destination: Path) -> Path:
    database = validate_source(database, user_data)
    destination.mkdir(parents=True, exist_ok=True)
    incident = destination / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex)
    incident.mkdir()
    backup = incident / "prepared.db"
    backup_hash = snapshot(database, backup)
    with read_db(backup) as connection:
        check_supported(connection)
        rows = edits(connection)
        if not rows:
            raise Refused("No pending edit records to reset.")
        guards = file_guards(rows)
        plan = {
            "format": 1, "database": str(database), "user_data": str(user_data.resolve()),
            "schema": schema(connection), "edits_digest": digest(rows),
            "edit_keys": [[row[1], row[2]] for row in rows],
            "summary": metadata(connection)[SUMMARY], "files": guards,
            "prepared_backup": backup.name, "prepared_hash": backup_hash,
            "status": "prepared", "host_id": database.parent.name,
        }
    atomic_json(incident / "incident.json", plan)
    return incident / "incident.json"


def load_plan(path: Path):
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("format") != 1 or path.name != "incident.json":
        raise Refused("Select an incident.json generated by this tool.")
    database = validate_source(Path(plan["database"]), Path(plan["user_data"]))
    if database.parent.name != plan["host_id"] or path.resolve().parent == database.parent:
        raise Refused("Incident identity or storage location is invalid.")
    backup = path.parent / plan["prepared_backup"]
    if backup.name != "prepared.db" or backup.parent != path.parent:
        raise Refused("Invalid prepared backup path.")
    if file_hash(backup) != plan["prepared_hash"]:
        raise Refused("Prepared backup hash mismatch.")
    with read_db(backup) as connection:
        check_supported(connection)
        if (
            schema(connection) != plan["schema"] or digest(edits(connection)) != plan["edits_digest"]
            or metadata(connection)[SUMMARY] != plan["summary"]
            or [[row[1], row[2]] for row in edits(connection)] != plan["edit_keys"]
        ):
            raise Refused("Incident does not match its original snapshot.")
        if set(plan["files"]) != {str(Path(row[2]).resolve()) for row in edits(connection)}:
            raise Refused("Incident affected-file scope is invalid.")
    return plan, database


def check_current(connection, plan, restoring=False):
    check_supported(connection)
    if schema(connection) != plan["schema"]:
        raise Refused("Schema changed since preview.")
    if restoring:
        if edits(connection) or metadata(connection)[SUMMARY] != ZERO:
            raise Refused("Review state changed after repair; scoped restore refused.")
    elif digest(edits(connection)) != plan["edits_digest"] or metadata(connection)[SUMMARY] != plan["summary"]:
        raise Refused("Edit records or changes summary changed since preview. Scan and prepare again.")


def operate(incident: Path, restoring=False):
    plan, database = load_plan(incident)
    ensure_offline()
    if restoring:
        name = plan.get("repair_backup")
        if not name or Path(name).name != name or not name.startswith("pre-repair-"):
            raise Refused("No exact pre-repair backup recorded for this incident.")
        restore_from = incident.parent / name
        if file_hash(restore_from) != plan["repair_backup_hash"]:
            raise Refused("Pre-repair backup hash mismatch.")
        with read_db(restore_from) as original:
            integrity(original)
            check_current(original, plan)
            original_rows = edits(original)
    verify_files(plan["files"])
    indexes = index_guards(plan["files"])
    with read_db(database) as source:
        integrity(source)
        check_current(source, plan, restoring)
    action = "restore" if restoring else "repair"
    fresh = incident.parent / f"pre-{action}-{uuid.uuid4().hex}.db"
    fresh_hash = snapshot(database, fresh)
    if not restoring:
        plan["repair_backup"] = fresh.name
        plan["repair_backup_hash"] = fresh_hash
    plan["status"] = action + "_pending"
    plan["last_safety_backup"] = fresh.name
    atomic_json(incident, plan)
    ensure_offline()
    uri = database.as_uri() + "?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=2, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN EXCLUSIVE")
        with read_db(fresh) as saved:
            if protected_state(connection) != protected_state(saved):
                raise Refused("Database changed after the fresh backup.")
        check_current(connection, plan, restoring)
        before = protected_state(connection, exclude_review=True)
        if restoring:
            for row in original_rows:
                connection.execute(
                    "INSERT INTO file_edits (" + ",".join(EDIT_COLUMNS) + ") VALUES (?,?,?,?,?,?,?,?,?)", row
                )
            target_summary, expected_summary = plan["summary"], ZERO
        else:
            for key in plan["edit_keys"]:
                cursor = connection.execute(
                    "DELETE FROM file_edits WHERE tool_call_id=? AND file_path=?", key
                )
                if cursor.rowcount != 1:
                    raise Refused("An edit was not removed exactly once.")
            target_summary, expected_summary = ZERO, plan["summary"]
        cursor = connection.execute(
            "UPDATE session_metadata SET value=? WHERE key=? AND turn_id IS NULL AND value=?",
            (target_summary, SUMMARY, expected_summary),
        )
        if cursor.rowcount != 1:
            raise Refused("The single global summary was not updated exactly once.")
        if protected_state(connection, exclude_review=True) != before:
            raise Refused("Protected conversation data changed. Rolling back.")
        check_current(connection, plan, not restoring)
        integrity(connection)
        verify_files(plan["files"])
        verify_files(indexes)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    plan["status"] = "restored" if restoring else "applied"
    try:
        atomic_json(incident, plan)
    except OSError as error:
        raise Refused(
            f"Database operation COMMITTED, but receipt update failed: {error}. "
            f"Keep incident {incident} and backup {fresh}; do not retry blindly."
        ) from error
    return {
        "status": plan["status"], "records": len(plan["edit_keys"]),
        "safety_backup": str(fresh), "incident": str(incident),
        "message": "Reopen the same conversation and verify history and the original UI symptom.",
    }
