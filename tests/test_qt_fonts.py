import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from qt_app import MainWindow


class FontTests(unittest.TestCase):
    def test_default_widget_font_has_valid_point_size_without_qt_font_warnings(self):
        application = QApplication.instance() or QApplication([])
        messages = []
        previous = qInstallMessageHandler(lambda _kind, _context, message: messages.append(message))
        window = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
                window = MainWindow(Path(directory), settings=settings, auto_host_check=False)
                window.show()
                application.processEvents()
                self.assertGreater(window.font().pointSizeF(), 0)
                for page in range(window.pages.count()):
                    window._set_page(page)
                    application.processEvents()
                self.assertFalse([message for message in messages if "QFont::setPointSize" in message])
                window.close()
                window = None
        finally:
            if window is not None:
                window.close()
            qInstallMessageHandler(previous)
