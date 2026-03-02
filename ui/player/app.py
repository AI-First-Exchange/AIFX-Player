#!/usr/bin/env python3
import importlib
import json
import sys
from functools import partial
from pathlib import Path, PurePosixPath
import zipfile

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


def _normalize_member_path(raw_name: str) -> str:
    normalized = raw_name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_unsafe_member_path(normalized: str) -> bool:
    if not normalized:
        return True
    if normalized.startswith("/"):
        return True
    if len(normalized) >= 2 and normalized[1] == ":":
        return True
    return ".." in PurePosixPath(normalized).parts


def _is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def safe_read_member_bytes(package_path: Path, normalized_member_path: str) -> bytes | None:
    normalized_target = _normalize_member_path(normalized_member_path)
    if _is_unsafe_member_path(normalized_target):
        return None

    try:
        with zipfile.ZipFile(package_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                normalized_info_path = _normalize_member_path(info.filename)
                if normalized_info_path != normalized_target:
                    continue
                if _is_unsafe_member_path(normalized_info_path):
                    return None
                if _is_symlink_entry(info):
                    return None
                return zf.read(info.filename)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, KeyError):
        return None

    return None


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
_TEXT_EXTS = {".txt", ".md", ".json"}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AIFX Player (v0) — Read-only Viewer")
        self.resize(980, 640)

        self._media_bytes_qba: QByteArray | None = None
        self._media_buffer: QBuffer | None = None
        self._loaded_pixmap: QtGui.QPixmap | None = None
        self._user_seeking = False
        self._media_seekable = False
        self._current_package_path: Path | None = None
        self._current_file_paths: tuple[str, ...] = ()
        self._recent_paths: list[str] = []

        self.audio_output = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.errorOccurred.connect(self._on_playback_error)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        if hasattr(self.player, "seekableChanged"):
            self.player.seekableChanged.connect(self._on_seekable_changed)
        self.video_widget = QVideoWidget(self)
        self.player.setVideoOutput(self.video_widget)
        self.video_widget.hide()
        self.image_label = QtWidgets.QLabel(self)
        self.image_label.setAlignment(QtCore.Qt.AlignCenter)
        self.image_label.setScaledContents(False)
        self.image_label.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Ignored,
        )
        self.image_label.setMinimumSize(0, 0)
        self.image_label.setStyleSheet("background: black;")
        self.image_label.hide()
        self.empty_label = QtWidgets.QLabel(
            "Use File \u2192 Open... to inspect an AIFX package.",
            self,
        )
        self.empty_label.setAlignment(QtCore.Qt.AlignCenter)
        self.empty_label.setWordWrap(False)
        self.empty_label.setMinimumWidth(0)
        self.empty_label.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Ignored,
        )
        self.empty_label.setMinimumSize(0, 0)
        self.empty_label.setStyleSheet("color: rgba(255,255,255,0.35);")

        self.play_button = QtWidgets.QPushButton("Play")
        self.pause_button = QtWidgets.QPushButton("Pause")
        self.stop_button = QtWidgets.QPushButton("Stop")

        self.play_button.clicked.connect(self.player.play)
        self.pause_button.clicked.connect(self.player.pause)
        self.stop_button.clicked.connect(self.player.stop)
        self._set_controls_enabled(False)

        self.time_current_label = QtWidgets.QLabel("0:00")
        self.time_current_label.setSizePolicy(
            QtWidgets.QSizePolicy.Fixed,
            QtWidgets.QSizePolicy.Fixed,
        )
        self.time_current_label.setMaximumWidth(60)
        self.time_current_label.setMinimumWidth(40)
        self.time_current_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.time_total_label = QtWidgets.QLabel("0:00")
        self.time_total_label.setSizePolicy(
            QtWidgets.QSizePolicy.Fixed,
            QtWidgets.QSizePolicy.Fixed,
        )
        self.time_total_label.setMaximumWidth(60)
        self.time_total_label.setMinimumWidth(40)
        self.time_total_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self.position_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.position_slider.setOrientation(QtCore.Qt.Horizontal)
        self.position_slider.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )
        self.position_slider.setMinimumWidth(0)
        self.position_slider.setRange(0, 0)
        self.position_slider.setEnabled(False)
        self.position_slider.sliderPressed.connect(self._on_slider_pressed)
        self.position_slider.sliderReleased.connect(self._on_slider_released)
        self.position_slider.sliderMoved.connect(self._on_slider_moved)

        self.timeline_row = QtWidgets.QWidget(self)
        self.timeline_row.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )
        self.timeline_row.setMinimumWidth(0)
        timeline_layout = QtWidgets.QHBoxLayout(self.timeline_row)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        timeline_layout.setSpacing(8)
        timeline_layout.addWidget(self.time_current_label)
        timeline_layout.addWidget(self.position_slider, 1)
        timeline_layout.addWidget(self.time_total_label)

        controls_layout = QtWidgets.QHBoxLayout()
        controls_layout.addWidget(self.play_button)
        controls_layout.addWidget(self.pause_button)
        controls_layout.addWidget(self.stop_button)
        controls_layout.addStretch(1)

        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.addWidget(self.empty_label, stretch=1)
        layout.addWidget(self.video_widget, stretch=2)
        layout.addWidget(self.image_label, stretch=2)
        layout.addWidget(self.timeline_row)
        layout.addLayout(controls_layout)
        self.setCentralWidget(container)

        self.file_menu = self.menuBar().addMenu("&File")
        self.open_action = self.file_menu.addAction("Open...")
        self.open_action.triggered.connect(self.on_open)
        self.recent_menu = self.file_menu.addMenu("Open Recent")
        self.clear_recent_action = self.file_menu.addAction("Clear Recent")
        self.clear_recent_action.triggered.connect(self._clear_recent_paths)
        self._refresh_recent_menu()

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

        self.files_dock = QtWidgets.QDockWidget("Files", self)
        self.files_dock.setAllowedAreas(
            QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea
        )
        files_container = QtWidgets.QWidget(self.files_dock)
        files_layout = QtWidgets.QVBoxLayout(files_container)
        files_layout.setContentsMargins(8, 8, 8, 8)
        files_layout.setSpacing(6)
        self.files_filter_input = QtWidgets.QLineEdit(files_container)
        self.files_filter_input.setPlaceholderText("Filter files...")
        self.files_filter_input.textChanged.connect(self._apply_files_filter)
        self.files_list = QtWidgets.QListWidget(files_container)
        self.files_list.itemActivated.connect(self._on_files_item_activated)
        files_layout.addWidget(self.files_filter_input)
        files_layout.addWidget(self.files_list, 1)
        self.files_dock.setWidget(files_container)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.files_dock)
        self.files_dock.hide()

        self.preview_dock = QtWidgets.QDockWidget("Preview", self)
        self.preview_dock.setAllowedAreas(
            QtCore.Qt.RightDockWidgetArea | QtCore.Qt.LeftDockWidgetArea
        )
        self.preview_text = QtWidgets.QPlainTextEdit(self.preview_dock)
        self.preview_text.setReadOnly(True)
        self.preview_text.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        self.preview_dock.setWidget(self.preview_text)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.preview_dock)
        self.preview_dock.hide()

        view_menu = self.menuBar().addMenu("&View")
        self.metadata_action = view_menu.addAction("Metadata Inspector")
        self.metadata_action.setCheckable(True)
        self.metadata_action.setChecked(False)
        self.metadata_action.toggled.connect(self.metadata_dock.setVisible)
        self.metadata_dock.visibilityChanged.connect(self.metadata_action.setChecked)
        self.files_action = view_menu.addAction("Files Browser")
        self.files_action.setCheckable(True)
        self.files_action.setChecked(False)
        self.files_action.toggled.connect(self.files_dock.setVisible)
        self.files_dock.visibilityChanged.connect(self.files_action.setChecked)

        self.view_toolbar = self.addToolBar("View")
        self.view_toolbar.setMovable(False)
        self.metadata_toolbar_action = self.view_toolbar.addAction("Metadata")
        self.metadata_toolbar_action.setCheckable(True)
        self.metadata_toolbar_action.setChecked(False)
        self.metadata_toolbar_action.toggled.connect(self.metadata_dock.setVisible)
        self.metadata_dock.visibilityChanged.connect(self.metadata_toolbar_action.setChecked)

    def _refresh_recent_menu(self) -> None:
        self.recent_menu.clear()
        has_recent = bool(self._recent_paths)
        self.recent_menu.setEnabled(has_recent)

        for index, path in enumerate(self._recent_paths, start=1):
            basename = Path(path).name or path
            action = self.recent_menu.addAction(f"{index}. {basename}")
            action.setToolTip(path)
            action.triggered.connect(partial(self._open_recent_path, path))

        self.recent_menu.addSeparator()
        clear_action = self.recent_menu.addAction("Clear Recent")
        clear_action.setEnabled(has_recent)
        clear_action.triggered.connect(self._clear_recent_paths)

    def _add_recent_path(self, file_path: str) -> None:
        self._recent_paths = [p for p in self._recent_paths if p != file_path]
        self._recent_paths.insert(0, file_path)
        if len(self._recent_paths) > 10:
            self._recent_paths = self._recent_paths[:10]
        self._refresh_recent_menu()

    def _remove_recent_path(self, file_path: str) -> None:
        self._recent_paths = [p for p in self._recent_paths if p != file_path]
        self._refresh_recent_menu()

    def _clear_recent_paths(self) -> None:
        self._recent_paths = []
        self._refresh_recent_menu()

    def _open_recent_path(self, file_path: str) -> None:
        if not Path(file_path).exists():
            QtWidgets.QMessageBox.information(
                self,
                "Open Recent",
                f"File not found:\n{file_path}",
            )
            self._remove_recent_path(file_path)
            return
        self._open_package_path(file_path)

    def _decode_manifest(self, manifest_bytes: bytes) -> tuple[str, dict | None]:
        manifest_text = manifest_bytes.decode("utf-8", errors="replace")
        try:
            manifest_json = json.loads(manifest_text)
        except Exception:
            return manifest_text, None
        if not isinstance(manifest_json, dict):
            return manifest_text, None
        return manifest_text, manifest_json

    def _extract_work_title(self, manifest_json: dict | None, primary_media_path: str | None) -> str:
        if isinstance(manifest_json, dict):
            work = manifest_json.get("work")
            if isinstance(work, dict):
                title = work.get("title")
                if title is not None and str(title).strip():
                    return str(title)
        if primary_media_path:
            return Path(primary_media_path).stem
        return "(untitled)"

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.play_button.setEnabled(enabled)
        self.pause_button.setEnabled(enabled)
        self.stop_button.setEnabled(enabled)

    def _apply_files_filter(self, _text: str = "") -> None:
        needle = self.files_filter_input.text().strip().lower()
        self.files_list.clear()
        for path in self._current_file_paths:
            if needle and needle not in path.lower():
                continue
            self.files_list.addItem(path)

    def _populate_files_list(self, file_paths: tuple[str, ...]) -> None:
        self._current_file_paths = file_paths
        self._apply_files_filter()

    def _select_file_in_list(self, normalized_path: str) -> bool:
        for idx in range(self.files_list.count()):
            item = self.files_list.item(idx)
            if item.text() == normalized_path:
                self.files_list.setCurrentItem(item)
                self.files_list.scrollToItem(item, QtWidgets.QAbstractItemView.PositionAtCenter)
                return True
        return False

    def _read_current_member_bytes(self, normalized_path: str) -> tuple[bytes | None, str | None]:
        if self._current_package_path is None:
            return None, "No package loaded."

        normalized_target = _normalize_member_path(normalized_path)
        if _is_unsafe_member_path(normalized_target):
            return None, "Unsafe archive member path."

        try:
            with zipfile.ZipFile(self._current_package_path, "r") as zf:
                normalized_to_raw: dict[str, str] = {}
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    normalized_info = _normalize_member_path(info.filename)
                    if _is_unsafe_member_path(normalized_info):
                        continue
                    if _is_symlink_entry(info):
                        continue
                    normalized_to_raw.setdefault(normalized_info, info.filename)

                raw_name = normalized_to_raw.get(normalized_target)
                if raw_name is None:
                    return None, "File not found in archive."

                return zf.read(raw_name), None
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, KeyError):
            return None, "Failed to read file from archive."

    def _show_text_preview(self, normalized_path: str, file_bytes: bytes) -> None:
        suffix = Path(normalized_path).suffix.lower()
        if suffix == ".json":
            raw_text = file_bytes.decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw_text)
                text = json.dumps(parsed, ensure_ascii=True, indent=2)
            except Exception:
                text = raw_text
        else:
            text = file_bytes.decode("utf-8", errors="replace")

        self.preview_text.setPlainText(text)
        self.preview_dock.show()

    @QtCore.Slot(QtWidgets.QListWidgetItem)
    def _on_files_item_activated(self, item: QtWidgets.QListWidgetItem) -> None:
        selected_path = item.text()
        suffix = Path(selected_path).suffix.lower()
        file_bytes, error = self._read_current_member_bytes(selected_path)
        if file_bytes is None:
            QtWidgets.QMessageBox.information(
                self,
                "Preview",
                error or f"Preview not supported for: {selected_path}",
            )
            return

        # Keep preview interactions read-only and manual; never autoplay.
        self.player.stop()

        if suffix in _IMAGE_EXTS:
            self._clear_media_source()
            self.video_widget.hide()
            if self._show_image_from_bytes(file_bytes):
                self._set_controls_enabled(False)
            return

        if suffix in _AUDIO_EXTS:
            self._clear_image()
            self._load_media_from_bytes(file_bytes, selected_path)
            self._set_controls_enabled(True)
            self.video_widget.hide()
            return

        if suffix in _VIDEO_EXTS:
            self._clear_image()
            self._load_media_from_bytes(file_bytes, selected_path)
            self._set_controls_enabled(True)
            self.video_widget.show()
            return

        if suffix in _TEXT_EXTS:
            self._clear_media_source()
            self._clear_image()
            self.video_widget.hide()
            self._set_controls_enabled(False)
            self._show_text_preview(selected_path, file_bytes)
            return

        QtWidgets.QMessageBox.information(self, "Preview", f"Preview not supported for: {selected_path}")

    def _fmt_ms(self, ms: int) -> str:
        total_seconds = max(0, int(ms) // 1000)
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    def _set_timeline_enabled(self, enabled: bool) -> None:
        self.position_slider.setEnabled(enabled)

    def _reset_timeline(self) -> None:
        self._user_seeking = False
        self._media_seekable = False
        self.position_slider.setRange(0, 0)
        self.position_slider.setValue(0)
        self.time_current_label.setText("0:00")
        self.time_total_label.setText("0:00")
        self._set_timeline_enabled(False)

    def _on_position_changed(self, position: int) -> None:
        if not self._user_seeking:
            self.position_slider.setValue(position)
        display_ms = self.position_slider.value() if self._user_seeking else position
        self.time_current_label.setText(self._fmt_ms(display_ms))

    def _on_duration_changed(self, duration: int) -> None:
        safe_duration = max(0, int(duration))
        self.position_slider.setRange(0, safe_duration)
        self.time_total_label.setText(self._fmt_ms(safe_duration))
        if not self._user_seeking:
            self.time_current_label.setText(self._fmt_ms(self.player.position()))
        self._media_seekable = self._media_seekable or self.player.isSeekable()
        self._set_timeline_enabled(self._media_seekable and safe_duration > 0)

    def _on_seekable_changed(self, seekable: bool) -> None:
        self._media_seekable = bool(seekable)
        self._set_timeline_enabled(self._media_seekable and self.position_slider.maximum() > 0)

    def _on_slider_pressed(self) -> None:
        self._user_seeking = True

    def _on_slider_released(self) -> None:
        self._user_seeking = False
        if self._media_seekable:
            self.player.setPosition(self.position_slider.value())
        self.time_current_label.setText(self._fmt_ms(self.position_slider.value()))

    def _on_slider_moved(self, value: int) -> None:
        self.time_current_label.setText(self._fmt_ms(value))

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

    def _add_metadata_text_section(self, title: str, text: str) -> None:
        section = QtWidgets.QWidget(self.metadata_container)
        section_layout = QtWidgets.QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(6)

        header = QtWidgets.QLabel(title, section)
        header.setStyleSheet("font-weight: 700;")
        section_layout.addWidget(header)

        body = QtWidgets.QLabel(text, section)
        body.setWordWrap(True)
        body.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        body.setStyleSheet("background: rgba(255,255,255,0.04); padding: 8px;")
        section_layout.addWidget(body)
        self.metadata_layout.addWidget(section)

    def _refresh_metadata(self, result, manifest_text: str, manifest_json: dict | None) -> None:
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

        if manifest_json is None:
            self._add_metadata_section("Manifest", [("Status", "(Invalid JSON)")])
            self._add_metadata_text_section("Manifest Text", manifest_text)
            self.metadata_layout.addStretch(1)
            return

        if result.package_type == "aifm":
            title = self._extract_work_title(manifest_json, result.primary_media_path)
            self._add_metadata_section("Work", [("Title", title)])

            aifx_obj = manifest_json.get("aifx")
            aifx_version = manifest_json.get("aifx_version")
            aifx_format = manifest_json.get("format")
            if isinstance(aifx_obj, dict):
                if aifx_obj.get("version") is not None:
                    aifx_version = aifx_obj.get("version")
                if aifx_obj.get("format") is not None:
                    aifx_format = aifx_obj.get("format")
            version_rows: list[tuple[str, str]] = []
            if aifx_version is not None:
                version_rows.append(("Version", str(aifx_version)))
            if aifx_format is not None:
                version_rows.append(("Format", str(aifx_format)))
            if version_rows:
                self._add_metadata_section("AIFX Version", version_rows)

            provenance_rows: list[tuple[str, str]] = []
            creator = manifest_json.get("creator")
            if isinstance(creator, dict):
                if creator.get("name") is not None:
                    provenance_rows.append(("Creator", str(creator.get("name"))))
                if creator.get("contact") is not None:
                    provenance_rows.append(("Contact", str(creator.get("contact"))))
            ai = manifest_json.get("ai")
            if isinstance(ai, dict) and ai.get("system") is not None:
                provenance_rows.append(("AI System", str(ai.get("system"))))
            if manifest_json.get("mode") is not None:
                provenance_rows.append(("Mode", str(manifest_json.get("mode"))))
            verification = manifest_json.get("verification")
            if isinstance(verification, dict) and verification.get("tier") is not None:
                provenance_rows.append(("Verification Tier", str(verification.get("tier"))))
            provenance = manifest_json.get("provenance")
            if isinstance(provenance, dict):
                if provenance.get("primary_tool") is not None:
                    provenance_rows.append(("Primary Tool", str(provenance.get("primary_tool"))))
                if provenance.get("supporting_tools") is not None:
                    support = provenance.get("supporting_tools")
                    if isinstance(support, list):
                        support_text = ", ".join(str(item) for item in support)
                    else:
                        support_text = str(support)
                    provenance_rows.append(("Supporting Tools", support_text))
            if provenance_rows:
                self._add_metadata_section("Provenance", provenance_rows)

            declaration_text: str | None = None
            metadata_refs = manifest_json.get("metadata_refs")
            if isinstance(metadata_refs, dict):
                declaration_path = metadata_refs.get("declaration_text")
                if isinstance(declaration_path, str):
                    declaration_bytes = safe_read_member_bytes(result.package_path, declaration_path)
                    if declaration_bytes is not None:
                        declaration_text = declaration_bytes.decode("utf-8", errors="replace")

            if declaration_text is None and manifest_json.get("declaration") is not None:
                declaration_obj = manifest_json.get("declaration")
                if isinstance(declaration_obj, (dict, list)):
                    declaration_text = json.dumps(declaration_obj, ensure_ascii=True, indent=2)
                else:
                    declaration_text = str(declaration_obj)

            if declaration_text is not None:
                self._add_metadata_text_section("Declaration", declaration_text)
        else:
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
                self._add_metadata_text_section("Declaration", declaration_value)

        self.metadata_layout.addStretch(1)

    def _clear_media_source(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())

        if self._media_buffer is not None and self._media_buffer.isOpen():
            self._media_buffer.close()

        self._media_buffer = None
        self._media_bytes_qba = None
        self._reset_timeline()

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

    def _show_pixmap(self, pixmap: QtGui.QPixmap) -> None:
        self._loaded_pixmap = pixmap
        self.video_widget.hide()
        self.image_label.show()
        self._update_scaled_image()

    def _make_aifm_placeholder_pixmap(self, title: str) -> QtGui.QPixmap:
        size = self.image_label.size()
        width = max(size.width(), 1280)
        height = max(size.height(), 720)
        pixmap = QtGui.QPixmap(width, height)
        pixmap.fill(QtGui.QColor("black"))
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.TextAntialiasing, True)

        brand_font = QtGui.QFont(self.font())
        brand_font.setPointSize(56)
        brand_font.setBold(True)
        painter.setFont(brand_font)
        painter.setPen(QtGui.QColor(230, 230, 230))
        painter.drawText(pixmap.rect().adjusted(0, -40, 0, 0), QtCore.Qt.AlignCenter, "AIFM")

        title_font = QtGui.QFont(self.font())
        title_font.setPointSize(20)
        painter.setFont(title_font)
        painter.setPen(QtGui.QColor(185, 185, 185))
        painter.drawText(
            pixmap.rect().adjusted(80, 140, -80, -40),
            QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop | QtCore.Qt.TextWordWrap,
            title,
        )
        painter.end()
        return pixmap

    def _show_aifm_artwork_or_placeholder(self, result, manifest_json: dict | None) -> None:
        title = self._extract_work_title(manifest_json, result.primary_media_path)
        cover_path: str | None = None
        if isinstance(manifest_json, dict):
            metadata_refs = manifest_json.get("metadata_refs")
            if isinstance(metadata_refs, dict):
                raw_cover = metadata_refs.get("cover_image")
                if isinstance(raw_cover, str):
                    cover_path = raw_cover

        if cover_path:
            cover_bytes = safe_read_member_bytes(result.package_path, cover_path)
            if cover_bytes is not None:
                cover_pixmap = QtGui.QPixmap()
                if cover_pixmap.loadFromData(cover_bytes):
                    self._show_pixmap(cover_pixmap)
                    return

        self._show_pixmap(self._make_aifm_placeholder_pixmap(title))

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
        self._on_seekable_changed(self.player.isSeekable())

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
        self._open_package_path(file_path)

    def _open_package_path(self, file_path: str) -> None:
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

        self._add_recent_path(file_path)
        manifest_text, manifest_json = self._decode_manifest(result.manifest_bytes)
        self._refresh_metadata(result, manifest_text, manifest_json)
        self._current_package_path = result.package_path
        self._populate_files_list(result.file_paths)
        self.empty_label.hide()

        if result.package_type == "aifp":
            if not self.files_dock.isVisible():
                self.files_dock.show()
            if "manifest.json" in result.file_paths:
                self._select_file_in_list("manifest.json")
                manifest_preview_bytes, _ = self._read_current_member_bytes("manifest.json")
                if manifest_preview_bytes is not None:
                    self._show_text_preview("manifest.json", manifest_preview_bytes)

        if result.package_type == "aifi" and result.primary_media_bytes is not None:
            self._clear_media_source()
            self.video_widget.hide()
            self._show_image_from_bytes(result.primary_media_bytes)
            self._set_controls_enabled(False)
            return

        if result.package_type == "aifm" and result.primary_media_bytes is not None:
            self._load_media_from_bytes(result.primary_media_bytes, result.primary_media_path)
            self._set_controls_enabled(True)
            self.video_widget.hide()
            self._show_aifm_artwork_or_placeholder(result, manifest_json)
            return

        self._clear_image()

        if result.package_type == "aifv" and result.primary_media_bytes is not None:
            self._load_media_from_bytes(result.primary_media_bytes, result.primary_media_path)
            self._set_controls_enabled(True)
            self.video_widget.show()
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
