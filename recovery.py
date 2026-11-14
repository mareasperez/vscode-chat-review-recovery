"""Conservative, local-only recovery of schema-20 VS Code chat reviews."""

from __future__ import annotations

import hashlib
import base64
import json
import math
import ntpath
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

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
def read_db(path: Path, timeout=2):
    connection = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=timeout)
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
        if not verified_standalone_fork(candidate, user_data):
            raise Refused(
                "The conversation directory is not a session UUID and has no exact standalone fork mapping."
            ) from error
    return candidate


def verified_standalone_fork(database: Path, user_data: Path) -> bool:
    """Require one exact catalog-owned fork resource before accepting a non-UUID directory."""
    try:
        _, sessions, chats = catalog_creation_times(user_data, include_chats=True)
        association = catalog_associations(sessions, chats).get(database.parent.name.casefold(), {})
        resource = association.get("storage_resource")
        parent = association.get("parent_id")
        if (
            association.get("kind") != "Fork"
            or not isinstance(resource, str)
            or association.get("chat_uri") != resource
            or catalog_storage_key(resource) != database.parent.name
            or not isinstance(parent, str)
            or association.get("owner_uri") != f"copilotcli:/{parent}"
        ):
            return False
        return str(uuid.UUID(parent)) == parent
    except (OSError, sqlite3.Error, TypeError, ValueError, AttributeError):
        return False


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


def identity_metadata(connection):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(session_metadata)")}
    if not {"key", "value"}.issubset(columns):
        raise Refused("Unrecognized session metadata layout.")
    keys = ("customTitle", "title", "copilot.project.displayName", "copilot.project.uri",
            "copilot.workingDirectories", "copilot.workingDirectory")
    sql = "SELECT key,value FROM session_metadata WHERE key IN (" + ",".join("?" for _ in keys) + ")"
    if "turn_id" in columns:
        sql += " AND turn_id IS NULL"
    result = {}
    for key, value in connection.execute(sql, keys):
        if key in result:
            raise Refused("Duplicate identity metadata keys.")
        result[key] = value
    return result


def local_folder(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.lower().startswith("file:"):
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        if parsed.scheme.lower() != "file" or parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            return None
        if re.search(r"%(?![0-9a-fA-F]{2})", parsed.path):
            return None
        value = unquote(parsed.path)
        if len(value) >= 3 and value[0] == "/" and value[2] == ":":
            value = value[1:]
    if "\x00" in value or not re.match(r"^[A-Za-z]:[\\/]", value):
        return None
    return ntpath.normpath(value)


def project_identity(meta):
    folders, invalid = [], False
    for key in ("copilot.project.uri", "copilot.workingDirectory", "copilot.workingDirectories"):
        value = meta.get(key)
        if value is None:
            continue
        if key == "copilot.workingDirectories":
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                invalid = True
                continue
            if not isinstance(value, list):
                invalid = True
                continue
        else:
            value = [value]
        for raw in value:
            path = local_folder(raw)
            if path is None:
                invalid = True
            elif not any(ntpath.normcase(existing) == ntpath.normcase(path) for existing in folders):
                folders.append(path)
    name = meta.get("copilot.project.displayName")
    name = name.strip()[:200] if isinstance(name, str) and name.strip() else "Unknown project"
    return name, folders, invalid


def format_file_time(timestamp):
    try:
        return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (OSError, OverflowError, TypeError, ValueError):
        return "Unavailable"


def catalog_creation_times(user_data: Path, *, include_chats=False):
    """Return exact, unambiguous host IDs and creation times in seconds."""
    catalog = user_data / "User" / "globalStorage" / "agent-host.db"
    unavailable = (None, [], []) if include_chats else None
    try:
        if (str(user_data).startswith(("\\\\", "//")) or catalog.is_symlink()
                or catalog.parent.is_symlink() or catalog.parent.parent.is_symlink()):
            return unavailable
        with read_db(catalog) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions_v2)")}
            if not {"session_uri", "provider", "start_time"}.issubset(columns):
                return unavailable
            rows = connection.execute(
                "SELECT session_uri, start_time, provider FROM sessions_v2"
            ).fetchall()
            chats = []
            if include_chats:
                chat_columns = {row[1] for row in connection.execute("PRAGMA table_info(chats_v2)")}
                required = (
                    "owner_session_uri", "chat_uri", "parent_chat", "origin_session_uri",
                    "inherited_turn_id", "storage_resource", "origin",
                )
                if set(required).issubset(chat_columns):
                    chats = connection.execute("SELECT " + ",".join(required) + " FROM chats_v2").fetchall()
        matches = {}
        for uri, timestamp, provider in rows:
            if not isinstance(uri, str) or not uri.startswith("copilotcli:/"):
                continue
            host = uri[len("copilotcli:/"):]
            try:
                if str(uuid.UUID(host)) != host:
                    continue
            except (ValueError, AttributeError):
                continue
            matches.setdefault(host, []).append((timestamp, provider))
        result = {}
        for host, values in matches.items():
            if len(values) != 1 or values[0][1] != "copilotcli":
                continue
            timestamp = values[0][0]
            if (type(timestamp) not in (int, float)
                    or not math.isfinite(timestamp) or timestamp < 0):
                continue
            seconds = timestamp / 1000
            if format_file_time(seconds) != "Unavailable":
                result[host] = seconds
        return (result, rows, chats) if include_chats else result
    except (OSError, sqlite3.Error, ValueError, OverflowError):
        return unavailable


