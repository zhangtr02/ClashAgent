import sys

from PySide6.QtWidgets import QApplication

from editor.style import configure_appearance
from editor.window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ClashAgent")
    app.setOrganizationName("ClashAgent")
    configure_appearance(app)
    window = MainWindow()
    window.show()
    return app.exec()
