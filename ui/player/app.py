#!/usr/bin/env python3
import importlib
import json
import sys
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget

try:
    from ...core import SafeOpenError, safe_open_package
except ImportError:
    # Fallback for repository layout where core is a top-level sibling package.
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    core_module = importlib.import_module("core")
    SafeOpenError = core_module.SafeOpenError
    safe_open_package = core_module.safe_open_package


def _format_summary(result) -> str:
    primary_media_path = result.primary_media_path if result.primary_media_path is not None else "None"
    lines = [
        f"package_type: {result.package_type}",
        f"manifest bytes length: {len(result.manifest_bytes)}",
        f"primary_media_path: {primary_media_path}",
        f"file_paths count: {len(result.file_paths)}",
    ]
    return "\n".join(lines)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AIFX Player (v0) — Read-only Viewer")
        self.resize(980, 640)

        self._media_bytes_qba: QByteArray | None = None
        self._media_buffer: QBuffer | None = None
        self._loaded_pixmap: QtGui.QPixmap | None = None

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.errorOccurred.connect(self._on_playback_error)
        self.video_widget = QVideoWidget(self)
        self.player.setVideoOutput(self.video_widget)
        self.video_widget.hide()
        self.image_label = QtWidgets.QLabel(self)
        self.image_label.setAlignment(QtCore.Qt.AlignCenter)
        self.image_label.setScaledContents(False)
        self.image_label.setStyleSheet("background: black;")
        self.image_label.hide()

        self.summary_view = QtWidgets.QPlainTextEdit()
        self.summary_view.setReadOnly(True)
        self.summary_view.setPlainText("Use File -> Open... to inspect an AIFX package.")

        self.play_button = QtWidgets.QPushButton("Play")
        self.pause_button = QtWidgets.QPushButton("Pause")
        self.stop_button = QtWidgets.QPushButton("Stop")

        self.play_button.clicked.connect(self.player.play)
        self.pause_button.clicked.connect(self.player.pause)
        self.stop_button.clicked.connect(self.player.stop)
        self._set_controls_enabled(False)

        controls_layout = QtWidgets.QHBoxLayout()
        controls_layout.addWidget(self.play_button)
        controls_layout.addWidget(self.pause_button)
        controls_layout.addWidget(self.stop_button)
        controls_layout.addStretch(1)

        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.addWidget(self.video_widget, stretch=2)
        layout.addWidget(self.image_label, stretch=2)
        layout.addWidget(self.summary_view)
        layout.addLayout(controls_layout)
        self.setCentralWidget(container)

        file_menu = self.menuBar().addMenu("&File")
        open_action = file_menu.addAction("Open...")
        open_action.triggered.connect(self.on_open)

        self.metadata_dock = QtWidgets.QDockWidget("Metadata", self)
        self.metadata_dock.setAllowedAreas(
            QtCore.Qt.RightDockWidgetArea | QtCore.Qt.LeftDockWidgetArea
        )
        self.metadata_scroll = QtWidgets.QScrollArea(self.metadata_dock)
        self.metadata_scroll.setWidgetResizable(True)
        self.metadata_container = QtWidgets.QWidget(self.metadata_scroll)
        self.metadata_layout = QtWidgets.QVBoxLayout(self.metadata_container)
        self.metadata_layout.setContentsMargins(12, 12, 12, 12)
        self.metadata_layout.setSpacing(12)
        self.metadata_scroll.setWidget(self.metadata_container)
        self.metadata_dock.setWidget(self.metadata_scroll)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.metadata_dock)
        self.metadata_dock.hide()

        view_menu = self.menuBar().addMenu("&View")
        self.metadata_action = view_menu.addAction("Metadata Inspector")
        self.metadata_action.setCheckable(True)
        self.metadata_action.setChecked(False)
        self.metadata_action.toggled.connect(self.metadata_dock.setVisible)
        self.metadata_dock.visibilityChanged.connect(self.metadata_action.setChecked)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.play_button.setEnabled(enabled)
        self.pause_button.setEnabled(enabled)
        self.stop_button.setEnabled(enabled)

    def _clear_metadata(self) -> None:
        while self.metadata_layout.count():
            item = self.metadata_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _add_metadata_section(self, title: str, rows: list[tuple[str, str]]) -> None:
        section = QtWidgets.QWidget(self.metadata_container)
        section_layout = QtWidgets.QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(6)

        header = QtWidgets.QLabel(title, section)
        header.setStyleSheet("font-weight: 700;")
        section_layout.addWidget(header)

        form = QtWidgets.QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(4)
        form.setLabelAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignTop)
        for key, value in rows:
            key_label = QtWidgets.QLabel(key, section)
            value_label = QtWidgets.QLabel(value, section)
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            form.addRow(key_label, value_label)
        section_layout.addLayout(form)
        self.metadata_layout.addWidget(section)

    def _refresh_metadata(self, result) -> None:
        self._clear_metadata()

        primary_media_path = result.primary_media_path if result.primary_media_path is not None else "None"
        self._add_metadata_section(
            "Package Info",
            [
                ("Package Type", str(result.package_type)),
                ("Primary Media Path", primary_media_path),
                ("File Count", str(len(result.file_paths))),
            ],
        )

        manifest_text = result.manifest_bytes.decode("utf-8", errors="replace")
        try:
            manifest_json = json.loads(manifest_text)
        except Exception:
            self._add_metadata_section("Manifest", [("Status", "(Invalid JSON)")])
            raw_label = QtWidgets.QLabel(manifest_text, self.metadata_container)
            raw_label.setWordWrap(True)
            raw_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            raw_label.setStyleSheet("background: rgba(255,255,255,0.04); padding: 8px;")
            self.metadata_layout.addWidget(raw_label)
            self.metadata_layout.addStretch(1)
            return

        if isinstance(manifest_json, dict):
            work = manifest_json.get("work")
            if isinstance(work, dict):
                title = work.get("title")
                if title is not None:
                    self._add_metadata_section("Work", [("Title", str(title))])

            fmt = manifest_json.get("format")
            if fmt is not None:
                self._add_metadata_section("Format", [("Format", str(fmt))])

            aifx_version = manifest_json.get("aifx_version")
            if aifx_version is not None:
                self._add_metadata_section("AIFX", [("Version", str(aifx_version))])

            provenance = manifest_json.get("provenance")
            if isinstance(provenance, dict):
                provenance_rows: list[tuple[str, str]] = []
                primary_tool = provenance.get("primary_tool")
                if primary_tool is not None:
                    provenance_rows.append(("Primary Tool", str(primary_tool)))
                supporting_tools = provenance.get("supporting_tools")
                if supporting_tools is not None:
                    if isinstance(supporting_tools, list):
                        supporting_value = ", ".join(str(item) for item in supporting_tools)
                    else:
                        supporting_value = str(supporting_tools)
                    provenance_rows.append(("Supporting Tools", supporting_value))
                if provenance_rows:
                    self._add_metadata_section("Provenance", provenance_rows)

            declaration = manifest_json.get("declaration")
            if declaration is not None:
                if isinstance(declaration, (dict, list)):
                    declaration_value = json.dumps(declaration, ensure_ascii=True, indent=2)
                else:
                    declaration_value = str(declaration)
                self._add_metadata_section("Declaration", [("Value", declaration_value)])

        self.metadata_layout.addStretch(1)

    def _clear_media_source(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())

        if self._media_buffer is not None and self._media_buffer.isOpen():
            self._media_buffer.close()

        self._media_buffer = None
        self._media_bytes_qba = None

    def _clear_image(self) -> None:
        self._loaded_pixmap = None
        self.image_label.clear()
        self.image_label.hide()

    def _update_scaled_image(self) -> None:
        if self._loaded_pixmap is None:
            self.image_label.clear()
            return

        scaled = self._loaded_pixmap.scaled(
            self.image_label.size(),
            QtCore.Qt.KeepAspectRatio,
            QtCore.Qt.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    def _show_image_from_bytes(self, image_bytes: bytes) -> bool:
        pixmap = QtGui.QPixmap()
        if not pixmap.loadFromData(image_bytes):
            QtWidgets.QMessageBox.critical(self, "Image Error", "Failed to decode image.")
            self._clear_image()
            return False

        self._loaded_pixmap = pixmap
        self.video_widget.hide()
        self.image_label.show()
        self._update_scaled_image()
        return True

    def _load_media_from_bytes(self, media_bytes: bytes, media_path: str | None) -> None:
        self._clear_media_source()

        self._media_bytes_qba = QByteArray(media_bytes)
        self._media_buffer = QBuffer(self)
        self._media_buffer.setData(self._media_bytes_qba)
        if not self._media_buffer.open(QIODevice.ReadOnly):
            raise RuntimeError("Failed to open in-memory media buffer")

        # Give Qt a suffix hint for codec inference (no disk IO happens).
        hint_name = media_path or "audio.wav"
        source_url = QUrl.fromLocalFile(hint_name)

        self.player.setSourceDevice(self._media_buffer, source_url)

    def _on_playback_error(self, _error: QMediaPlayer.Error) -> None:
        QtWidgets.QMessageBox.critical(self, "Playback Error", self.player.errorString())

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.image_label.isVisible():
            self._update_scaled_image()

    @QtCore.Slot()
    def on_open(self) -> None:
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Open AIFX Package",
            "",
            "AIFX Packages (*.aifm *.aifv *.aifi *.aifp)",
        )
        if not file_path:
            return

        try:
            result = safe_open_package(file_path)
        except SafeOpenError as exc:
            QtWidgets.QMessageBox.critical(self, "Open Error", str(exc))
            return
        except Exception:
            QtWidgets.QMessageBox.critical(
                self,
                "Open Error",
                "An unexpected error occurred while opening the package.",
            )
            return

        self.summary_view.setPlainText(_format_summary(result))
        self._refresh_metadata(result)
        if result.package_type == "aifi" and result.primary_media_bytes is not None:
            self._clear_media_source()
            self.video_widget.hide()
            if self._show_image_from_bytes(result.primary_media_bytes):
                self._set_controls_enabled(False)
            else:
                self._set_controls_enabled(False)
            return

        self._clear_image()

        if result.package_type in ("aifm", "aifv") and result.primary_media_bytes is not None:
            self._load_media_from_bytes(result.primary_media_bytes, result.primary_media_path)
            self._set_controls_enabled(True)
            self.video_widget.setVisible(result.package_type == "aifv")
        else:
            self._clear_media_source()
            self._set_controls_enabled(False)
            self.video_widget.hide()


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("AIFX Player")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