def catalog_storage_key(resource):
    """Installed 1.146 SessionDataService._dataKey, restricted to proven local shapes."""
    if not isinstance(resource, str) or len(resource) > 4096:
        return None
    try:
        parsed = urlsplit(resource)
        if parsed.query or parsed.fragment or "%" in resource or "\\" in resource:
            return None
        if parsed.scheme == "copilotcli" and not parsed.netloc:
            host = parsed.path.removeprefix("/")
            return host if resource == "copilotcli:/" + host and str(uuid.UUID(host)) == host else None
        if parsed.scheme != "ahp-chat" or not re.fullmatch(r"[A-Za-z0-9_.-]+", parsed.netloc):
            return None
        encoded = parsed.path.removeprefix("/")
        if (resource != f"ahp-chat://{parsed.netloc}/{encoded}"
                or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded)):
            return None
        base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True).decode("utf-8")
        key = re.sub(r"[^a-zA-Z0-9_.-]", "-", parsed.netloc + "-" + encoded)
        return key if key not in (".", "..") else None
    except (ValueError, UnicodeError):
        return None


def catalog_associations(session_rows, chat_rows):
    """Resolve recorded resources to storage, never infer ownership from directory names."""
    owners = {}
    for uri, _, provider in session_rows:
        owners.setdefault(uri, []).append(provider)
    owners = {uri for uri, rows in owners.items()
              if rows == ["copilotcli"] and catalog_storage_key(uri) is not None}
    by_chat, by_storage = {}, {}
    for row in chat_rows:
        by_chat.setdefault(row[1], []).append(row)
        key = catalog_storage_key(row[5])
        if key is not None:
            by_storage.setdefault(key.casefold(), []).append(row)
    result = {
        catalog_storage_key(owner).casefold(): {
            "kind": "Session", "parent": "None (session)", "parent_id": None,
            "owner_uri": owner,
        } for owner in owners
    }
    for key, rows in by_storage.items():
        if len(rows) != 1:
            result[key] = {}
            continue
        owner, chat, parent, origin_owner, inherited, resource, raw_origin = rows[0]
        if owner not in owners or len(by_chat.get(chat, [])) != 1:
            result[key] = {}
            continue
        try:
            parsed = urlsplit(chat)
            encoded = parsed.path.removeprefix("/")
            decoded = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            ).decode("utf-8")
            if catalog_storage_key(chat) is None or parsed.scheme != "ahp-chat" or decoded != owner:
                raise ValueError
            is_default = parsed.netloc == "default"
            if resource != chat and not (is_default and resource == owner):
                raise ValueError
            if origin_owner not in (None, owner):
                raise ValueError
            if raw_origin and (not isinstance(raw_origin, str) or len(raw_origin) > 8192):
                raise ValueError
            origin = json.loads(raw_origin) if raw_origin else None
            if origin is not None and not isinstance(origin, dict):
                raise ValueError
            if parent is not None:
                parent_rows = by_chat.get(parent, [])
                if len(parent_rows) != 1 or parent_rows[0][0] != owner:
                    raise ValueError
            kind = "Session" if is_default else "Child chat (origin unknown)"
            parent_id = None
            if origin and origin.get("kind") in ("fork", "tool"):
                source = by_chat.get(origin.get("chat"), [])
                if (len(source) != 1 or source[0][0] != owner or origin_owner != owner
                        or parent not in (None, origin.get("chat")) or origin.get("chat") == chat):
                    raise ValueError
                source_chat, source_resource = source[0][1], source[0][5]
                source_uri = urlsplit(source_chat)
                source_encoded = source_uri.path.removeprefix("/")
                if (catalog_storage_key(source_chat) is None or source_uri.scheme != "ahp-chat"
                        or base64.b64decode(
                            source_encoded + "=" * (-len(source_encoded) % 4),
                            altchars=b"-_", validate=True,
                        ).decode("utf-8") != owner
                        or (source_resource != source_chat
                            and not (source_uri.netloc == "default" and source_resource == owner))):
                    raise ValueError
                source_key = catalog_storage_key(source[0][5])
                if source_key is None or len(by_storage.get(source_key.casefold(), [])) != 1:
                    raise ValueError
                parent_id = source_key
                if origin["kind"] == "fork":
                    if not isinstance(inherited, str) or not inherited or origin.get("turnId") != inherited:
                        raise ValueError
                    kind = "Fork"
                else:
                    kind = "Child chat (tool)"
            elif origin and origin.get("kind") not in ("user", None):
                raise ValueError
            result[key] = {
                "kind": kind, "parent_id": parent_id,
                "parent": parent_id or ("None (session)" if is_default else "Unavailable (source unknown)"),
                "owner_uri": owner,
                "chat_uri": chat, "storage_resource": resource,
            }
        except (ValueError, TypeError, UnicodeError, RecursionError):
            result[key] = {}
    return result


