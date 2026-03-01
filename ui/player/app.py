#!/usr/bin/env python3
import sys
from PySide6 import QtWidgets

def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("AIFX Player")
    w = QtWidgets.QMainWindow()
    w.setWindowTitle("AIFX Player (v0) — Read-only Viewer")
    w.resize(980, 640)
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
