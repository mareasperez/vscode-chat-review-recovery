"""Native, serverless desktop UI for conservative VS Code chat recovery."""

from __future__ import annotations

import argparse
import json
import os
import queue
import sqlite3
import sys
import tempfile
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import recovery


class Application:
    def __init__(self, root: tk.Tk, user_data: Path | None = None):
        self.root = root
        self.root.title("VS Code Chat Recovery")
        self.root.geometry("1060x740")
        self.root.minsize(800, 580)
        self.user_data = tk.StringVar(value=str(user_data or recovery.default_user_data()))
        self.status = tk.StringVar(value="Scan to inspect conversations. Pending reviews are not proof of damage.")
        self.acknowledged = tk.BooleanVar(value=False)
        self.incident: Path | None = None
        self.items = {}
        self.busy = False
        self.read_only = True
        self.mode = tk.StringVar(value="READ-ONLY: host state has not been checked.")
        self.messages = queue.Queue()
        top = ttk.Frame(root, padding=12)
        top.pack(fill="both", expand=True)
        ttk.Label(top, text="VS Code Chat Recovery", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            top, text="Unsupported recovery experiment - schema 20 only. No project edits, server, or telemetry.",
            wraplength=950,
        ).pack(anchor="w", pady=(4, 10))
        location = ttk.Frame(top)
        location.pack(fill="x")
        self.location_entry = ttk.Entry(location, textvariable=self.user_data)
        self.location_entry.pack(side="left", fill="x", expand=True)
        self.browse_button = ttk.Button(location, text="User-data folder...", command=self.browse)
        self.browse_button.pack(side="left", padx=8)
        self.scan_button = ttk.Button(location, text="Scan (read-only)", command=self.scan)
        self.scan_button.pack(side="left")
        mode_bar = ttk.Frame(top)
        mode_bar.pack(fill="x", pady=(8, 0))
        ttk.Label(mode_bar, textvariable=self.mode, wraplength=800).pack(side="left", fill="x", expand=True)
        self.mode_button = ttk.Button(mode_bar, text="Check host / mode", command=self.check_mode)
        self.mode_button.pack(side="right")
        tree_frame = ttk.Frame(top)
        tree_frame.pack(fill="both", expand=True, pady=10)
        self.tree = ttk.Treeview(
            tree_frame, columns=("title", "state", "edits", "id"), show="headings", selectmode="browse", height=10
        )
        for column, heading, width in (
            ("title", "Conversation", 260), ("state", "Evidence / status", 340),
            ("edits", "Edits", 65), ("id", "Host session", 240),
        ):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, minwidth=50)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.selected)
        self.details = tk.Text(top, height=10, wrap="word", state="disabled", font=("Consolas", 10))
        self.details.pack(fill="both")
        self.confirmation = ttk.Checkbutton(
            top, variable=self.acknowledged,
            text="I observed the stuck Keep/editing symptom. I accept losing ALL inline review/Undo history for this conversation.",
            command=self.update_buttons,
        )
        self.confirmation.pack(anchor="w", pady=8)
        actions = ttk.Frame(top)
        actions.pack(fill="x")
        self.prepare_button = ttk.Button(actions, text="1. Prepare preview + backup", command=self.prepare)
        self.prepare_button.pack(side="left")
        self.repair_button = ttk.Button(actions, text="2. Repair selected conversation", command=self.repair)
        self.repair_button.pack(side="left", padx=8)
        self.restore_button = ttk.Button(actions, text="Restore from incident...", command=self.restore)
        self.restore_button.pack(side="left")
        ttk.Label(top, textvariable=self.status, wraplength=980).pack(anchor="w", pady=(12, 0))
        ttk.Label(
            top, text="Scan with VS Code open; close it normally before repair/restore. Backups contain private chat data: keep them local.",
            wraplength=980,
        ).pack(anchor="w", pady=(8, 0))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.update_buttons()
        self.root.after(100, self.poll)

    def show(self, text: str):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def update_buttons(self):
        selection = self.tree.selection()
        can_prepare = bool(selection and self.items.get(selection[0], {}).get("can_prepare"))
        self.prepare_button.configure(state="normal" if can_prepare and not self.busy else "disabled")
        self.repair_button.configure(
            state="normal" if self.incident and self.acknowledged.get() and not self.busy and not self.read_only else "disabled"
        )
        self.restore_button.configure(state="normal" if not self.busy and not self.read_only else "disabled")
        for widget in (self.scan_button, self.browse_button, self.location_entry, self.confirmation, self.mode_button):
            widget.configure(state="disabled" if self.busy else "normal")

    def check_mode(self):
        def detect():
            try:
                recovery.ensure_offline()
            except recovery.Refused as error:
                return True, f"READ-ONLY: {error}"
            return False, "OFFLINE: repair can be explicitly confirmed. Host state is rechecked before every write."

        def detected(result):
            self.read_only, text = result
            self.mode.set(text)
            self.status.set(
                "Scanning, inspection and backup preparation are available. Close VS Code and check mode again to enable repair."
                if self.read_only else "Host closed. Review the selected incident before confirming any repair."
            )

        self.run(detect, detected)

    def selected(self, event=None):
        if self.busy:
            return
        self.incident = None
        self.acknowledged.set(False)
        selection = self.tree.selection()
        if selection:
            item = self.items[selection[0]]
            self.show(
                f"Conversation: {item['title']}\nHost ID: {item['id']}\n"
                f"Database: {item['database']}\nStatus: {item['status']}\n{item['evidence']}\n\n"
                f"ALL {item['edit_count']} edit records would be reset. Actual files stay unchanged.\n"
                + "\n".join(item.get("paths", []))
            )
        self.update_buttons()

    def browse(self):
        selected = filedialog.askdirectory(title="Select VS Code user-data folder (contains agentSessionData)")
        if selected:
            self.user_data.set(selected)
            self.incident = None
            self.update_buttons()

    def run(self, action, callback):
        if self.busy:
            return
        self.busy = True
        self.status.set("Working... No other operation can start until this finishes.")
        self.update_buttons()

        def worker():
            try:
                self.messages.put((callback, action(), None))
            except Exception:
                self.messages.put((None, None, traceback.format_exc()))

        threading.Thread(target=worker, daemon=False).start()

    def poll(self):
        try:
            callback, result, error = self.messages.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            if error:
                self.incident = None
                self.read_only = True
                self.mode.set("READ-ONLY: an operation failed or host state is uncertain. Check host / mode again.")
                self.status.set("Operation refused or failed. Review the error; do not bypass safety checks.")
                self.show(error)
                messagebox.showerror("Operation stopped", error, parent=self.root)
            else:
                callback(result)
            self.update_buttons()
        self.root.after(100, self.poll)

    def populate(self, rows):
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        self.incident = None
        self.acknowledged.set(False)
        for index, item in enumerate(rows):
            key = str(index)
            self.items[key] = item
            self.tree.insert("", "end", iid=key, values=(
                item["title"], item["status"], item["edit_count"], item["id"]
            ))
        self.status.set(f"{len(rows)} conversations inspected. Select one to inspect evidence; no repairs were applied.")
        self.show("A path-case conflict is only an indication. Normal pending reviews are not damaged conversations.")
        self.update_buttons()

    def scan(self):
        directory = Path(self.user_data.get())
        self.incident = None
        self.run(lambda: recovery.scan(directory), self.populate)

    def prepare(self):
        selection = self.tree.selection()
        if not selection:
            return
        database = Path(self.items[selection[0]]["database"])
        directory = Path(self.user_data.get())

        def prepared(path):
            self.incident = path
            self.acknowledged.set(False)
            plan, _ = recovery.load_plan(path)
            self.show(
                f"PREPARED - NOT REPAIRED\nHost: {plan['host_id']}\n"
                f"Database: {plan['database']}\nRecords to remove: {len(plan['edit_keys'])}\n"
                f"Incident: {path}\nVerified backup: {path.parent / plan['prepared_backup']}\n\n"
                "ALL stored inline edit reviews for this conversation will be cleared.\n"
                "Messages and actual files will not be edited. A fresh backup is also made before repair.\n\n"
                + "\n".join(plan["files"])
            )
            self.status.set("Preview ready. Close VS Code normally; acknowledge the scope, then confirm Repair.")

        self.run(
            lambda: recovery.prepare(database, directory, recovery.default_incidents()), prepared
        )

    def repair(self):
        if self.busy or self.read_only or not self.incident or not self.acknowledged.get():
            return
        incident = self.incident
        try:
            plan, _ = recovery.load_plan(incident)
        except (OSError, sqlite3.Error, ValueError, KeyError, recovery.Refused) as error:
            messagebox.showerror("Invalid incident", str(error), parent=self.root)
            return
        if not messagebox.askyesno(
            "Confirm offline repair",
            f"Reset ALL {len(plan['edit_keys'])} inline edit reviews for host\n{plan['host_id']}?\n\n"
            "Saved file contents and messages stay unchanged. Inline diff/Undo history is lost.\n"
            "All VS Code processes must be closed. This may not fix the visible symptom.",
            default="no", parent=self.root,
        ):
            return
        self.run(lambda: recovery.operate(incident), self.finished)

    def restore(self):
        if self.busy or self.read_only:
            return
        name = filedialog.askopenfilename(
            title="Select exact incident.json to restore (not a database)",
            initialdir=str(recovery.default_incidents()),
            filetypes=[("Recovery incident", "incident.json")],
        )
        if not name:
            return
        incident = Path(name)
        try:
            plan, _ = recovery.load_plan(incident)
        except (OSError, sqlite3.Error, ValueError, KeyError, recovery.Refused) as error:
            messagebox.showerror("Invalid incident", str(error), parent=self.root)
            return
        if not messagebox.askyesno(
            "Confirm scoped restore",
            f"Restore {len(plan['edit_keys'])} review records for host {plan['host_id']}?\n\n"
            "Newer messages are retained. Changed review state causes refusal.\n"
            "Close VS Code normally before continuing.",
            default="no", parent=self.root,
        ):
            return
        self.run(lambda: recovery.operate(incident, restoring=True), self.finished)

    def finished(self, result):
        self.incident = None
        self.acknowledged.set(False)
        self.show(json.dumps(result, indent=2))
        self.status.set(f"{result['status']}. Keep the incident and backup. Reopen this chat and verify the UI and history.")
        messagebox.showinfo("Operation complete", json.dumps(result, indent=2), parent=self.root)

    def close(self):
        if self.busy:
            messagebox.showwarning("Operation in progress", "Wait for the current operation before closing.", parent=self.root)
            return
        self.root.destroy()


