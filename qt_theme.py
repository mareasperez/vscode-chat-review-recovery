COLORS = {
    "sidebar": "#14213D",
    "sidebar_alt": "#0F1A33",
    "primary": "#2563EB",
    "primary_hover": "#1D4ED8",
    "background": "#F8FAFC",
    "surface": "#FFFFFF",
    "surface_muted": "#F1F5F9",
    "text": "#0F172A",
    "muted": "#64748B",
    "border": "#D8E1EE",
    "success": "#16A34A",
    "success_bg": "#DCFCE7",
    "warning": "#B45309",
    "warning_bg": "#FEF3C7",
    "danger": "#DC2626",
    "danger_bg": "#FEE2E2",
    "unknown": "#64748B",
}


STYLESHEET = """
* {
    font-family: "Segoe UI";
    font-size: 9.75pt;
    color: #0F172A;
}
QMainWindow, QWidget#workspace, QScrollArea#pageScroll, QScrollArea#pageScroll > QWidget > QWidget {
    background: #F8FAFC;
}
QFrame#sidebar {
    background: #14213D;
    border: none;
}
QLabel#brandTitle { color: white; font-size: 18px; font-weight: 700; }
QLabel#brandSubtitle, QLabel#sidebarMuted { color: #C7D2FE; font-size: 11px; }
QPushButton#navButton {
    background: transparent;
    color: #E2E8F0;
    border: none;
    border-radius: 9px;
    text-align: left;
    padding: 12px 16px;
    font-size: 14px;
}
QPushButton#navButton:hover { background: #1E3157; }
QPushButton#navButton:checked { background: #2563EB; color: white; font-weight: 600; }
QPushButton#navButton:disabled { color: #718096; }
QPushButton#hostRefresh {
    color: white; background: #23385F; border: 1px solid #37517E;
    border-radius: 8px; min-width: 32px; min-height: 32px;
}
QPushButton#hostRefresh:hover { background: #2D4674; }
QLabel#hostTitle { color: white; font-weight: 600; }
QFrame#card, QFrame#summaryCard, QFrame#statusBanner {
    background: white;
    border: 1px solid #D8E1EE;
    border-radius: 12px;
}
QLabel#pageTitle { font-size: 28px; font-weight: 700; }
QLabel#pageSubtitle, QLabel#muted { color: #64748B; }
QLabel#sectionTitle { font-size: 17px; font-weight: 700; }
QLabel#summaryValue { font-size: 22px; font-weight: 700; }
QLabel#summaryLabel { color: #64748B; }
QLabel#selectedTitle { font-size: 18px; font-weight: 700; }
QLineEdit, QComboBox {
    background: white; border: 1px solid #CBD5E1; border-radius: 7px;
    padding: 7px 10px; min-height: 20px;
}
QLineEdit:focus, QComboBox:focus { border: 2px solid #2563EB; }
QPushButton#primaryButton {
    background: #2563EB; color: white; border: none; border-radius: 8px;
    padding: 9px 16px; font-weight: 600;
}
QPushButton#primaryButton:hover { background: #1D4ED8; }
QPushButton#primaryButton:disabled { background: #BFDBFE; color: #EFF6FF; }
QPushButton#secondaryButton {
    background: white; color: #0F172A; border: 1px solid #CBD5E1;
    border-radius: 8px; padding: 8px 14px;
}
QPushButton#secondaryButton:hover { background: #F1F5F9; border-color: #94A3B8; }
QPushButton#secondaryButton:disabled { color: #94A3B8; background: #F8FAFC; }
QPushButton#dangerButton {
    background: #2563EB; color: white; border: none; border-radius: 8px;
    padding: 9px 16px; font-weight: 600;
}
QPushButton#dangerButton:hover { background: #1D4ED8; }
QPushButton#dangerButton:disabled { background: #CBD5E1; color: #F8FAFC; }
QTableWidget {
    background: white; alternate-background-color: #F8FAFC; border: 1px solid #D8E1EE;
    border-radius: 8px; gridline-color: transparent; selection-background-color: #DBEAFE;
    selection-color: #0F172A;
}
QHeaderView::section {
    background: #F8FAFC; border: none; border-bottom: 1px solid #D8E1EE;
    padding: 9px; font-weight: 600;
}
QTabWidget::pane { border: none; border-top: 1px solid #E2E8F0; }
QTabBar::tab { padding: 9px 14px; color: #475569; background: transparent; }
QTabBar::tab:selected { color: #2563EB; border-bottom: 2px solid #2563EB; font-weight: 600; }
QPlainTextEdit { background: white; border: none; padding: 8px; }
QToolTip { background: #0F172A; color: white; border: none; padding: 6px; }
QCheckBox { spacing: 8px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #CBD5E1; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
