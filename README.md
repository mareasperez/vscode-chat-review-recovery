# VS Code Chat Recovery

A small, local Windows desktop tool for inspecting suspected stale chat reviews and explicitly resetting one conversation's review history. No server, web UI, telemetry, administrator access, or runtime Python installation is needed for the packaged executable.

**Unsupported recovery experiment, not a universal fix.** Pending edits are normal. A path-case conflict is an indication, not proof of damage. The tool cannot detect the visible spinner.

## Run

Open `dist\VSCodeChatRecovery.exe`, or run `python -B app.py` from a source checkout.

### Executable names

The current Qt build is `dist\VSCodeChatRecovery.exe`. The other names in `dist` are
historical or intermediate build candidates:

- `VSCodeChatRecovery-Modern.exe` — older Qt redesign candidate.
- `VSCodeChatRecovery-UnifiedQt.exe` — older combined Qt candidate.
- `VSCodeChatRecovery-Status.exe` — older status-indicator candidate.
- `VSCodeChatRecovery-Family.exe` — older family-navigation candidate.
- `VSCodeChatRecovery-Hierarchy.exe` — older hierarchy candidate.
- `VSCodeChatRecovery-StandaloneFork.exe` — older fork-specific candidate.

Do not assume that an intermediate name is the latest build. Prefer the executable
with the canonical name, rebuilt by `build.ps1`.

The desktop interface uses a permanent dark sidebar with **Recovery**, **Settings**, and **About** pages. The disabled **History** navigation item is intentionally not presented as implemented: verified per-conversation history is available inside the selected conversation's diagnosis panel. Host closure and conversation eligibility remain separate signals; **VS Code Closed** never makes an unsupported conversation repairable.

