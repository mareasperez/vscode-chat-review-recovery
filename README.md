# VS Code Chat Recovery

A small, local Windows desktop tool for inspecting suspected stale chat reviews and explicitly resetting one conversation's review history. No server, web UI, telemetry, administrator access, or runtime Python installation is needed for the packaged executable.

**Unsupported recovery experiment, not a universal fix.** Pending edits are normal. A path-case conflict is an indication, not proof of damage. The tool cannot detect the visible spinner.

## Run

Open `dist\VSCodeChatRecovery.exe`, or run `python -B app.py` from a source checkout.

1. Select the VS Code user-data folder (defaults to the current user's `%APPDATA%\Code`). For Insiders or custom profiles, choose the actual directory containing `agentSessionData`.
2. Scan read-only and select a conversation. Check its title, exact host session ID, evidence, and file paths.
3. If you have observed the matching stuck Keep/editing symptom, prepare a preview and verified backup.
4. Close VS Code normally while keeping this independent app open. Click **Check host / mode** to enable offline operations. Acknowledge loss of **all inline review/Undo history for this conversation** and explicitly confirm Repair.
5. Reopen the same chat and verify both its history and the original UI symptom. A successful database operation alone does not establish UI recovery.

Saved project files are never edited. Conversation messages and unrelated metadata are checked for preservation. Nothing is repaired automatically or in bulk. If another user's Code process prevents repair, stop; do not kill it.

### Read-only mode

The app starts in **READ-ONLY** and checks for VS Code processes. While Code is open or its status cannot be verified, Repair and Restore remain disabled. Scanning, inspection and preparing a backup remain available. After closing Code, click **Check host / mode**. The engine independently rechecks immediately before writing, so reopening Code after that check still prevents repair.

## Backups and restore

Private local incident files live under `%LOCALAPPDATA%\VSCodeChatRecovery\incidents`. Preparation makes a consistent SQLite backup including committed WAL data. Repair makes another verified backup and pins its hash before its transaction.

Keep the whole incident directory, including `incident.json`. Do not upload chat backups. Restore requires choosing that exact incident, closing VS Code, and confirming. It restores only the original review rows and summary, retaining newer messages. New or changed reviews cause refusal.

Never replace the live database or delete its `-wal` or `-shm` files.

## Supported scope

- Windows, host database schema version **20** with the recognized table layout.
- Ordinary existing local-file create/edit histories for one independent host conversation.
- No shared/peer ownership, reviewed-file entries, renames/deletes/accept markers, missing snapshots/files, linked worktrees or unsupported schemas.
- Every stored edit record for the selected conversation is included in the preview; there is no heuristic deletion of "damaged" rows.
- Any changed selection, summary, schema, affected files, or protected data stops the operation. Rescan and review a new preview rather than bypassing checks.
- File bytes and discovered Git index bytes are checked during the operation; no Git commands or index writes are performed.
- Windows may warn about the unsigned executable. No code-signing certificate is included.

The app and its source directory can be moved without changing runtime data locations.

## Develop and build

Python 3.13 with Tkinter is used for development; the application itself uses only the standard library.

```powershell
python -B -m unittest discover -s tests -v
python -B app.py --scan --json
```

Only if the packager is missing, install build dependencies in a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r build-requirements.txt
```

Build and test:

```powershell
.\build.ps1
.\dist\VSCodeChatRecovery.exe --self-test --report "$env:TEMP\chat-recovery-self-test.json"
```

The packaged self-test checks Tk/Tcl and SQLite without opening real chat databases. Use `--scan --report <path>` for read-only diagnostics from the windowed executable. Build output and private incident files must not be committed.