def self_test(report: Path) -> int:
    result = {}
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        app = Application(root)
        app.populate([])
        root.update()
        assert str(app.repair_button["state"]) == "disabled"
        assert str(app.prepare_button["state"]) == "disabled"
        with tempfile.TemporaryDirectory() as temporary:
            connection = sqlite3.connect(Path(temporary) / "smoke.db")
            try:
                assert connection.execute("SELECT 1").fetchone() == (1,)
            finally:
                connection.close()
        result = {"status": "ok", "tk": tk.TkVersion, "sqlite": sqlite3.sqlite_version, "frozen": bool(getattr(sys, "frozen", False))}
        code = 0
    except Exception:
        result = {"status": "failed", "error": traceback.format_exc()}
        code = 1
    finally:
        if root is not None:
            root.destroy()
    recovery.atomic_json(report, result)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="Read-only diagnostics, without opening the GUI.")
    parser.add_argument("--json", action="store_true", help="Print scan results as JSON.")
    parser.add_argument("--user-data-dir", type=Path, default=recovery.default_user_data())
    parser.add_argument("--self-test", action="store_true", help="Test bundled Tk/SQLite without opening real conversations.")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.self_test:
        if not args.report:
            parser.error("--self-test requires --report")
        return self_test(args.report)
    if args.scan:
        try:
            rows = recovery.scan(args.user_data_dir)
            text = json.dumps(rows, indent=2) if args.json else "\n".join(
                f"{row['id']}  {row['status']}  ({row['edit_count']} edits)" for row in rows
            )
            if sys.stdout is None:
                if not args.report:
                    raise recovery.Refused("Windowed executable diagnostics require --report.")
                recovery.atomic_json(args.report, rows)
            else:
                print(text)
            return 0
        except (OSError, sqlite3.Error, ValueError, recovery.Refused) as error:
            if sys.stderr is not None:
                print(f"Scan failed: {error}", file=sys.stderr)
            else:
                messagebox.showerror("Scan failed", str(error))
            return 1
    root = tk.Tk()
    application = Application(root, args.user_data_dir)
    root.after(100, application.check_mode)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