def validate_read_source(path: Path, user_data: Path) -> Path:
    sessions = user_data.resolve(strict=True) / "agentSessionData"
    candidate = path.resolve(strict=True)
    if (path.is_symlink() or path.parent.is_symlink() or sessions.is_symlink()
            or candidate.name != "session.db" or candidate.parent.parent != sessions):
        raise Refused("Select a direct host session.db under this user-data directory.")
    return candidate


def inspect_session(path: Path, user_data: Path, *, strict=True):
    item = {
        "database": str(path), "id": path.parent.name, "title": path.parent.name,
        "status": "Unreadable database", "evidence": "", "edit_count": None,
        "file_count": None, "can_prepare": False, "project": "Unknown project",
        "folders": [], "identity_warning": False, "modified": None, "created": None,
        "inspection_complete": strict, "kind": "Unknown", "parent": "Unavailable",
    }
    try:
        path = validate_read_source(path, user_data)
        item["modified"] = path.stat().st_mtime
        with read_db(path, timeout=2 if strict else 0.1) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            item["schema_version"] = version
            if not tables(connection):
                item["status"] = "Empty or unrecognized database"
                item["evidence"] = "No application tables are present."
                return item
            try:
                meta = identity_metadata(connection)
            except Refused as error:
                meta = {}
                item["identity_warning"] = str(error)
            title = meta.get("customTitle") or meta.get("title") or path.parent.name
            item["title"] = title[:200] if isinstance(title, str) else path.parent.name
            item["project"], item["folders"], invalid = project_identity(meta)
            item["identity_warning"] = item["identity_warning"] or invalid
            if {"file_path"}.issubset({r[1] for r in connection.execute("PRAGMA table_info(file_edits)")}):
                item["edit_count"] = connection.execute("SELECT COUNT(*) FROM file_edits").fetchone()[0]
            if not strict:
                item["status"] = "Not yet checked"
                item["evidence"] = (
                    "Read-only scan metadata only. Select this row for full repair-safety inspection; "
                    "unknown eligibility never permits preparation."
                )
                return item
            item["inspection_complete"] = True
            try:
                check_supported(connection)
                validate_source(path, user_data)
            except (Refused, TypeError, ValueError, sqlite3.Error) as error:
                item["status"] = (
                    f"Read-only: {'schema' if version in (15, 16, 18) else 'unrecognized schema'} {version} (repair supports 20)"
                    if version != 20 else "Read-only: repair safety check refused"
                )
                item["evidence"] = str(error)
                return item
            rows = connection.execute("SELECT file_path FROM file_edits").fetchall()
            if any(not isinstance(row[0], str) for row in rows):
                item["status"] = "Read-only: repair safety check refused"
                item["evidence"] = "An edit file path is not text."
                return item
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
    except (OSError, sqlite3.Error, TypeError, ValueError, Refused) as error:
        item["evidence"] = str(error)
    return item


def stream_scan(user_data: Path, *, cancel=None, strict=False):
    """Yield (item, completed, total); cancellation never writes source data."""
    sessions = user_data / "agentSessionData"
    if not sessions.is_dir():
        raise Refused(f"No host conversations directory: {sessions}")
    catalog, session_rows, chat_rows = catalog_creation_times(user_data, include_chats=True)
    associations = catalog_associations(session_rows, chat_rows)
    paths = []
    with os.scandir(sessions) as entries:
        for entry in entries:
            if cancel is not None and cancel.is_set():
                return
            if entry.is_dir(follow_symlinks=False):
                path = Path(entry.path) / "session.db"
                if path.is_file():
                    paths.append(path)
    for completed, path in enumerate(sorted(paths), 1):
        if cancel is not None and cancel.is_set():
            return
        item = inspect_session(path, user_data, strict=strict)
        item["created"] = catalog.get(path.parent.name) if catalog is not None else None
        item["catalog_available"] = catalog is not None
        item.update(associations.get(path.parent.name.casefold(), {}))
        yield item, completed, len(paths)