1. Select the VS Code user-data folder (defaults to the current user's `%APPDATA%\Code`). For Insiders or custom profiles, choose the actual directory containing `agentSessionData`.
2. Scan read-only. Results arrive progressively; search, filter, sort and select already displayed rows while scanning. **Cancel scan** keeps the results already received. Select a conversation for an automatic, background safety inspection before proceeding.
3. If you have observed the matching stuck Keep/editing symptom, prepare a preview and verified backup.
4. Close VS Code normally while keeping this independent app open. Click **Recheck VS Code is closed** and wait for **OFFLINE VERIFIED**. This verifies host closure only, not conversation eligibility. For an eligible, prepared conversation, acknowledge loss of **all inline review/Undo history for this conversation** and explicitly confirm Repair.
5. Reopen the same chat and verify both its history and the original UI symptom. A successful database operation alone does not establish UI recovery.

Saved project files are never edited. Conversation messages and unrelated metadata are checked for preservation. Nothing is repaired automatically or in bulk. If another user's Code process prevents repair, stop; do not kill it.

### Read-only mode

The app automatically checks for VS Code processes in the background after startup. The persistent **Host** line shows **CHECKING** (writes disabled), **READ-ONLY** with the refusal or verification-failure reason, or **OFFLINE VERIFIED**. While Code is open or its status cannot be verified, Repair and Restore remain disabled. Scanning, inspection and eligible backup preparation remain available after the check. After closing Code, click **Recheck VS Code is closed**. Rechecking immediately invalidates any previous offline result and disables writes while the bounded process check runs (10-second subprocess timeout). Failed checks stay read-only. The engine independently rechecks immediately before writing, so reopening Code after that check still prevents repair.

The separate, persistent **Selected conversation** line explains repair eligibility and the next preparation/confirmation step, independently of host status. No selection and **NOT YET CHECKED** rows never authorize repair. Unsupported selections show their safety-inspection reason. **OFFLINE VERIFIED** does not enable unsupported forks, child chats, parent sessions with peers, or schemas; it is not permission to repair every row. Scan progress and conversation previews do not replace these two status lines. Eligibility is not a diagnosis of damage, and preparing a preview never performs repair.

Identity and stored edit counts are best-effort, read-only observations, including recognized older metadata layouts (schemas 15, 16 and 18). Other schemas may expose the same metadata but are not recognized for repair. A **Read-only** schema or safety refusal does not mean the database is broken; the selected row shows the precise reason. An unreadable or empty database is labeled separately. An edit count for an unsupported schema is only a count of stored rows, not proof of pending review or damage.

The desktop scan initially reads lightweight identity and counts, not full repair eligibility or path-conflict evidence. **Not yet checked** is never permission to prepare or repair. Selecting a row runs the full safety checks for that exact database. Preparation remains disabled until scanning has ended and the selected inspection passes; preparation and every mutation independently recheck the existing safeguards. Locked databases remain visible, with a bounded read timeout. Changing the user-data folder cancels the previous scan and discards late results; no scan writes to a conversation database. The CLI and `recovery.scan()` retain complete, strict inspection for compatibility.

Projects and folders come only from session metadata, not edit paths. Unknown, malformed, remote and multiple folder values are shown conservatively; the detail panel lists all recognized local folders. Some older conversations have no title or project metadata and can still appear as host IDs.

**Chat type** and **Parent chat (storage ID)** distinguish catalog-verified sessions from forks and other child chats. The tool resolves an exact `chats_v2.storage_resource` through the verified VS Code 1.146 shaped-URI storage naming rules; it does not decode a directory name to guess ownership. A **Fork** additionally requires a consistent fork origin referencing another unique chat owned by the same catalog session. The parent column shows that exact source chat's storage identity, which can also be searched—not necessarily the root session identity. A fork of a fork therefore identifies the source fork as its parent; a verified tool child identifies its source chat. A child with no fork origin is not called a fork, and an unverified source is shown as unavailable rather than replaced with its owning session. Missing, duplicate, foreign, malformed, unsupported/opaque or colliding mappings stay **Unknown / Unavailable**. This read-only identification does not authorize repair: parent sessions with peers and non-UUID child storage remain blocked.

Use the **Chat type** filter to select a verified type or **Unknown**. Select a verified conversation and click **View selected family** to show that chat, its direct parent, direct children, and siblings with the same verified immediate parent. A root shows only itself and its direct children, not all descendants. Unknown parentage never groups unrelated chats; titles and project folders never establish family. If the verified parent is absent from scan results, its available children/siblings can still be shown and the missing parent is noted. Only exact, unique storage IDs and verified parent IDs establish membership.

The family stays anchored to the originally selected chat when you select another row; click **View selected family** again to change it. Search, project, type and sort controls still apply, so they may hide some family members. **Clear family filter** removes only the family restriction. Streaming adds matching relatives as they arrive, and cancelling a scan keeps the partial family. A new scan clears the family restriction; changing user-data folders also resets the type filter and discards old results. Filter changes invalidate prepared incidents and stale selection requests; these display controls never enable repair or read messages.

**Created (VS Code catalog)** comes from the selected user-data directory's local `User\globalStorage\agent-host.db` catalog. It is shown in local time only when a canonical host UUID matches exactly one `copilotcli:/<uuid>` catalog row with a valid creation timestamp. Matches are sparse; missing, ambiguous, malformed or unsupported catalog data is **Unavailable**, never inferred from file times. Catalog failure does not stop session inspection.

The default sort is creation time, newest first; creation and file-modified sorts offer both directions, with unavailable values last. Title sorting is also available. **File modified** is not chat activity and may exclude WAL activity. Last-message sorting is unavailable because no complete, verified message timestamps have been established; selecting it retains the previous sort and explains why.

### Partial conversation preview and privacy

**Show conversation preview** performs a fresh, read-only catalog verification for the exact selected database. When ownership is unambiguous, it reads only `local_turns WHERE chat_uri=? AND kind='local'`, in stored sequence order. The preview accepts recognized user messages and local markdown payloads, caps individual payloads and total displayed text, and omits tools and attachments. Unknown payloads, foreign ownership markers, ambiguous/multiple folders or unsupported schemas fail closed.

This is **partial local history**, not a transcript. It may omit inherited history. A fork can contain many inherited response records but zero independently attributable local turns; in that case the preview explicitly explains that no independently attributable messages are available and directs you to open the chat in VS Code. It never falls back to unfiltered `turns` or `assistant_response_parts`.

No message content is loaded during scans, written to logs/incidents/exports, or sent to a network service. Preview text exists only on demand in GUI memory. Selection and user-data generation checks discard stale inspection/preview results rather than displaying them for a different row. Closing the window cancels read-only scanning; in-progress repair/restore operations must still finish before closing.

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

### Automatic GitHub releases

For manual tags, open **Actions → Create tag (admin only) → Run workflow**, choose the source branch, and enter a new tag such as `v0.2.0`. The workflow tags the selected run's exact commit, then calls the Windows release workflow directly (tags created by `GITHUB_TOKEN` do not trigger tag-push workflows). A read-only authorization job checks both the original actor and rerun actor: each must be the repository owner or have `admin` permission returned by GitHub's repository permissions API. Read, triage, write, maintain, unknown permissions, and API errors fail closed before any tag-creation or release job. This also supports administrators of organization-owned repositories.

The workflow must exist on the default branch to appear in the Actions UI. Invalid or existing tags fail without moving any tag. A successful tag is retained if the later build or release fails; it is never deleted automatically. Public visibility alone does not authorize execution or tag creation. These workflow checks do not replace repository tag rulesets or prevent collaborators with separate Git write permissions from pushing tags outside this workflow.

Pushing any tag triggers `.github/workflows/release.yml`. A Windows runner installs the pinned dependencies, runs the tests, builds the executable, and checks its packaged `--self-test`. Only after these gates pass does a separate job publish a GitHub release with `VSCodeChatRecovery.exe`, `SHA256SUMS.txt`, and generated release notes. Publication uses the built-in `GITHUB_TOKEN` with job-scoped `contents: write`; no personal token is required.

Include the workflow and all source/dependency files in the tagged commit before pushing a tag (for example `v0.2.0`). Local-only tags do not trigger Actions. Existing releases are not overwritten, and failed builds do not publish releases. The executable remains unsigned. The local build script never creates tags or releases.

### Local development

Python 3.13 and PySide6 are used for development. `app.py` is the canonical Qt application and packaged entrypoint; `qt_app.py` remains a compatibility launcher that re-exports it.

```powershell
python -B -m unittest discover -s tests -v
python -m pip install -r requirements.txt
python -B app.py --scan --json
```

Only if the packager is missing, install build dependencies in a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r build-requirements.txt
```

Build and test:

The build temporarily restricts DLL discovery through `PATH` to Python and Windows system directories, then restores the original value. This prevents unrelated developer tools from bundling incompatible ICU libraries with Qt. The default UI font uses points so widgets inheriting it have a valid point size.

```powershell
.\build.ps1
.\dist\VSCodeChatRecovery.exe --self-test --report "$env:TEMP\chat-recovery-self-test.json"
```

The packaged self-test checks Qt and SQLite without opening real chat databases. Use `--scan --report <path>` for read-only diagnostics from the windowed executable. Build output and private incident files must not be committed. Pass `-OutputName <unique-name>` to `build.ps1` when an older executable may still be running; the script never needs to terminate it or overwrite it by force.
