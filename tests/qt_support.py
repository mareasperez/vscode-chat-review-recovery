"""Small adapters for exercising the Qt widgets from migrated state tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

import app


_application = None


def make_ui(test_case, user_data: Path | None = None):
    global _application
    _application = QApplication.instance() or QApplication([])
    temporary = tempfile.TemporaryDirectory()
    test_case.addCleanup(temporary.cleanup)
    settings = QSettings(str(Path(temporary.name) / "settings.ini"), QSettings.Format.IniFormat)
    window = app.MainWindow(user_data or Path(temporary.name), settings=settings, auto_host_check=False)
    def close_window():
        window.busy = False
        window.close()

    test_case.addCleanup(close_window)
    _application.processEvents()
    return QtTestAdapter(window)


def process_events():
    if QApplication.instance():
        QApplication.instance().processEvents()


class _Value:
    def __init__(self, widget, *, kind="text", owner=None):
        self.widget = widget
        self.kind = kind
        self.owner = owner

    def get(self):
        if self.kind == "combo":
            return self.widget.currentText()
        if self.kind == "check":
            return self.widget.isChecked()
        return self.widget.text()

    def set(self, value):
        if self.kind == "combo":
            self.widget.setCurrentText(str(value))
        elif self.kind == "check":
            self.widget.setChecked(bool(value))
        elif self.kind == "path":
            self.owner.set_directory(Path(str(value)))
        else:
            self.widget.setText(str(value))
        process_events()


class _WidgetState:
    def __init__(self, widget):
        self.widget = widget

    def __getitem__(self, key):
        if key != "state":
            raise KeyError(key)
        return "normal" if self.widget.isEnabled() else "disabled"

    def __setitem__(self, key, value):
        if key != "state":
            raise KeyError(key)
        self.widget.setEnabled(value != "disabled")


class _Table:
    def __init__(self, window):
        self.window = window

    def get_children(self):
        return tuple(self.window.visible_keys)

    def selection(self):
        key = self.window.current_key()
        return (key,) if key is not None else ()

    def selection_set(self, key):
        if key in self.window.visible_keys:
            row = self.window.visible_keys.index(key)
            self.window.table.setCurrentCell(row, 0)
            self.window.table.selectRow(row)
            process_events()

    def selection_remove(self, _key=None):
        self.window.table.clearSelection()
        self.window.table.setCurrentCell(-1, -1)
        process_events()

    def item(self, key):
        row = self.window.visible_keys.index(key)
        values = tuple(self.window.table.item(row, col).text() for col in range(self.window.table.columnCount()))
        return {"values": values}

    def __getitem__(self, key):
        if key == "displaycolumns":
            return tuple(range(self.window.table.columnCount()))
        raise KeyError(key)


class QtTestAdapter:
    """Expose semantic helpers while all observable state remains in Qt widgets."""

    def __init__(self, window):
        self.window = window
        self.tree = _Table(window)
        self.search_text = _Value(window.search_input)
        self.type_filter = _Value(window.type_filter, kind="combo")
        self.project_filter = _Value(window.project_filter, kind="combo")
        self.sort_order = _Value(window.sort_filter, kind="combo")
        self.status = _Value(window.operation_status)
        self.family_note = _Value(window.family_note)
        self.acknowledged = _Value(window.acknowledgement, kind="check")
        self.read_only = not window.host_verified
        self.user_data = _Value(window.directory_input, kind="path", owner=window)
        self.type_box = _WidgetState(window.type_filter)
        self.family_button = _WidgetState(window.family_button)
        self.clear_family_button = _WidgetState(window.clear_family_button)
        self.prepare_button = _WidgetState(window.prepare_button)
        self.repair_button = _WidgetState(window.repair_button)
        self.restore_button = _WidgetState(window.restore_button)

    def __getattr__(self, name):
        if name in {"items", "incident", "family_anchor", "selection_generation", "scan_generation", "scanning", "busy", "scan_cancel"}:
            return getattr(self.window, name)
        if name == "read_only":
            return not self.window.host_verified
        if name == "details":
            return _TextView(self.window.technical_text)
        if name == "eligibility":
            return _TextView(self.window.overview_text)
        if name == "mode":
            return _TextView(self.window.host_title)
        if name == "preview_button":
            return _WidgetState(self.window.history_button)
        if name == "scan_button":
            return _WidgetState(self.window.scan_button)
        if name == "mode_button":
            return _WidgetState(self.window.host_refresh)
        if name == "notebook":
            return _Notebook(self.window)
        if name in {"source_frame", "status_frame", "conversation_frame", "recovery_frame"}:
            return self.window.host_card
        if name == "tooltips":
            return []
        if hasattr(self.window, name):
            return getattr(self.window, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in {"window", "tree", "search_text", "type_filter", "project_filter", "sort_order", "status", "family_note", "acknowledged", "user_data", "type_box", "family_button", "clear_family_button", "prepare_button", "repair_button", "restore_button"}:
            object.__setattr__(self, name, value)
            return
        if name in {"items", "incident", "family_anchor", "selection_generation", "scan_generation", "scanning", "busy", "scan_cancel"}:
            setattr(self.window, name, value)
            return
        if name == "read_only":
            self.window.host_verified = not value
            self.window._update_actions()
            return
        object.__setattr__(self, name, value)

    def populate(self, rows):
        self.window.items = {str(i): item for i, item in enumerate(rows)}
        self.window.apply_filters()

    def apply_filters(self):
        self.window.apply_filters()
        process_events()

    def sort_changed(self):
        self.apply_filters()

    def view_family(self):
        self.window.show_family()
        process_events()

    def clear_family(self):
        self.window.clear_family()
        process_events()

    def update_buttons(self):
        self.window._update_actions()



class _TextView:
    def __init__(self, widget):
        self.widget = widget

    def get(self, *_args):
        return self.widget.toPlainText() if hasattr(self.widget, "toPlainText") else self.widget.text()


class _Notebook:
    def __init__(self, window):
        self.window = window

    def tabs(self):
        return tuple(range(self.window.diagnosis_tabs.count()))

    def tab(self, index, _option):
        return {"text": self.window.diagnosis_tabs.tabText(index)}