def scan(user_data: Path):
    """Retain the complete strict inspection contract for CLI/API callers."""
    return [item for item, _, _ in stream_scan(user_data, strict=True)]


def conversation_preview(database: Path, user_data: Path):
    """Read only bounded, independently attributable local-turn payloads on demand."""
    unavailable = (
        "Conversation preview unavailable: no verified, unambiguous selected-chat mapping. "
        "No private message text was read."
    )
    try:
        database = validate_read_source(database, user_data)
        _, sessions, chats = catalog_creation_times(user_data, include_chats=True)
        association = catalog_associations(sessions, chats).get(database.parent.name.casefold(), {})
        chat = association.get("chat_uri")
        if not chat or not association.get("storage_resource"):
            return unavailable
        with read_db(database, timeout=0.1) as connection:
            deadline = time.monotonic() + 1
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            if connection.execute("PRAGMA user_version").fetchone()[0] != 20:
                return "Conversation preview unavailable: only the verified schema-20 local-turn layout is supported."
            meta = identity_metadata(connection)
            _, folders, invalid = project_identity(meta)
            if invalid or len(folders) > 1:
                return "Conversation preview unavailable: ambiguous or multi-root folder identity requires separate investigation."
            owners = connection.execute(
                "SELECT key,value FROM session_metadata WHERE key IN (?,?) AND turn_id IS NULL",
                ("agentHost.rowOwnerChat", "agentHost.foreignTurnIds"),
            ).fetchall()
            seen = set()
            for key, value in owners:
                if key in seen:
                    return unavailable
                seen.add(key)
                if key == "agentHost.rowOwnerChat" and value != chat:
                    return unavailable
                if key == "agentHost.foreignTurnIds" and json.loads(value) != []:
                    return unavailable
            columns = {row[1] for row in connection.execute("PRAGMA table_info(local_turns)")}
            if not {"turn_id", "chat_uri", "seq", "payload", "kind"}.issubset(columns):
                return "Conversation preview unavailable: this database has no supported partial local-turn layout."
            rows = connection.execute(
                "SELECT turn_id, CASE WHEN length(CAST(payload AS BLOB)) <= 65536 "
                "THEN payload END FROM local_turns "
                "WHERE chat_uri=? AND kind='local' ORDER BY seq,turn_id LIMIT 101",
                (chat,),
            ).fetchall()
        header = (
            "PARTIAL LOCAL TURNS — may omit inherited history.\n"
            "Only recognized user text and markdown from local_turns for the exact selected chat "
            "are shown. Tools, attachments, inherited turns and assistant_response_parts are not read.\n\n"
        )
        if not rows:
            return header + (
                "No independently attributable messages are available in this chat's local turns. "
                "Inherited/history content is not shown. Open the selected chat in VS Code to view its history."
            )
        output, skipped, size = [], 0, 0
        for index, (turn_id, raw) in enumerate(rows[:100]):
            try:
                payload = json.loads(raw)
                message = payload["message"]
                parts = payload["responseParts"]
                if (not isinstance(payload, dict) or payload.get("id") != turn_id
                        or not isinstance(message, dict)
                        or message.get("origin") != {"kind": "user"}
                        or not isinstance(message.get("text"), str)
                        or not isinstance(parts, list)
                        or payload.get("state") not in ("complete", "cancelled", "error")):
                    raise ValueError
                text = "User:\n" + message["text"][:16000]
                markdown = [part["content"][:16000] for part in parts
                            if isinstance(part, dict) and part.get("kind") == "markdown"
                            and isinstance(part.get("content"), str)]
                if markdown:
                    text += "\n\nAssistant (local markdown only):\n" + "\n".join(markdown)
                if size + len(text) > 200000:
                    skipped += min(len(rows), 100) - index
                    break
                size += len(text)
                output.append(text)
            except (ValueError, TypeError, KeyError, RecursionError):
                skipped += 1
        note = f"\n\n{skipped} unsupported/oversized local turns omitted." if skipped else ""
        if len(rows) > 100:
            note += "\nPreview limited to the first 100 local turns."
        return header + ("\n\n---\n\n".join(output) or "No recognized, independently attributable local messages to display.") + note
    except (OSError, sqlite3.Error, TypeError, ValueError, Refused, RecursionError):
        return "Conversation preview unavailable: selected database/catalog is unreadable, locked or unsupported. No unverified history is shown."


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
