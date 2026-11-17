"""Modern PySide6 desktop UI for conservative VS Code chat recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, QSignalBlocker, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import recovery
from qt_theme import COLORS, STYLESHEET


APP_VERSION = "0.2.0"
ALL_PROJECTS = "All projects"
ALL_CHAT_TYPES = "All chat types"
SCAN_CACHE_VERSION = 1
SCAN_CACHE_KEYS = (
    "database",
    "id",
    "title",
    "kind",
    "project",
    "folders",
    "status",
    "evidence",
    "edit_count",
    "parent",
    "parent_id",
    "created",
    "file_modified",
    "catalog_available",
)
CHAT_TYPES = ("Session", "Fork", "Child chat (tool)", "Child chat (origin unknown)", "Unknown")
SORTS = (
    "Created: newest first",
    "Created: oldest first",
    "File modified: newest first",
    "File modified: oldest first",
    "Title: A-Z",
)


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


def set_margins(layout, value=0, spacing=0):
    layout.setContentsMargins(value, value, value, value)
    layout.setSpacing(spacing)
    return layout


class Card(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")


class SummaryCard(Card):
    def __init__(self, icon: str, title: str, value: str = "—", parent=None):
        super().__init__(parent)
        self.setObjectName("summaryCard")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        icon_label = QLabel(icon)
        icon_label.setStyleSheet(f"color:{COLORS['primary']}; font-size:25px; font-weight:700;")
        layout.addWidget(icon_label)
        text = QVBoxLayout()
        text.setSpacing(1)
        title_label = QLabel(title)
        title_label.setObjectName("summaryLabel")
        self.value = QLabel(value)
        self.value.setObjectName("summaryValue")
        text.addWidget(title_label)
        text.addWidget(self.value)
        layout.addLayout(text, 1)


class NavButton(QPushButton):
    def __init__(self, icon: str, text: str, parent=None):
        super().__init__(f"{icon}    {text}", parent)
        self.setObjectName("navButton")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)


class WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class FunctionWorker(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.finished.emit(self.function())
        except Exception:
            self.signals.failed.emit(traceback.format_exc())


class ScanSignals(QObject):
    item = Signal(object, int, int, int)
    finished = Signal(int, bool)
    failed = Signal(int, str)


class ScanWorker(QRunnable):
    def __init__(self, user_data: Path, generation: int, cancel: threading.Event):
        super().__init__()
        self.user_data = user_data
        self.generation = generation
        self.cancel = cancel
        self.signals = ScanSignals()

    def run(self):
        try:
            for item, completed, total in recovery.stream_scan(self.user_data, cancel=self.cancel):
                self.signals.item.emit(item, completed, total, self.generation)
            self.signals.finished.emit(self.generation, self.cancel.is_set())
        except Exception:
            self.signals.failed.emit(self.generation, traceback.format_exc())


class Preferences:
    DIRECTORY = "storage/userDataDirectory"
    REMEMBER = "storage/rememberDirectory"

    def __init__(self, settings: QSettings | None = None):
        self.settings = settings or QSettings("VSCodeChatRecovery", "VSCodeChatRecovery")

    def remember_directory(self) -> bool:
        value = self.settings.value(self.REMEMBER, True)
        return value if isinstance(value, bool) else str(value).casefold() not in {"false", "0", "no"}

    def set_remember_directory(self, enabled: bool):
        self.settings.setValue(self.REMEMBER, enabled)
        if not enabled:
            self.settings.remove(self.DIRECTORY)
        self.settings.sync()

    def initial_directory(self, supplied: Path | None = None) -> Path:
        if supplied is not None:
            return supplied
        if self.remember_directory():
            saved = self.settings.value(self.DIRECTORY, "")
            if isinstance(saved, str) and saved.strip():
                return Path(saved)
        return recovery.default_user_data()

    def save_directory(self, path: Path):
        if self.remember_directory():
            self.settings.setValue(self.DIRECTORY, str(path))
            self.settings.sync()


class MainWindow(QMainWindow):
    def __init__(
        self,
        user_data: Path | None = None,
        *,
        settings: QSettings | None = None,
        auto_host_check: bool = True,
    ):
        super().__init__()
        self.setWindowTitle("VS Code Chat Recovery")
        self.resize(1340, 870)
        self.setMinimumSize(1050, 700)
        self.setStyleSheet(STYLESHEET)

        self.preferences = Preferences(settings)
        self.user_data = self.preferences.initial_directory(user_data)
        self.pool = QThreadPool.globalInstance()
        self.items: dict[str, dict] = {}
        self.visible_keys: list[str] = []
        self.scan_generation = 0
        self.selection_generation = 0
        self.scan_cancel = threading.Event()
        self.scanning = False
        self.busy = False
        self._updating_table = False
        self.host_verified = False
        self.incident: Path | None = None
        self.family_anchor: tuple[str, str, str | None] | None = None
        self.last_scan: datetime | None = None

        self._build_shell()
        self._build_recovery_page()
        self._build_settings_page()
        self._build_about_page()
        self._wire_navigation()
        self._set_page(0)
        self._sync_directory_ui(invalidate=False)
        self._restore_scan_cache()
        self._reset_selection()
        self._update_actions()
        if auto_host_check:
            self.recheck_host()
        else:
            self._set_host_state("unknown", "VS Code status unknown", "Host closure has not been checked")

    # ---------- shell and reusable sections ----------
    def _build_shell(self):
        container = QWidget()
        shell = QHBoxLayout(container)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.setCentralWidget(container)

        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(220)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(16, 22, 16, 18)
        sidebar_layout.setSpacing(8)
        brand = QHBoxLayout()
        logo = QLabel("◇")
        logo.setStyleSheet(f"color:{COLORS['primary']}; font-size:38px; font-weight:800;")
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        title = QLabel("VS Code")
        title.setObjectName("brandTitle")
        subtitle = QLabel("Chat Recovery")
        subtitle.setObjectName("brandTitle")
        utility = QLabel("Local Utility")
        utility.setObjectName("brandSubtitle")
        brand_text.addWidget(title)
        brand_text.addWidget(subtitle)
        brand_text.addWidget(utility)
        brand.addWidget(logo)
        brand.addLayout(brand_text, 1)
        sidebar_layout.addLayout(brand)
        sidebar_layout.addSpacing(18)

        self.recovery_nav = NavButton("◯", "Recovery")
        self.history_nav = NavButton("◷", "History")
        self.settings_nav = NavButton("⚙", "Settings")
        self.about_nav = NavButton("ⓘ", "About")
        self.history_nav.setEnabled(False)
        self.history_nav.setToolTip("Global incident history is not implemented; conversation history lives in Diagnosis.")
        self.nav_buttons = [self.recovery_nav, self.history_nav, self.settings_nav, self.about_nav]
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for index, button in enumerate(self.nav_buttons):
            self.nav_group.addButton(button, index)
            sidebar_layout.addWidget(button)
        sidebar_layout.addStretch(1)

        host_separator = QFrame()
        host_separator.setFixedHeight(1)
        host_separator.setStyleSheet("background:#37517E;")
        sidebar_layout.addWidget(host_separator)
        host_row = QHBoxLayout()
        self.host_led = QLabel("●")
        self.host_led.setStyleSheet(f"color:{COLORS['unknown']}; font-size:20px;")
        host_copy = QVBoxLayout()
        host_copy.setSpacing(0)
        self.host_title = QLabel("VS Code status unknown")
        self.host_title.setObjectName("hostTitle")
        self.host_detail = QLabel("Not checked")
        self.host_detail.setObjectName("sidebarMuted")
        self.host_detail.setWordWrap(True)
        host_copy.addWidget(self.host_title)
        host_copy.addWidget(self.host_detail)
        self.host_refresh = QPushButton("↻")
        self.host_refresh.setObjectName("hostRefresh")
        self.host_refresh.setToolTip("Recheck whether VS Code is closed. Writes always recheck again.")
        self.host_refresh.clicked.connect(self.recheck_host)
        host_row.addWidget(self.host_led)
        host_row.addLayout(host_copy, 1)
        host_row.addWidget(self.host_refresh)
        sidebar_layout.addLayout(host_row)

        self.workspace = QFrame()
        self.workspace.setObjectName("workspace")
        workspace_layout = QVBoxLayout(self.workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        self.pages = QStackedWidget()
        workspace_layout.addWidget(self.pages)
        shell.addWidget(self.sidebar)
        shell.addWidget(self.workspace, 1)

    def _page_scroll(self):
        scroll = QScrollArea()
        scroll.setObjectName("pageScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 22, 28, 24)
        layout.setSpacing(14)
        scroll.setWidget(content)
        return scroll, content, layout

    @staticmethod
    def _header(title: str, subtitle: str):
        wrapper = QHBoxLayout()
        copy = QVBoxLayout()
        copy.setSpacing(1)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        subheading = QLabel(subtitle)
        subheading.setObjectName("pageSubtitle")
        copy.addWidget(heading)
        copy.addWidget(subheading)
        wrapper.addLayout(copy)
        wrapper.addStretch(1)
        return wrapper

    @staticmethod
    def _section_header(number: str, title: str, subtitle: str):
        row = QHBoxLayout()
        icon = QLabel(number)
        icon.setStyleSheet(f"color:{COLORS['primary']}; font-size:22px; font-weight:700;")
        copy = QVBoxLayout()
        copy.setSpacing(0)
        heading = QLabel(title)
        heading.setObjectName("sectionTitle")
        detail = QLabel(subtitle)
        detail.setObjectName("muted")
        copy.addWidget(heading)
        copy.addWidget(detail)
        row.addWidget(icon)
        row.addLayout(copy, 1)
        return row

    # ---------- Recovery page ----------
    def _build_recovery_page(self):
        page, _, layout = self._page_scroll()
        self.recovery_page = page
        header = self._header("Recovery", "Scan, diagnose and safely recover VS Code conversations.")
        self.ready_badge = QLabel("● Checking")
        self.ready_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ready_badge.setMinimumWidth(104)
        header.addWidget(self.ready_badge)
        layout.addLayout(header)

        summary = QHBoxLayout()
        summary.setSpacing(12)
        self.database_summary = SummaryCard("◉", "Databases found")
        self.conversation_summary = SummaryCard("◯", "Conversations")
        self.scan_summary = SummaryCard("◷", "Last scan")
        for card in (self.database_summary, self.conversation_summary, self.scan_summary):
            summary.addWidget(card, 1)
        layout.addLayout(summary)

        scan_card = Card()
        scan_layout = QHBoxLayout(scan_card)
        scan_layout.setContentsMargins(16, 12, 16, 12)
        scan_layout.addLayout(self._section_header("⌕", "1. Scan & Discover", "Read local VS Code databases without modifying them."), 1)
        self.scan_button = QPushButton("▷  Scan Conversations (Read-only)")
        self.scan_button.setObjectName("primaryButton")
        self.scan_button.setToolTip("Read-only scan. No database content is changed.")
        self.scan_button.clicked.connect(self.start_scan)
        self.cancel_scan_button = QPushButton("Cancel")
        self.cancel_scan_button.setObjectName("secondaryButton")
        self.cancel_scan_button.clicked.connect(self.cancel_scan)
        self.cancel_scan_button.setVisible(False)
        scan_layout.addWidget(self.cancel_scan_button)
        scan_layout.addWidget(self.scan_button)
        layout.addWidget(scan_card)

        conversations = Card()
        conversations_layout = QVBoxLayout(conversations)
        conversations_layout.setContentsMargins(14, 12, 14, 14)
        conversations_layout.setSpacing(10)
        conversations_layout.addLayout(
            self._section_header("◯", "2. Conversations", "Search, filter and select a conversation to inspect.")
        )
        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search conversations…")
        self.search_input.setClearButtonEnabled(True)
        self.project_filter = QComboBox()
        self.project_filter.addItem(ALL_PROJECTS)
        self.type_filter = QComboBox()
        self.type_filter.addItems((ALL_CHAT_TYPES, *CHAT_TYPES))
        self.sort_filter = QComboBox()
        self.sort_filter.addItems(SORTS)
        filters.addWidget(self.search_input, 2)
        filters.addWidget(QLabel("Project"))
        filters.addWidget(self.project_filter, 2)
        filters.addWidget(QLabel("Type"))
        filters.addWidget(self.type_filter, 1)
        filters.addWidget(QLabel("Sort"))
        filters.addWidget(self.sort_filter, 1)
        conversations_layout.addLayout(filters)
        family = QHBoxLayout()
        self.family_button = QPushButton("Show parent + related chats")
        self.family_button.setObjectName("secondaryButton")
        self.family_button.clicked.connect(self.show_family)
        self.clear_family_button = QPushButton("Clear family filter")
        self.clear_family_button.setObjectName("secondaryButton")
        self.clear_family_button.clicked.connect(self.clear_family)
        self.family_note = QLabel("No family filter")
        self.family_note.setObjectName("muted")
        family.addWidget(self.family_button)
        family.addWidget(self.clear_family_button)
        family.addWidget(self.family_note, 1)
        conversations_layout.addLayout(family)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("Conversation", "Type", "Project", "Repair diagnosis", "Stored edits"))
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(230)
        self.table.setAccessibleName("Conversations")
        conversations_layout.addWidget(self.table)
        self.empty_label = QLabel("Scan to discover conversations. No messages are read during scanning.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("muted")
        conversations_layout.addWidget(self.empty_label)
        layout.addWidget(conversations, 2)

        lower = QHBoxLayout()
        lower.setSpacing(14)
        diagnosis = Card()
        diagnosis_layout = QVBoxLayout(diagnosis)
        diagnosis_layout.setContentsMargins(16, 12, 16, 14)
        diagnosis_layout.addLayout(
            self._section_header("▣", "3. Selected Conversation & Diagnosis", "Review eligibility, history and technical evidence.")
        )
        self.selected_title = QLabel("No conversation selected")
        self.selected_title.setObjectName("selectedTitle")
        self.selected_meta = QLabel("Select a row above to inspect its exact safety status.")
        self.selected_meta.setObjectName("muted")
        self.selected_meta.setWordWrap(True)
        diagnosis_layout.addWidget(self.selected_title)
        diagnosis_layout.addWidget(self.selected_meta)
        self.diagnosis_tabs = QTabWidget()
        self.overview_text = QLabel("No diagnosis available.")
        self.overview_text.setWordWrap(True)
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(10, 12, 10, 12)
        self.diagnosis_banner = QFrame()
        self.diagnosis_banner.setObjectName("statusBanner")
        banner_layout = QHBoxLayout(self.diagnosis_banner)
        self.diagnosis_icon = QLabel("●")
        self.diagnosis_title = QLabel("Not checked")
        self.diagnosis_title.setObjectName("sectionTitle")
        banner_copy = QVBoxLayout()
        banner_copy.addWidget(self.diagnosis_title)
        banner_copy.addWidget(self.overview_text)
        banner_layout.addWidget(self.diagnosis_icon)
        banner_layout.addLayout(banner_copy, 1)
        overview_layout.addWidget(self.diagnosis_banner)
        overview_layout.addStretch(1)
        self.history_text = QPlainTextEdit()
        self.history_text.setReadOnly(True)
        self.history_text.setPlainText("Select a conversation, then choose Show history.")
        self.technical_text = QPlainTextEdit()
        self.technical_text.setReadOnly(True)
        self.technical_text.setPlainText("Technical evidence appears after selection.")
        self.diagnosis_tabs.addTab(overview, "Overview")
        self.diagnosis_tabs.addTab(self.history_text, "Conversation history")
        self.diagnosis_tabs.addTab(self.technical_text, "Technical evidence")
        diagnosis_layout.addWidget(self.diagnosis_tabs, 1)
        lower.addWidget(diagnosis, 3)

        actions = Card()
        actions.setMaximumWidth(390)
        actions_layout = QVBoxLayout(actions)
        actions_layout.setContentsMargins(16, 12, 16, 14)
        actions_layout.addLayout(
            self._section_header("⌁", "4. Recovery Actions", "Only available when every safety gate passes.")
        )
        self.history_button = QPushButton("◷  Show history")
        self.prepare_button = QPushButton("▣  Prepare preview + backup")
        self.repair_button = QPushButton("⌁  Repair conversation")
        self.restore_button = QPushButton("◉  Restore incident…")
        for button in (self.history_button, self.prepare_button, self.restore_button):
            button.setObjectName("secondaryButton")
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.repair_button.setObjectName("dangerButton")
        self.history_button.clicked.connect(self.show_history)
        self.prepare_button.clicked.connect(self.prepare_repair)
        self.repair_button.clicked.connect(self.repair_selected)
        self.restore_button.clicked.connect(self.restore_incident)
        actions_layout.addWidget(self.history_button)
        actions_layout.addWidget(self.prepare_button)
        actions_layout.addWidget(self.repair_button)
        actions_layout.addWidget(self.restore_button)
        self.acknowledgement = QCheckBox("I understand and accept the recovery impact.")
        self.acknowledgement.setToolTip(
            "Confirm only after observing the stuck Keep/editing symptom. "
            "Recovery removes the selected inline review/Undo history."
        )
        self.acknowledgement.toggled.connect(self._update_actions)
        actions_layout.addWidget(self.acknowledgement)
        acknowledgement_detail = QLabel(
            "Confirm only after observing the stuck Keep/editing symptom. "
            "The selected inline review/Undo history will be lost."
        )
        acknowledgement_detail.setObjectName("muted")
        acknowledgement_detail.setWordWrap(True)
        actions_layout.addWidget(acknowledgement_detail)
        self.operation_status = QLabel("No operation in progress.")
        self.operation_status.setObjectName("muted")
        self.operation_status.setWordWrap(True)
        actions_layout.addWidget(self.operation_status)
        actions_layout.addStretch(1)
        lower.addWidget(actions, 2)
        layout.addLayout(lower)
        layout.addStretch(1)
        self.pages.addWidget(page)

        self.search_input.textChanged.connect(self.filters_changed)
        self.project_filter.currentTextChanged.connect(self.filters_changed)
        self.type_filter.currentTextChanged.connect(self.filters_changed)
        self.sort_filter.currentTextChanged.connect(self.filters_changed)
        self.table.currentCellChanged.connect(self._table_selection_changed)

    # ---------- Settings page ----------
    def _build_settings_page(self):
        page, _, layout = self._page_scroll()
        self.settings_page = page
        layout.addLayout(self._header("Settings", "Configure local application behavior without weakening safety."))

        storage = Card()
        storage_layout = QVBoxLayout(storage)
        storage_layout.setContentsMargins(18, 16, 18, 18)
        storage_layout.addLayout(self._section_header("▣", "Local VS Code Data", "Folder used to scan local conversations."))
        directory_row = QHBoxLayout()
        self.directory_input = QLineEdit()
        self.directory_input.setPlaceholderText("Choose the VS Code user-data directory")
        self.directory_browse = QPushButton("Browse…")
        self.directory_browse.setObjectName("secondaryButton")
        self.directory_detect = QPushButton("Use detected")
        self.directory_detect.setObjectName("secondaryButton")
        directory_row.addWidget(self.directory_input, 1)
        directory_row.addWidget(self.directory_browse)
        directory_row.addWidget(self.directory_detect)
        storage_layout.addLayout(directory_row)
        self.directory_validation = QLabel()
        self.directory_validation.setWordWrap(True)
        storage_layout.addWidget(self.directory_validation)
        layout.addWidget(storage)

        sources = Card()
        sources_layout = QVBoxLayout(sources)
        sources_layout.setContentsMargins(18, 16, 18, 18)
        sources_layout.addLayout(self._section_header("◉", "Data Sources", "Latest scan results for the current directory."))
        source_row = QHBoxLayout()
        self.settings_databases = QLabel("Databases: —")
        self.settings_conversations = QLabel("Conversations: —")
        self.settings_last_scan = QLabel("Last scan: Never")
        source_row.addWidget(self.settings_databases)
        source_row.addWidget(self.settings_conversations)
        source_row.addWidget(self.settings_last_scan)
        source_row.addStretch(1)
        sources_layout.addLayout(source_row)
        layout.addWidget(sources)

        general = Card()
        general_layout = QVBoxLayout(general)
        general_layout.setContentsMargins(18, 16, 18, 18)
        general_layout.addLayout(self._section_header("☷", "General", "Preferences that do not bypass recovery safeguards."))
        self.remember_directory = QCheckBox("Remember the last valid user-data directory")
        self.remember_directory.setChecked(self.preferences.remember_directory())
        self.locked_host_check = QCheckBox("Verify VS Code host immediately before write operations (always on)")
        self.locked_host_check.setChecked(True)
        self.locked_host_check.setEnabled(False)
        general_layout.addWidget(self.remember_directory)
        general_layout.addWidget(self.locked_host_check)
        layout.addWidget(general)
        layout.addStretch(1)
        self.pages.addWidget(page)

        self.directory_browse.clicked.connect(self.browse_directory)
        self.directory_detect.clicked.connect(lambda: self.set_directory(recovery.default_user_data()))
        self.directory_input.editingFinished.connect(self.directory_edited)
        self.remember_directory.toggled.connect(self.preferences.set_remember_directory)

    def _build_about_page(self):
        page, _, layout = self._page_scroll()
        self.about_page = page
        layout.addLayout(self._header("About", "A local, conservative utility for VS Code chat review recovery."))
        about = Card()
        about_layout = QVBoxLayout(about)
        about_layout.setContentsMargins(22, 20, 22, 22)
        title = QLabel("VS Code Chat Recovery")
        title.setObjectName("pageTitle")
        about_layout.addWidget(title)
        about_layout.addWidget(QLabel(f"Version {APP_VERSION}"))
        description = QLabel(
            "Runs locally. Scans are read-only. Repairs require an exact supported scope, a verified backup, "
            "explicit acknowledgement, confirmation, and a fresh host-closure check."
        )
        description.setWordWrap(True)
        description.setObjectName("muted")
        about_layout.addWidget(description)
        warning = QLabel("Unsupported recovery experiment. Keep incident backups local because they contain private chat data.")
        warning.setWordWrap(True)
        about_layout.addWidget(warning)
        layout.addWidget(about)
        layout.addStretch(1)
        self.pages.addWidget(page)

    def _wire_navigation(self):
        self.recovery_nav.clicked.connect(lambda: self._set_page(0))
        self.settings_nav.clicked.connect(lambda: self._set_page(1))
        self.about_nav.clicked.connect(lambda: self._set_page(2))

    def _set_page(self, index: int):
        self.pages.setCurrentIndex(index)
        button_index = {0: 0, 1: 2, 2: 3}[index]
        self.nav_buttons[button_index].setChecked(True)

    # ---------- state, directory, host ----------
    @staticmethod
    def directory_state(path: Path):
        try:
            resolved = path.expanduser().resolve(strict=True)
            if not resolved.is_dir():
                return False, path, "The selected path is not a directory."
            sessions = resolved / "agentSessionData"
            if not sessions.is_dir():
                return False, resolved, "This folder does not contain agentSessionData. Choose the VS Code user-data folder."
            return True, resolved, "Valid VS Code user-data directory"
        except (OSError, RuntimeError):
            return False, path, "The selected directory does not exist or cannot be read."

    def _scan_cache_path(self) -> Path:
        local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        try:
            directory_key = str(self.user_data.expanduser().resolve()).casefold()
        except (OSError, RuntimeError):
            directory_key = str(self.user_data).casefold()
        digest = hashlib.sha256(directory_key.encode("utf-8")).hexdigest()[:24]
        return local_app_data / "VSCodeChatRecovery" / "scan-cache" / f"scan-{digest}.json"

    def _persist_scan_cache(self) -> bool:
        cache_path = self._scan_cache_path()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            items = []
            for item in self.items.values():
                lightweight = {
                    key: item[key]
                    for key in SCAN_CACHE_KEYS
                    if key in item
                }
                if lightweight.get("database"):
                    items.append(lightweight)
            payload = {
                "version": SCAN_CACHE_VERSION,
                "directory": str(self.user_data.expanduser().resolve()),
                "last_scan": self.last_scan.isoformat() if self.last_scan is not None else None,
                "items": items,
            }
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
            os.replace(temporary, cache_path)
            return True
        except (OSError, RuntimeError, TypeError, ValueError):
            return False

    def _restore_scan_cache(self) -> bool:
        cache_path = self._scan_cache_path()
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if payload.get("version") != SCAN_CACHE_VERSION:
                return False
            cached_directory = Path(payload.get("directory", "")).expanduser().resolve()
            if cached_directory != self.user_data.expanduser().resolve():
                return False
            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                return False

            restored: dict[str, dict] = {}
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                database = raw.get("database")
                if not isinstance(database, str) or not database:
                    continue
                item = {
                    key: raw[key]
                    for key in SCAN_CACHE_KEYS
                    if key in raw
                }
                item.update({
                    "status": "Not yet checked",
                    "evidence": "This row was restored from the previous read-only scan. Select it to re-run exact safety inspection.",
                    "inspection_complete": False,
                    "can_prepare": False,
                })
                restored[database] = item

            last_scan_text = payload.get("last_scan")
            restored_last_scan = None
            if isinstance(last_scan_text, str):
                try:
                    restored_last_scan = datetime.fromisoformat(last_scan_text)
                except ValueError:
                    restored_last_scan = None

            if not restored:
                return False
            self.items = restored
            self.last_scan = restored_last_scan
            rendered = (
                restored_last_scan.strftime("%b %d, %Y, %I:%M %p")
                if restored_last_scan is not None
                else "Previous scan"
            )
            self.database_summary.value.setText(str(len(restored)))
            self.conversation_summary.value.setText(str(len(restored)))
            self.scan_summary.value.setText(rendered)
            self.settings_databases.setText(f"Databases: {len(restored)}")
            self.settings_conversations.setText(f"Conversations: {len(restored)}")
            self.settings_last_scan.setText(f"Last scan: {rendered}")
            self.operation_status.setText("Restored previous read-only scan. Select a row to re-run safety inspection.")
            self.empty_label.setVisible(False)
            self.apply_filters()
            return True
        except (OSError, RuntimeError, json.JSONDecodeError, TypeError, ValueError):
            return False

    def _sync_directory_ui(self, *, invalidate=True):
        blocker = QSignalBlocker(self.directory_input)
        self.directory_input.setText(str(self.user_data))
        del blocker
        valid, resolved, message = self.directory_state(self.user_data)
        if valid:
            self.user_data = resolved
            self.directory_validation.setText(f"● {message}")
            self.directory_validation.setStyleSheet(f"color:{COLORS['success']}; font-weight:600;")
            self.preferences.save_directory(resolved)
            self._set_ready(True, "Ready")
        else:
            self.directory_validation.setText(f"● {message}")
            self.directory_validation.setStyleSheet(f"color:{COLORS['danger']}; font-weight:600;")
            self._set_ready(False, "Setup required")
        self.scan_button.setEnabled(valid and not self.scanning and not self.busy)
        if invalidate:
            self.invalidate_scan("Directory changed. Scan results were cleared.", reset_filters=True)

    def set_directory(self, path: Path):
        if Path(path) == self.user_data:
            self._sync_directory_ui(invalidate=False)
            return
        self.user_data = Path(path)
        self._sync_directory_ui(invalidate=True)
        self._restore_scan_cache()

    def directory_edited(self):
        self.set_directory(Path(self.directory_input.text().strip()))

    def browse_directory(self):
        selected = QFileDialog.getExistingDirectory(self, "Choose VS Code user-data folder", str(self.user_data))
        if selected:
            self.set_directory(Path(selected))

    def invalidate_scan(self, message: str, *, reset_filters: bool = False):
        self.cancel_scan()
        self.scanning = False
        self.scan_generation += 1
        self.selection_generation += 1
        self.items.clear()
        self.visible_keys.clear()
        self.family_anchor = None
        self.last_scan = None
        self.table.setRowCount(0)
        self.project_filter.clear()
        self.project_filter.addItem(ALL_PROJECTS)
        if reset_filters:
            self.search_input.clear()
            self.type_filter.setCurrentText(ALL_CHAT_TYPES)
            self.sort_filter.setCurrentText(SORTS[0])
            self.family_note.setText("No family filter.")
        self.database_summary.value.setText("—")
        self.conversation_summary.value.setText("—")
        self.scan_summary.value.setText("Never")
        self.settings_databases.setText("Databases: —")
        self.settings_conversations.setText("Conversations: —")
        self.settings_last_scan.setText("Last scan: Never")
        self.empty_label.setText(message)
        self.empty_label.setVisible(True)
        self._reset_selection()

    def _set_ready(self, ready: bool, text: str):
        tone = COLORS["success"] if ready else COLORS["warning"]
        background = COLORS["success_bg"] if ready else COLORS["warning_bg"]
        self.ready_badge.setText(("✓ " if ready else "! ") + text)
        self.ready_badge.setStyleSheet(
            f"color:{tone}; background:{background}; border-radius:16px; padding:8px 14px; font-weight:700;"
        )

    def _set_host_state(self, state: str, title: str, detail: str):
        color = {
            "closed": COLORS["success"],
            "running": COLORS["danger"],
            "checking": COLORS["warning"],
            "unknown": COLORS["unknown"],
        }[state]
        self.host_led.setStyleSheet(f"color:{color}; font-size:20px;")
        self.host_title.setText(title)
        self.host_detail.setText(detail)
        self.host_verified = state == "closed"
        self.host_refresh.setEnabled(state != "checking" and not self.busy)
        self._update_actions()

    def recheck_host(self):
        if self.busy:
            return
        self._set_host_state("checking", "Checking VS Code…", "Writes are disabled during verification")
        worker = FunctionWorker(recovery.ensure_offline)
        worker.signals.finished.connect(
            lambda _: self._set_host_state("closed", "VS Code Closed", "Host closure verified; writes recheck again")
        )
        worker.signals.failed.connect(self._host_check_failed)
        self.pool.start(worker)

    def _host_check_failed(self, error: str):
        final = error.strip().splitlines()[-1] if error.strip() else "Host check unavailable"
        if (
            "Close all VS Code/Insiders windows normally first" in error
            or "appears to be running" in error
            or "Code is running" in error
        ):
            self._set_host_state("running", "VS Code Running", "Close VS Code normally before write operations")
        else:
            self._set_host_state("unknown", "VS Code status unknown", final)

    # ---------- scan and table ----------
    def start_scan(self):
        valid, resolved, message = self.directory_state(self.user_data)
        if not valid:
            self._set_page(1)
            self.directory_validation.setText(f"● {message}")
            self.directory_validation.setStyleSheet(f"color:{COLORS['danger']}; font-weight:600;")
            return
        self.user_data = resolved
        self.invalidate_scan("Scanning local databases…")
        self.scanning = True
        self.scan_generation += 1
        generation = self.scan_generation
        self.scan_cancel = threading.Event()
        self.scan_button.setEnabled(False)
        self.cancel_scan_button.setVisible(True)
        self.cancel_scan_button.setEnabled(True)
        self.operation_status.setText("Scanning read-only…")
        self._set_ready(False, "Scanning")
        worker = ScanWorker(self.user_data, generation, self.scan_cancel)
        worker.signals.item.connect(self._scan_item)
        worker.signals.finished.connect(self._scan_finished)
        worker.signals.failed.connect(self._scan_failed)
        self.pool.start(worker)

    def cancel_scan(self):
        if self.scanning:
            self.scan_cancel.set()
            self.cancel_scan_button.setEnabled(False)
            self.operation_status.setText("Cancelling scan…")
            self._persist_scan_cache()

    def _scan_item(self, item: dict, completed: int, total: int, generation: int):
        if generation != self.scan_generation:
            return
        self.items[item["database"]] = item
        self.operation_status.setText(f"Scanning read-only: {completed}/{total} databases")
        self.database_summary.value.setText(str(len(self.items)))
        self.conversation_summary.value.setText(str(len(self.items)))
        self.apply_filters()

    def _scan_finished(self, generation: int, cancelled: bool):
        if generation != self.scan_generation:
            return
        self.scanning = False
        self.cancel_scan_button.setVisible(False)
        valid, _, _ = self.directory_state(self.user_data)
        self.scan_button.setEnabled(valid and not self.busy)
        self.last_scan = datetime.now().astimezone()
        rendered = self.last_scan.strftime("%b %d, %Y, %I:%M %p")
        self.scan_summary.value.setText(rendered)
        self.settings_last_scan.setText(f"Last scan: {rendered}")
        self.settings_databases.setText(f"Databases: {len(self.items)}")
        self.settings_conversations.setText(f"Conversations: {len(self.items)}")
        self.operation_status.setText("Scan cancelled." if cancelled else "Scan complete. No repairs were applied.")
        self._persist_scan_cache()
        self._set_ready(valid, "Ready" if valid else "Setup required")
        self.apply_filters()

    def _scan_failed(self, generation: int, error: str):
        if generation != self.scan_generation:
            return
        self.scanning = False
        self.cancel_scan_button.setVisible(False)
        self.operation_status.setText("Scan failed. No repairs were applied.")
        self.empty_label.setText("Scan failed. Open Settings to validate the directory, then retry.")
        self.empty_label.setVisible(True)
        self.technical_text.setPlainText(error)
        self._set_ready(False, "Scan failed")

    @staticmethod
    def project_label(item: dict):
        project = item.get("project") or "Unknown project"
        folders = item.get("folders") or []
        return project + (" — " + "; ".join(folders) if folders else "")

    @staticmethod
    def chat_type(item: dict):
        kind = item.get("kind")
        return kind if kind in CHAT_TYPES else "Unknown"

    def _filtered_items(self):
        text = self.search_input.text().strip().casefold()
        project = self.project_filter.currentText() or ALL_PROJECTS
        chat_type = self.type_filter.currentText() or ALL_CHAT_TYPES
        family = self.family_members()
        rows = []
        for key, item in self.items.items():
            project_name = self.project_label(item)
            item_type = self.chat_type(item)
            haystack = " ".join(
                str(value) for value in (
                    item.get("title", ""), item.get("id", ""), item.get("status", ""),
                    item_type, item.get("parent", ""), project_name,
                )
            ).casefold()
            if (
                (project == ALL_PROJECTS or project_name == project)
                and (chat_type == ALL_CHAT_TYPES or item_type == chat_type)
                and (family is None or key in family)
                and text in haystack
            ):
                rows.append((key, item))
        selected_sort = self.sort_filter.currentText()
        if selected_sort == "Title: A-Z":
            rows.sort(key=lambda row: (str(row[1].get("title", "")).casefold(), row[0].casefold()))
        else:
            field = "created" if selected_sort.startswith("Created") else "modified"
            descending = selected_sort.endswith("newest first")
            rows.sort(
                key=lambda row: (
                    row[1].get(field) is None,
                    -(row[1].get(field) or 0) if descending else row[1].get(field) or 0,
                    str(row[1].get("title", "")).casefold(),
                )
            )
        return rows

    def apply_filters(self):
        selected_key = self.current_key()
        rows = self._filtered_items()
        self.visible_keys = [key for key, _ in rows]
        self._updating_table = True
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for row_index, (key, item) in enumerate(rows):
            values = (
                item.get("title") or item.get("id") or "Untitled conversation",
                self.chat_type(item),
                self.project_label(item),
                item.get("status", "Not yet checked"),
                item.get("edit_count") if item.get("edit_count") is not None else "—",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, key)
                if column == 3:
                    if item.get("can_prepare"):
                        cell.setForeground(QColor(COLORS["success"]))
                    elif item.get("inspection_complete", True):
                        cell.setForeground(QColor(COLORS["warning"]))
                self.table.setItem(row_index, column, cell)
        self.table.setUpdatesEnabled(True)
        self.empty_label.setVisible(not rows)
        if not rows:
            self.empty_label.setText(
                "No conversations match the current filters." if self.items else "No conversations discovered yet."
            )
        self._refresh_project_options()
        if selected_key in self.visible_keys:
            self.table.selectRow(self.visible_keys.index(selected_key))
        elif selected_key is not None:
            self._reset_selection()
        self._updating_table = False
        self._update_actions()

    def filters_changed(self, *_args):
        self.selection_generation += 1
        self.incident = None
        self.acknowledgement.setChecked(False)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.apply_filters()

    def _refresh_project_options(self):
        current = self.project_filter.currentText() or ALL_PROJECTS
        values = [ALL_PROJECTS] + sorted({self.project_label(item) for item in self.items.values()}, key=str.casefold)
        if [self.project_filter.itemText(i) for i in range(self.project_filter.count())] == values:
            return
        blocker = QSignalBlocker(self.project_filter)
        self.project_filter.clear()
        self.project_filter.addItems(values)
        self.project_filter.setCurrentText(current if current in values else ALL_PROJECTS)
        del blocker

    def current_key(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        cell = self.table.item(row, 0)
        return cell.data(Qt.ItemDataRole.UserRole) if cell else None

    def _table_selection_changed(self, current_row: int, _current_col: int, _previous_row: int, _previous_col: int):
        if self._updating_table:
            return
        if current_row < 0:
            self._reset_selection()
            return
        key = self.current_key()
        item = self.items.get(key)
        if item is None:
            self._reset_selection()
            return
        self.selection_generation += 1
        self.incident = None
        self.acknowledgement.setChecked(False)
        self._display_item(item)
        if not item.get("inspection_complete", True):
            generation = self.selection_generation
            database = Path(item["database"])
            worker = FunctionWorker(lambda: recovery.inspect_session(database, self.user_data, strict=True))
            worker.signals.finished.connect(
                lambda inspected, g=generation, k=key: self._inspection_finished(g, k, inspected)
            )
            worker.signals.failed.connect(
                lambda error, g=generation, k=key: self._inspection_failed(g, k, error)
            )
            self.pool.start(worker)
        self._update_actions()

    def _inspection_finished(self, generation: int, key: str, inspected: dict):
        if generation != self.selection_generation or key != self.current_key():
            return
        previous = self.items.get(key)
        if previous is None or previous.get("database") != inspected.get("database"):
            return
        previous.update(
            {
                name: value
                for name, value in inspected.items()
                if name not in {
                    "created", "catalog_available", "kind", "parent", "parent_id",
                    "owner_uri", "chat_uri", "storage_resource",
                }
            }
        )
        self.apply_filters()
        self._display_item(previous)
        self._update_actions()

    def _inspection_failed(self, generation: int, key: str, error: str):
        if generation == self.selection_generation and key == self.current_key():
            self.technical_text.setPlainText(error)

    def _display_item(self, item: dict):
        title = item.get("title") or item.get("id") or "Untitled conversation"
        self.selected_title.setText(title)
        created = recovery.format_file_time(item.get("created"))
        edits = item.get("edit_count") if item.get("edit_count") is not None else "Unavailable"
        self.selected_meta.setText(
            f"{self.chat_type(item)}  •  {self.project_label(item)}  •  Created: {created}  •  Stored edits: {edits}"
        )
        if not item.get("inspection_complete", True):
            self._set_diagnosis("checking", "Checking safety…", "The selected database is being inspected read-only.")
        elif item.get("can_prepare"):
            self._set_diagnosis(
                "success", "Repairable scope found",
                "The conversation can be prepared for a reversible repair preview. This is not proof of corruption.",
            )
        else:
            reason = item.get("evidence") or item.get("status") or "The exact repair scope is not proven."
            self._set_diagnosis(
                "warning", "Needs investigation",
                f"Repair remains disabled: {reason}",
            )
        details = (
            f"Conversation: {title}\nHost/storage ID: {item.get('id', 'Unavailable')}\n"
            f"Project: {item.get('project', 'Unknown')}\nFolders: {'; '.join(item.get('folders') or []) or 'Unavailable'}\n"
            f"Chat type: {self.chat_type(item)}\nParent storage ID: {item.get('parent', 'Unavailable')}\n"
            f"Created: {created}\nFile modified (not chat activity): {recovery.format_file_time(item.get('modified'))}\n"
            f"Database: {item.get('database', 'Unavailable')}\nStatus: {item.get('status', 'Unavailable')}\n"
            f"Evidence: {item.get('evidence') or 'None'}\nStored edit records: {edits}"
        )
        self.technical_text.setPlainText(details)

    def _set_diagnosis(self, tone: str, title: str, detail: str):
        colors = {
            "success": (COLORS["success"], COLORS["success_bg"]),
            "warning": (COLORS["warning"], COLORS["warning_bg"]),
            "checking": (COLORS["primary"], "#DBEAFE"),
            "unknown": (COLORS["unknown"], COLORS["surface_muted"]),
        }
        color, background = colors[tone]
        self.diagnosis_banner.setStyleSheet(
            f"QFrame#statusBanner{{background:{background}; border:1px solid {color}; border-radius:10px;}}"
        )
        self.diagnosis_icon.setText("✓" if tone == "success" else "!" if tone == "warning" else "●")
        self.diagnosis_icon.setStyleSheet(f"color:{color}; font-size:24px; font-weight:700;")
        self.diagnosis_title.setText(title)
        self.overview_text.setText(detail)

    def _reset_selection(self):
        self.selection_generation += 1
        self.incident = None
        if hasattr(self, "acknowledgement"):
            self.acknowledgement.setChecked(False)
        if hasattr(self, "selected_title"):
            self.selected_title.setText("No conversation selected")
            self.selected_meta.setText("Select a row above to inspect its exact safety status.")
            self._set_diagnosis("unknown", "Not checked", "No diagnosis available.")
            self.history_text.setPlainText("Select a conversation, then choose Show history.")
            self.technical_text.setPlainText("Technical evidence appears after selection.")
        self._update_actions()

    # ---------- family filtering ----------
    def verified_family_rows(self):
        by_id: dict[str, list[tuple[str, dict]]] = {}
        for key, item in self.items.items():
            by_id.setdefault(item.get("id"), []).append((key, item))
        return {
            identity: rows[0]
            for identity, rows in by_id.items()
            if identity and len(rows) == 1 and self.chat_type(rows[0][1]) != "Unknown"
        }

    def family_members(self):
        if not self.family_anchor:
            return None
        key, identity, parent = self.family_anchor
        verified = self.verified_family_rows()
        anchor = verified.get(identity)
        if not anchor or anchor[0] != key or anchor[1].get("parent_id") != parent:
            self.family_note.setText("Family evidence changed; clear this filter and select again.")
            return set()
        if parent and parent not in verified:
            self.family_note.setText("Parent is not in verified scan results; showing verified matches only.")
        else:
            self.family_note.setText("Showing verified family. No siblings inferred from unverified origins.")
        members = {key}
        for candidate_id, (candidate_key, item) in verified.items():
            candidate_parent = item.get("parent_id")
            if candidate_parent == identity or (
                parent is not None and (candidate_id == parent or candidate_parent == parent)
            ):
                members.add(candidate_key)
        return members

    def show_family(self):
        if self.busy:
            return
        key = self.current_key()
        item = self.items.get(key)
        verified = self.verified_family_rows()
        if item is None or verified.get(item.get("id"), (None,))[0] != key:
            self.family_note.setText("Select a conversation with a verified family relationship.")
            return
        self.family_anchor = (key, item["id"], item.get("parent_id"))
        self.family_note.setText("Showing verified family. No siblings inferred from unverified origins.")
        self.selection_generation += 1
        self.incident = None
        self.acknowledgement.setChecked(False)
        self.apply_filters()

    def clear_family(self):
        if self.busy:
            return
        self.family_anchor = None
        self.family_note.setText("No family filter.")
        self.selection_generation += 1
        self.incident = None
        self.acknowledgement.setChecked(False)
        self.apply_filters()

    # ---------- history and recovery operations ----------
    def show_history(self):
        key = self.current_key()
        item = self.items.get(key)
        if item is None or self.busy:
            return
        if not item.get("chat_uri"):
            self.history_text.setPlainText(
                "History unavailable: no verified chat mapping exists. No private message text was read."
            )
            self.diagnosis_tabs.setCurrentIndex(1)
            return
        generation = self.selection_generation
        database = Path(item["database"])
        self.history_text.setPlainText(
            "Loading verified local history. Inherited history remains unavailable until its attribution is proven."
        )
        self.diagnosis_tabs.setCurrentIndex(1)
        worker = FunctionWorker(lambda: recovery.conversation_preview(database, self.user_data))
        worker.signals.finished.connect(
            lambda text, g=generation, k=key: self._history_finished(g, k, text)
        )
        worker.signals.failed.connect(
            lambda error, g=generation, k=key: self._history_failed(g, k, error)
        )
        self.pool.start(worker)

    def _history_finished(self, generation: int, key: str, text: str):
        if generation == self.selection_generation and key == self.current_key():
            self.history_text.setPlainText(text)

    def _history_failed(self, generation: int, key: str, error: str):
        if generation == self.selection_generation and key == self.current_key():
            self.history_text.setPlainText(error)

    def prepare_repair(self):
        key = self.current_key()
        item = self.items.get(key)
        if item is None or not item.get("can_prepare") or self.busy:
            return
        selected_database = Path(item["database"])
        selected_directory = self.user_data
        self._run_operation(
            lambda: recovery.prepare(selected_database, selected_directory, recovery.default_incidents()),
            lambda incident: self._prepared(key, selected_database, incident),
            "Preparing an exact preview and SQLite backup…",
        )

    def _prepared(self, key: str, database: Path, incident: Path):
        item = self.items.get(key)
        if item is None or key != self.current_key() or Path(item["database"]).resolve() != database.resolve():
            self.incident = None
            self.operation_status.setText("Selection changed. Prepare the intended conversation again.")
            return
        self.incident = Path(incident)
        self.acknowledgement.setChecked(False)
        plan, _ = recovery.load_plan(self.incident)
        self.operation_status.setText(
            f"Preview prepared: {len(plan['edit_keys'])} stored edits across {len(plan['files'])} files. "
            "Review the scope and acknowledge before repair."
        )
        self._update_actions()

    def repair_selected(self):
        if not self.incident or not self.acknowledgement.isChecked() or not self.host_verified or self.busy:
            return
        answer = QMessageBox.warning(
            self,
            "Confirm offline repair",
            "This resets only the prepared stored review records and their supported summary. "
            "Actual project files and messages remain unchanged. Inline review/Undo history is lost.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run_operation(
            lambda: recovery.operate(self.incident),
            lambda _: self._operation_succeeded("Repair completed. Reopen VS Code and verify the visible symptom."),
            "Repairing the prepared scope after a fresh host check…",
        )

    def restore_incident(self):
        if self.busy or not self.host_verified:
            return
        selected, _ = QFileDialog.getOpenFileName(
            self, "Select incident manifest", str(recovery.default_incidents()), "Incident manifest (incident.json)"
        )
        if not selected:
            return
        manifest = Path(selected)
        self._run_operation(
            lambda: recovery.operate(manifest, restoring=True),
            lambda _: self._operation_succeeded("Scoped restoration completed."),
            "Restoring the authorized review records after a fresh host check…",
        )

    def _run_operation(self, function, success, status: str):
        self.busy = True
        self.operation_status.setText(status)
        self._update_actions()
        worker = FunctionWorker(function)

        def finished(result):
            self.busy = False
            success(result)
            self._update_actions()

        def failed(error):
            self.busy = False
            self.host_verified = False
            self._set_host_state("unknown", "VS Code status unknown", "Operation failed; recheck before any write")
            self.operation_status.setText("Operation stopped. Review Technical evidence; no safety check was bypassed.")
            self.technical_text.setPlainText(error)
            QMessageBox.critical(self, "Operation stopped", error)
            self._update_actions()

        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        self.pool.start(worker)

    def _operation_succeeded(self, message: str):
        self.incident = None
        self.acknowledgement.setChecked(False)
        self.operation_status.setText(message)
        self._update_actions()

    def _update_actions(self):
        if not hasattr(self, "history_button"):
            return
        item = self.items.get(self.current_key())
        can_prepare = bool(item and item.get("can_prepare") and item.get("inspection_complete", True))
        valid, _, _ = self.directory_state(self.user_data)
        self.scan_button.setEnabled(valid and not self.scanning and not self.busy)
        self.history_button.setEnabled(bool(item) and not self.busy)
        self.prepare_button.setEnabled(can_prepare and not self.busy and not self.scanning)
        self.repair_button.setEnabled(
            bool(self.incident) and self.acknowledgement.isChecked() and self.host_verified and not self.busy
        )
        self.restore_button.setEnabled(self.host_verified and not self.busy)
        self.table.setEnabled(not self.busy)
        self.host_refresh.setEnabled(not self.busy)
        selected = self.current_key()
        verified = self.verified_family_rows()
        self.family_button.setEnabled(
            bool(item and selected and verified.get(item.get("id"), (None,))[0] == selected) and not self.busy
        )
        self.clear_family_button.setEnabled(self.family_anchor is not None and not self.busy)
        for widget in (
            self.search_input, self.project_filter, self.type_filter, self.sort_filter,
        ):
            widget.setEnabled(not self.busy)

    def closeEvent(self, event):
        if self.busy:
            QMessageBox.warning(self, "Operation in progress", "Wait for the current operation before closing.")
            event.ignore()
            return
        self.cancel_scan()
        self._persist_scan_cache()
        event.accept()


def self_test(report: Path) -> int:
    result = {}
    temporary = tempfile.TemporaryDirectory()
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("VS Code Chat Recovery")
    app.setOrganizationName("VSCodeChatRecovery")
    app.setApplicationVersion(APP_VERSION)
    window = None
    try:
        settings_path = Path(temporary.name) / "settings.ini"
        settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
        window = MainWindow(Path(temporary.name), settings=settings, auto_host_check=False)
        app.processEvents()
        assert window.pages.count() == 3
        assert window.history_nav.isEnabled() is False
        assert window.repair_button.isEnabled() is False
        assert window.host_verified is False
        connection = sqlite3.connect(Path(temporary.name) / "smoke.db")
        try:
            assert connection.execute("SELECT 1").fetchone() == (1,)
        finally:
            connection.close()
        result = {
            "status": "ok",
            "qt": QApplication.instance().applicationVersion() or APP_VERSION,
            "sqlite": sqlite3.sqlite_version,
            "frozen": bool(getattr(sys, "frozen", False)),
        }
        code = 0
    except Exception:
        result = {"status": "failed", "error": traceback.format_exc()}
        code = 1
    finally:
        if window is not None:
            window.close()
        temporary.cleanup()
    recovery.atomic_json(report, result)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="Read-only diagnostics, without opening the GUI.")
    parser.add_argument("--json", action="store_true", help="Print scan results as JSON.")
    parser.add_argument("--user-data-dir", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true", help="Test bundled Qt/SQLite without real conversations.")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    user_data = args.user_data_dir or recovery.default_user_data()
    if args.self_test:
        if not args.report:
            parser.error("--self-test requires --report")
        return self_test(args.report)
    if args.scan:
        try:
            rows = recovery.scan(user_data)
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
            return 1
    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName("VS Code Chat Recovery")
    application.setOrganizationName("VSCodeChatRecovery")
    application.setApplicationVersion(APP_VERSION)
    window = MainWindow(args.user_data_dir)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
