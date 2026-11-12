import os
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication


def configure_appearance(app: QApplication) -> None:
    # Some restricted Windows sessions cannot enumerate system fonts. Load only
    # the standard UI fonts in that case, without bundling machine font files.
    if os.name == "nt" and not QFontDatabase.families():
        font_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("segoeui.ttf", "msyh.ttc", "msyhbd.ttc"):
            font_path = font_dir / filename
            if font_path.is_file():
                QFontDatabase.addApplicationFont(str(font_path))
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)


STYLESHEET = """
QMainWindow { background: #10151e; }
QWidget { color: #e2e8f2; font-family: 'Segoe UI', 'Microsoft YaHei UI'; font-size: 13px; }
QLabel#title { font-size: 28px; font-weight: 700; color: #ffffff; }
QWidget#sidebar { background: #141d2a; border-right: 1px solid #27354a; }
QLabel#brand { font-size: 22px; font-weight: 700; color: #ffffff; padding-bottom: 4px; }
QListWidget#navigation { border: 0; background: transparent; outline: 0; padding: 18px 0; }
QListWidget#navigation::item { margin: 3px 0; padding: 13px; border-radius: 6px; }
QListWidget#navigation::item:selected { background: #284139; color: #85e5c4; }
QLabel#hint { color: #92a2b9; }
QLabel#status { background: #283446; color: #c4d3e9; border-radius: 8px; padding: 7px 12px; }
QGroupBox { background: #192230; border: 1px solid #2a384c; border-radius: 12px;
            margin-top: 15px; padding: 22px 18px 14px 18px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 18px; padding: 0 6px; color: #9fb7d9; }
QLineEdit { background: #101824; border: 1px solid #35465e; border-radius: 7px;
            padding: 9px; selection-background-color: #397c70; }
QLineEdit:focus { border-color: #65d8b4; }
QLineEdit:disabled { color: #8491a3; }
QPushButton { background: #293950; border: 1px solid #394c66; border-radius: 7px;
              padding: 10px 17px; font-weight: 600; }
QPushButton:hover { background: #354b68; }
QPushButton:pressed { background: #1c2c40; }
QPushButton#primary { background: #64d8b1; color: #10271f; border-color: #64d8b1; }
QPushButton#primary:hover { background: #87e7c7; }
QPushButton:disabled { background: #202c3d; color: #687b94; border-color: #29384b; }
QPushButton#primary:disabled { background: #202c3d; color: #687b94; border-color: #29384b; }
QPlainTextEdit { background: #0c131d; border: 1px solid #2b3c51; border-radius: 7px;
                 padding: 9px; font-family: 'Cascadia Mono', Consolas; font-size: 12px; }
QListWidget { background: #101824; color: #b6c6dc; border: 1px solid #2b3c51; border-radius: 7px; padding: 5px; }
QListWidget::item { padding: 7px; border-radius: 4px; }
QListWidget::item:selected { background: #315a55; color: #9bf1cf; }
QStatusBar { color: #8fa4c0; background: #10151e; }
QComboBox, QSpinBox { background: #101824; border: 1px solid #35465e; padding: 7px; border-radius: 5px; }
QProgressBar { background: #101824; border: 1px solid #2b3c51; border-radius: 5px;
               text-align: center; min-height: 22px; color: #d9e8e2; }
QProgressBar::chunk { background: #315f50; border-radius: 4px; }
QCheckBox { spacing: 7px; }
QComboBox QAbstractItemView { background: #192230; color: #e2e8f2; selection-background-color: #315a55; }
QScrollArea { border: 0; background: #10151e; }
QScrollArea > QWidget > QWidget { background: #10151e; }
QGraphicsView { border: 1px solid #2b3c51; border-radius: 6px; }
QPushButton:checked { background: #315a55; color: #9bf1cf; }
"""
