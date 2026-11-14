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

SORT_CREATED_NEW = "Created: newest first"
SORT_CREATED_OLD = "Created: oldest first"
SORT_MODIFIED_NEW = "File modified: newest first"
SORT_MODIFIED_OLD = "File modified: oldest first"
SORT_TITLE = "Title: A-Z"
SORT_MESSAGE = "Last message (unavailable)"
ALL_CHAT_TYPES = "All chat types"
CHAT_TYPES = ("Session", "Fork", "Child chat (tool)", "Child chat (origin unknown)", "Unknown")


class ToolTip:
    def __init__(self, widget, text: str, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.job = None
        self.window = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, event=None):
        self.hide()
        self.job = self.widget.after(self.delay, self.show)

    def show(self):
        self.job = None
        if self.window or not self.widget.winfo_exists():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{self.widget.winfo_rootx() + 16}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 8}")
        ttk.Label(
            self.window, text=self.text, justify="left", wraplength=360,
            padding=(9, 6), relief="solid", borderwidth=1,
        ).pack()

    def hide(self, event=None):
        if self.job is not None:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


class Application:
    def __init__(self, root: tk.Tk, user_data: Path | None = None):
        self.root = root
        self.root.title("VS Code Chat Recovery")
        self.root.geometry("1180x840")
        self.root.minsize(920, 680)
        self.user_data = tk.StringVar(value=str(user_data or recovery.default_user_data()))
        self.status = tk.StringVar(value="Scan to inspect conversations. Pending reviews are not proof of damage.")
        self.acknowledged = tk.BooleanVar(value=False)
        self.incident: Path | None = None
        self.items = {}
        self.rendered_values = {}
        self.render_generation = 0
        self.scan_generation = 0
        self.selection_generation = 0
        self.scan_cancel = threading.Event()
        self.scanning = False
        self.scan_progress = ""
        self.closed = False
        self.read_messages = queue.Queue()
        self.search_text = tk.StringVar()
        self.project_filter = tk.StringVar(value="All projects")
        self.type_filter = tk.StringVar(value=ALL_CHAT_TYPES)
        self.family_anchor = None
        self.family_note = tk.StringVar(value="No family filter.")
        self.sort_order = tk.StringVar(value=SORT_CREATED_NEW)
        self.active_sort = SORT_CREATED_NEW
        self.busy = False
        self.read_only = True
        self.mode = tk.StringVar(value="Host: CHECKING — writes disabled until closure is verified.")
        self.eligibility = tk.StringVar()
        self.selected_summary = tk.StringVar(value="Select a conversation to see its diagnosis.")
        self.selected_explanation = tk.StringVar(
            value="Scan is read-only. Repair stays disabled until the selected conversation passes every safety check."
        )
        self.tooltips = []
        self.messages = queue.Queue()
        style = ttk.Style(root)
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Summary.TLabel", font=("Segoe UI", 11, "bold"))

        top = ttk.Frame(root, padding=14)
        top.pack(fill="both", expand=True)
        header = ttk.Frame(top)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="VS Code Chat Recovery", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Inspect first. Repair only when the selected conversation has a proven, reversible scope.",
        ).pack(anchor="w", pady=(2, 0))

        self.status_frame = ttk.Frame(top)
        self.status_frame.pack(fill="x", pady=(0, 10))
        host_card = ttk.LabelFrame(
            self.status_frame, text="Host safety gate", padding=10, style="Section.TLabelframe"
        )
        host_card.pack(side="left", fill="both", expand=True, padx=(0, 5))
        ttk.Label(host_card, textvariable=self.mode, wraplength=470).pack(side="left", fill="x", expand=True)
        self.mode_button = ttk.Button(host_card, text="Recheck VS Code is closed", command=self.check_mode)
        self.mode_button.pack(side="right", padx=(8, 0))
        chat_card = ttk.LabelFrame(
            self.status_frame, text="Selected conversation", padding=10, style="Section.TLabelframe"
        )
        chat_card.pack(side="left", fill="both", expand=True, padx=(5, 0))
        ttk.Label(chat_card, textvariable=self.eligibility, wraplength=520).pack(fill="x", expand=True)

        self.source_frame = ttk.LabelFrame(
            top, text="1. Scan local VS Code data", padding=10, style="Section.TLabelframe"
        )
        self.source_frame.pack(fill="x", pady=(0, 10))
        location = ttk.Frame(self.source_frame)
        location.pack(fill="x")
        ttk.Label(location, text="User-data folder").pack(side="left")
        self.location_entry = ttk.Entry(location, textvariable=self.user_data)
        self.location_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.user_data.trace_add("write", self.location_changed)
        self.browse_button = ttk.Button(location, text="Choose...", command=self.browse)
        self.browse_button.pack(side="left")
        self.scan_button = ttk.Button(location, text="Scan read-only", command=self.scan)
        self.scan_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(location, text="Cancel", command=self.cancel_scan)
        self.cancel_button.pack(side="left", padx=(8, 0))

        self.conversation_frame = ttk.LabelFrame(
            top, text="2. Find a conversation", padding=10, style="Section.TLabelframe"
        )
        self.conversation_frame.pack(fill="both", expand=True, pady=(0, 10))
        filters = ttk.Frame(self.conversation_frame)
        filters.pack(fill="x")
        ttk.Label(filters, text="Search").pack(side="left")
        self.search_entry = ttk.Entry(filters, textvariable=self.search_text, width=24)
        self.search_entry.pack(side="left", padx=(6, 12))
        ttk.Label(filters, text="Project").pack(side="left")
        self.project_box = ttk.Combobox(filters, textvariable=self.project_filter, state="readonly", width=34)
        self.project_box.pack(side="left", padx=(6, 12), fill="x", expand=True)
        ttk.Label(filters, text="Type").pack(side="left")
        self.type_box = ttk.Combobox(
            filters, textvariable=self.type_filter, state="readonly", width=22,
            values=(ALL_CHAT_TYPES, *CHAT_TYPES),
        )
        self.type_box.pack(side="left", padx=(6, 12))
        ttk.Label(filters, text="Sort").pack(side="left")
        self.sort_box = ttk.Combobox(
            filters, textvariable=self.sort_order, state="readonly", width=24,
            values=(SORT_CREATED_NEW, SORT_CREATED_OLD, SORT_MODIFIED_NEW,
                    SORT_MODIFIED_OLD, SORT_TITLE, SORT_MESSAGE),
        )
        self.sort_box.pack(side="left", padx=(6, 0))
        self.sort_box.bind("<<ComboboxSelected>>", self.sort_changed)
        self.search_text.trace_add("write", lambda *_: self.apply_filters())
        self.project_box.bind("<<ComboboxSelected>>", self.apply_filters)
        self.type_box.bind("<<ComboboxSelected>>", self.apply_filters)

        family_bar = ttk.Frame(self.conversation_frame)
        family_bar.pack(fill="x", pady=(8, 6))
        self.family_button = ttk.Button(family_bar, text="Show parent + related chats", command=self.view_family)
        self.family_button.pack(side="left")
        self.clear_family_button = ttk.Button(family_bar, text="Clear family filter", command=self.clear_family)
        self.clear_family_button.pack(side="left", padx=8)
        ttk.Label(family_bar, textvariable=self.family_note).pack(side="left", fill="x", expand=True)

        tree_frame = ttk.Frame(self.conversation_frame)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            tree_frame, columns=("title", "project", "state", "edits", "created", "modified", "kind", "parent"),
            displaycolumns=("title", "kind", "project", "state", "edits"),
            show="headings", selectmode="browse", height=7,
        )
        for column, heading, width in (
            ("title", "Conversation", 230), ("project", "Project", 220),
            ("state", "Repair diagnosis", 310), ("edits", "Stored edits", 85),
            ("created", "Created", 150), ("modified", "File modified", 150),
            ("kind", "Type", 120), ("parent", "Parent storage ID", 220),
        ):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, minwidth=60)
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.selected)

        detail_frame = ttk.LabelFrame(
            top, text="3. Understand the selection", padding=8, style="Section.TLabelframe"
        )
        detail_frame.pack(fill="both", expand=True, pady=(0, 10))
        self.notebook = ttk.Notebook(detail_frame)
        self.notebook.pack(fill="both", expand=True)
        overview_tab = ttk.Frame(self.notebook, padding=10)
        history_tab = ttk.Frame(self.notebook, padding=8)
        technical_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(overview_tab, text="Overview")
        self.notebook.add(history_tab, text="Conversation history")
        self.notebook.add(technical_tab, text="Technical evidence")
        ttk.Label(overview_tab, textvariable=self.selected_summary, style="Summary.TLabel", wraplength=1080).pack(anchor="w")
        ttk.Label(overview_tab, textvariable=self.selected_explanation, wraplength=1080).pack(anchor="w", pady=(6, 0))
        self.history = tk.Text(history_tab, height=7, wrap="word", state="disabled", font=("Segoe UI", 10))
        history_scroll = ttk.Scrollbar(history_tab, orient="vertical", command=self.history.yview)
        self.history.configure(yscrollcommand=history_scroll.set)
        self.history.pack(side="left", fill="both", expand=True)
        history_scroll.pack(side="right", fill="y")
        self.details = tk.Text(technical_tab, height=7, wrap="word", state="disabled", font=("Consolas", 9))
        details_scroll = ttk.Scrollbar(technical_tab, orient="vertical", command=self.details.yview)
        self.details.configure(yscrollcommand=details_scroll.set)
        self.details.pack(side="left", fill="both", expand=True)
        details_scroll.pack(side="right", fill="y")
        self.show_history("Select a conversation, then choose Show history. No message text is read during scanning.")

        self.recovery_frame = ttk.LabelFrame(
            top, text="4. Recovery actions", padding=10, style="Section.TLabelframe"
        )
        self.recovery_frame.pack(fill="x")
        self.confirmation = ttk.Checkbutton(
            self.recovery_frame, variable=self.acknowledged,
            text="I observed the stuck Keep/editing symptom and accept losing the selected inline review/Undo history.",
            command=self.update_buttons,
        )
        self.confirmation.pack(anchor="w")
        actions = ttk.Frame(self.recovery_frame)
        actions.pack(fill="x", pady=(8, 0))
        self.preview_button = ttk.Button(actions, text="Show history", command=self.preview)
        self.preview_button.pack(side="left")
        self.prepare_button = ttk.Button(actions, text="Prepare preview + backup", command=self.prepare)
        self.prepare_button.pack(side="left", padx=8)
        self.repair_button = ttk.Button(actions, text="Repair selected conversation", command=self.repair)
        self.repair_button.pack(side="left")
        self.restore_button = ttk.Button(actions, text="Restore incident...", command=self.restore)
        self.restore_button.pack(side="left", padx=8)
        ttk.Label(actions, textvariable=self.status, wraplength=500).pack(side="right", fill="x", expand=True)

        self.add_tooltip(self.scan_button, "Performs a read-only metadata scan. It never changes chat databases.")
        self.add_tooltip(
            self.mode_button,
            "Checks whether VS Code is closed. Host closure does not make unsupported chats repairable.",
        )
        self.add_tooltip(
            self.preview_button,
            "Shows verified local history. Inherited history remains unavailable until its attribution rules are proven.",
        )
        self.add_tooltip(
            self.prepare_button,
            "Creates a local SQLite-consistent backup and an exact repair preview. It does not repair yet.",
        )
        self.add_tooltip(
            self.repair_button,
            "Runs only after eligibility, backup, acknowledgement, explicit confirmation, and verified host closure.",
        )
        self.add_tooltip(
            self.restore_button,
            "Restores only the authorized review records from a validated incident backup; newer messages stay preserved.",
        )
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.update_buttons()
        self.root.after(100, self.poll)
        self.root.after_idle(self.check_mode)

    def show(self, text: str):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def show_history(self, text: str):
        self.history.configure(state="normal")
        self.history.delete("1.0", "end")
        self.history.insert("1.0", text)
        self.history.configure(state="disabled")

    def add_tooltip(self, widget, text: str):
        tooltip = ToolTip(widget, text)
        self.tooltips.append(tooltip)
        return tooltip

    def update_buttons(self):
        selection = self.tree.selection()
        item = self.items.get(selection[0]) if selection else None
        can_prepare = bool(item and item.get("can_prepare"))
        if not item:
            eligibility = "Selected conversation: NONE — select a conversation to check repair eligibility."
        elif not item.get("inspection_complete", True):
            eligibility = "Selected conversation: NOT YET CHECKED — safety inspection pending; preparation and repair unavailable."
        elif not can_prepare:
            reason = item.get("status") or "Safety inspection did not authorize preparation"
            if item.get("evidence"):
                reason += f". {item['evidence']}"
            eligibility = (
                "Selected conversation: REPAIR UNAVAILABLE — "
                "Host closure does not make this conversation repairable; it only permits writes "
                f"for chats that pass every safety check. {reason}"
            )
        elif not self.incident:
            eligibility = "Selected conversation: ELIGIBLE FOR PREPARATION — prepare a preview and backup first. This is not proof of damage."
        elif not self.acknowledged.get():
            eligibility = "Selected conversation: PREVIEW PREPARED — review the scope and acknowledge loss of inline review/Undo history."
        else:
            eligibility = "Selected conversation: PREVIEW ACKNOWLEDGED — Repair still requires verified host closure and explicit confirmation."
        self.eligibility.set(eligibility)
        self.prepare_button.configure(state="normal" if can_prepare and not self.busy and not self.scanning else "disabled")
        self.cancel_button.configure(state="normal" if self.scanning else "disabled")
        self.preview_button.configure(state="normal" if selection and not self.busy else "disabled")
        self.repair_button.configure(
            state="normal" if self.incident and self.acknowledged.get() and not self.busy and not self.read_only else "disabled"
        )
        self.restore_button.configure(state="normal" if not self.busy and not self.read_only else "disabled")
        self.tree.configure(selectmode="none" if self.busy else "browse")
        self.search_entry.configure(state="disabled" if self.busy else "normal")
        self.project_box.configure(state="disabled" if self.busy else "readonly")
        self.sort_box.configure(state="disabled" if self.busy else "readonly")
        self.type_box.configure(state="disabled" if self.busy else "readonly")
        self.family_button.configure(
            state="normal" if not self.busy and self.selected_family_anchor() else "disabled"
        )
        self.clear_family_button.configure(
            state="normal" if not self.busy and self.family_anchor else "disabled"
        )
        for widget in (self.scan_button, self.browse_button, self.location_entry, self.confirmation, self.mode_button):
            widget.configure(state="disabled" if self.busy else "normal")
        if self.scanning:
            self.scan_button.configure(state="disabled")

    def check_mode(self):
        if self.busy or self.closed:
            return
        self.read_only = True
        self.mode.set("Host: CHECKING — writes disabled until closure is verified.")
        self.update_buttons()

        def detect():
            try:
                recovery.ensure_offline()
            except recovery.Refused as error:
                return True, f"Host: READ-ONLY — {error}"
            except Exception as error:
                return True, f"Host: READ-ONLY — host check failed; closure could not be verified: {error}"
            return False, "Host: OFFLINE VERIFIED — no VS Code host detected. Rechecked before every write."

        def detected(result):
            self.read_only, text = result
            self.mode.set(text)
            self.status.set(
                "Scanning, inspection and eligible backup preparation remain available. Close VS Code normally, then recheck."
                if self.read_only else "Host closure verified. Conversation safety restrictions still apply; nothing was repaired."
            )

        self.run(detect, detected)

    def selected(self, event=None):
        if self.busy:
            return
        self.selection_generation += 1
        self.incident = None
        self.acknowledged.set(False)
        selection = self.tree.selection()
        if selection and selection[0] in self.items:
            item = self.items[selection[0]]
            if not item.get("inspection_complete", True):
                self.selected_summary.set(f"Checking {item['title']}...")
                self.selected_explanation.set(
                    "The read-only safety inspection is still running. Recovery actions remain disabled."
                )
            elif item.get("can_prepare"):
                self.selected_summary.set(f"{item['title']} can be prepared for a reversible repair preview.")
                self.selected_explanation.set(
                    "This is eligibility, not proof of corruption. Review the history and exact backup scope before repair."
                )
            else:
                reason = item.get("evidence") or item.get("status") or "The safety scope is not proven."
                self.selected_summary.set(f"Repair blocked: {reason}")
                self.selected_explanation.set(
                    "The host state is a separate gate. Technical evidence explains what must be proven before support can be enabled."
                )
            self.show(
                f"Conversation: {item['title']}\nHost ID: {item['id']}\n"
                f"Project: {item['project']}\nFolders: {', '.join(item['folders']) or 'Unavailable'}\n"
                f"Chat type: {item.get('kind', 'Unknown')}\nParent chat (storage ID): {item.get('parent', 'Unavailable')}\n"
                f"Created (VS Code catalog): {recovery.format_file_time(item.get('created'))}\n"
                f"Last message: Unavailable\n"
                + ("VS Code catalog unavailable for this scan.\n" if not item.get("catalog_available", False) else "")
                +
                f"File modified (not chat activity): {recovery.format_file_time(item['modified'])}\n"
                f"Database: {item['database']}\nStatus: {item['status']}\n{item['evidence']}\n\n"
                + ("Some folder metadata was malformed or not a local file path.\n" if item["identity_warning"] else "")
                + f"Stored edit records: {item['edit_count'] if item['edit_count'] is not None else 'Unavailable'}.\n"
                + ("ALL stored edit records would be reset if repair is confirmed. Actual files stay unchanged.\n" if item["can_prepare"] else "Repair is unavailable for this conversation.\n")
                + "\n".join(item.get("paths", []))
            )
            if not item.get("inspection_complete", True):
                token = self.selection_generation
                scan_token = self.scan_generation
                key = selection[0]
                directory = Path(self.user_data.get())
                database = Path(item["database"])

                def inspect():
                    result = recovery.inspect_session(database, directory)
                    self.read_messages.put(("inspect", scan_token, (token, key, result)))

                threading.Thread(target=inspect, daemon=True).start()
        else:
            self.selected_summary.set("Select a conversation to see its diagnosis.")
            self.selected_explanation.set(
                "Scan is read-only. Repair stays disabled until the selected conversation passes every safety check."
            )
            self.show("Select a conversation to inspect its evidence.")
        self.update_buttons()

    def preview(self):
        if self.tree.selection() and not self.busy:
            key = self.tree.selection()[0]
            item = self.items.get(key, {})
            if not item.get("chat_uri"):
                unavailable = (
                    "Conversation preview unavailable: this database does not have a verified chat mapping supported "
                    "by this tool. No private message text was read."
                )
                self.show_history(unavailable)
                self.show(unavailable)
                self.notebook.select(1)
                return
            token = self.selection_generation
            generation = self.scan_generation
            database = Path(item["database"])
            directory = Path(self.user_data.get())
            self.show_history(
                "Loading verified local turns for the exact selected chat. Inherited history is still being investigated."
            )
            self.notebook.select(1)

            def preview():
                result = recovery.conversation_preview(database, directory)
                self.read_messages.put(("preview", generation, (token, key, result)))

            threading.Thread(target=preview, daemon=True).start()

    def location_changed(self, *_):
        self.cancel_scan()
        self.scanning = False
        self.scan_progress = ""
        self.scan_generation += 1
        self.selection_generation += 1
        self.incident = None
        self.acknowledged.set(False)
        self.render_generation += 1
        self.family_anchor = None
        self.family_note.set("No family filter.")
        self.type_filter.set(ALL_CHAT_TYPES)
        if hasattr(self, "tree"):
            self.tree.delete(*self.tree.get_children())
            self.items = {}
            self.rendered_values = {}
            self.project_filter.set("All projects")
            self.project_box.configure(values=["All projects"])
            self.show("User-data folder changed. Scan again before selecting a conversation.")
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
        if self.closed:
            return
        refresh = False
        for _ in range(50):
            try:
                kind, generation, result = self.read_messages.get_nowait()
            except queue.Empty:
                break
            if generation != self.scan_generation:
                continue
            if kind == "rows":
                rows, completed, total = result
                for item in rows:
                    self.items[item["database"]] = item
                self.scan_progress = f"Scanning: {completed}/{total} databases. "
                refresh = True
            elif kind == "done":
                self.scanning = False
                self.scan_progress = "Scan cancelled. " if result else "Scan complete. "
                refresh = True
            elif kind == "error":
                self.scanning = False
                self.scan_progress = "Scan stopped: " + result + " "
                refresh = True
            elif kind == "inspect":
                token, key, inspected = result
                if token != self.selection_generation or self.tree.selection() != (key,):
                    continue
                previous = self.items.get(key)
                if previous is None or previous["database"] != inspected["database"]:
                    continue
                previous.update({name: value for name, value in inspected.items()
                                 if name not in ("created", "catalog_available", "kind", "parent",
                                                 "parent_id", "owner_uri", "chat_uri", "storage_resource")})
                self.tree.item(key, values=self.row_values(previous))
                self.rendered_values[key] = self.row_values(previous)
                self.selected()
            elif kind == "preview":
                token, key, text = result
                if token == self.selection_generation and self.tree.selection() == (key,):
                    self.show_history(text)
                    self.notebook.select(1)
        if refresh:
            self.refresh_stream()
        try:
            callback, result, error = self.messages.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            if error:
                self.incident = None
                self.read_only = True
                self.mode.set("Host: READ-ONLY — an operation failed or host state is uncertain. Recheck VS Code is closed.")
                self.status.set("Operation refused or failed. Review the error; do not bypass safety checks.")
                self.show(error)
                messagebox.showerror("Operation stopped", error, parent=self.root)
            else:
                callback(result)
            self.update_buttons()
        self.root.after(100, self.poll)

    def refresh_stream(self):
        choices = sorted({self.project_label(item) for item in self.items.values()}, key=str.casefold)
        self.project_box.configure(values=["All projects", *choices])
        self.apply_filters(preserve_selection=True)

    def cancel_scan(self):
        self.scan_cancel.set()

    def populate(self, rows):
        self.family_anchor = None
        self.family_note.set("No family filter.")
        self.items = {str(index): item for index, item in enumerate(rows)}
        choices = sorted({self.project_label(item) for item in rows}, key=str.casefold)
        self.project_box.configure(values=["All projects", *choices])
        if self.project_filter.get() not in choices:
            self.project_filter.set("All projects")
        self.apply_filters()

    @staticmethod
    def project_label(item):
        return item["project"] + " — " + ("; ".join(item["folders"]) or "folder unavailable")

    @staticmethod
    def chat_type(item):
        kind = item.get("kind")
        return kind if kind in CHAT_TYPES else "Unknown"

    def verified_family_rows(self):
        by_id = {}
        for key, item in self.items.items():
            by_id.setdefault(item["id"], []).append((key, item))
        return {
            identity: rows[0] for identity, rows in by_id.items()
            if identity and len(rows) == 1 and self.chat_type(rows[0][1]) != "Unknown"
        }

    def selected_family_anchor(self):
        selection = self.tree.selection()
        item = self.items.get(selection[0]) if selection else None
        if item and self.verified_family_rows().get(item["id"], (None,))[0] == selection[0]:
            return selection[0], item["id"], item.get("parent_id")
        return None

    def family_members(self):
        if not self.family_anchor:
            return None
        key, identity, parent = self.family_anchor
        verified = self.verified_family_rows()
        anchor = verified.get(identity)
        if not anchor or anchor[0] != key or anchor[1].get("parent_id") != parent:
            self.family_note.set("Family evidence changed or is unavailable. Clear family filter and select again.")
            return set()
        members = {key}
        for candidate_id, (candidate_key, item) in verified.items():
            candidate_parent = item.get("parent_id")
            if (candidate_parent == identity
                    or (parent is not None and (candidate_id == parent or candidate_parent == parent))):
                members.add(candidate_key)
        note = f"Family of {identity}: selected chat, direct children"
        if parent is not None:
            note += ", direct parent and siblings."
            if parent not in verified:
                note += f" Parent {parent} is not in verified scan results."
        else:
            note += ". No verified immediate parent; no siblings inferred."
        self.family_note.set(note + " Other filters still apply.")
        return members

    def view_family(self):
        if self.busy:
            return
        anchor = self.selected_family_anchor()
        if anchor is None:
            return
        self.family_anchor = anchor
        self.apply_filters(preserve_selection=True)
        self.selected()

    def clear_family(self):
        if self.busy:
            return
        self.family_anchor = None
        self.family_note.set("No family filter.")
        self.apply_filters()

    def sort_changed(self, event=None):
        requested = self.sort_order.get()
        if requested == SORT_MESSAGE:
            self.sort_order.set(self.active_sort)
            self.apply_filters()
            self.status.set("Last-message sorting unavailable: no verified complete message timestamps. Previous sort retained.")
            return
        self.active_sort = requested
        self.apply_filters()

    def apply_filters(self, event=None, *, preserve_selection=False):
        previous_selection = self.tree.selection()
        selected = previous_selection if preserve_selection else ()
        self.render_generation += 1
        generation = self.render_generation
        if not preserve_selection:
            self.selection_generation += 1
            self.incident = None
            self.acknowledged.set(False)
        text = self.search_text.get().strip().casefold()
        project = self.project_filter.get()
        chat_type = self.type_filter.get()
        family = self.family_members()
        matches = [
            (key, item) for key, item in self.items.items()
            if (project == "All projects" or self.project_label(item) == project)
            and (chat_type == ALL_CHAT_TYPES or self.chat_type(item) == chat_type)
            and (family is None or key in family)
            and text in " ".join((item["title"], item["id"], item["status"],
                                 item.get("kind", ""), item.get("parent", ""), self.project_label(item))).casefold()
        ]
        def sort_key(pair):
            item = pair[1]
            tie = (item["title"].casefold(), item["id"].casefold(), item["id"])
            if self.active_sort == SORT_TITLE:
                return tie
            field = "created" if self.active_sort in (SORT_CREATED_NEW, SORT_CREATED_OLD) else "modified"
            timestamp = item.get(field)
            descending = self.active_sort in (SORT_CREATED_NEW, SORT_MODIFIED_NEW)
            return (timestamp is None, -timestamp if timestamp is not None and descending else timestamp or 0, *tie)

        matches.sort(key=sort_key)
        visible = {key for key, _ in matches}
        existing = set(self.tree.get_children())
        retained = []
        for key in self.tree.get_children():
            if key not in visible:
                self.tree.delete(key)
                self.rendered_values.pop(key, None)
            else:
                retained.append(key)
        reorder = retained != [key for key, _ in matches if key in existing]
        if not selected or selected[0] not in visible:
            if preserve_selection and previous_selection:
                self.selection_generation += 1
                self.incident = None
                self.acknowledged.set(False)
            self.tree.selection_remove(*self.tree.selection())
            self.show("Select a conversation. File modification time is not chat activity; no message text is loaded during scanning.")
        catalog_note = " VS Code catalog unavailable." if self.items and any(
            not item.get("catalog_available", False) for item in self.items.values()
        ) else ""
        self.status.set(self.scan_progress + f"{len(matches)} of {len(self.items)} conversations shown. No repairs were applied.{catalog_note}")
        self.update_buttons()

        def append_batch(start=0):
            if generation != self.render_generation:
                return
            for position, (key, item) in enumerate(matches[start:start + 100], start):
                values = self.row_values(item)
                if key in existing:
                    if self.rendered_values.get(key) != values:
                        self.tree.item(key, values=values)
                    if reorder:
                        self.tree.move(key, "", position)
                else:
                    self.tree.insert("", position, iid=key, values=values)
                self.rendered_values[key] = values
            if start + 100 < len(matches):
                self.root.after(1, lambda: append_batch(start + 100))

        append_batch()

    def row_values(self, item):
        return (
            item["title"], self.project_label(item), item["status"],
            item["edit_count"] if item["edit_count"] is not None else "Unavailable",
            recovery.format_file_time(item.get("created")),
            recovery.format_file_time(item["modified"]),
            item.get("kind", "Unknown"), item.get("parent", "Unavailable"),
        )

    def scan(self):
        if self.busy or self.scanning:
            return
        directory = Path(self.user_data.get())
        self.incident = None
        self.family_anchor = None
        self.family_note.set("No family filter.")
        self.acknowledged.set(False)
        self.render_generation += 1
        self.tree.delete(*self.tree.get_children())
        self.items = {}
        self.rendered_values = {}
        self.scan_generation += 1
        self.selection_generation += 1
        generation = self.scan_generation
        self.scan_cancel = threading.Event()
        cancel = self.scan_cancel
        self.scanning = True
        self.scan_progress = "Discovering databases... "
        self.status.set(self.scan_progress)
        self.update_buttons()

        def worker():
            try:
                for item, completed, total in recovery.stream_scan(directory, cancel=cancel):
                    self.read_messages.put(("rows", generation, ([item], completed, total)))
                self.read_messages.put(("done", generation, cancel.is_set()))
            except (OSError, sqlite3.Error, ValueError, recovery.Refused) as error:
                self.read_messages.put(("error", generation, str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def prepare(self):
        selection = self.tree.selection()
        if not selection:
            return
        database = Path(self.items[selection[0]]["database"])
        selected_key = selection[0]
        directory = Path(self.user_data.get())

        def prepared(path):
            if self.tree.selection() != (selected_key,) or self.user_data.get() != str(directory):
                self.incident = None
                self.status.set("Selection changed during preparation. Select the conversation and prepare again.")
                return
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
            self.status.set("Preview ready. Close VS Code normally and recheck host closure; acknowledge the scope, then confirm Repair.")

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
        self.closed = True
        self.cancel_scan()
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
    Application(root, args.user_data_dir)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
