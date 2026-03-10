#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGING_DIR="$ROOT_DIR/packaging/linux"
APPIMAGE_DIR="$PACKAGING_DIR/appimage"
DIST_DIR="$ROOT_DIR/dist"
BUILD_DIR="$ROOT_DIR/build/linux"
PYI_WORK_DIR="$BUILD_DIR/pyinstaller"
APP_NAME="AIFX Player"
APP_ID="io.aifx.player"
ENTRYPOINT="ui/player/app.py"
SPEC_PATH="$APPIMAGE_DIR/aifx_player_linux.spec"
APPDIR="$BUILD_DIR/AppDir"
PYINSTALLER_DIST_DIR="$DIST_DIR/pyinstaller"
FROZEN_APP_DIR="$PYINSTALLER_DIST_DIR/$APP_NAME"
ICON_SOURCE="$ROOT_DIR/assets/icon/AIFX_Player_1024.png"
DESKTOP_TEMPLATE="$APPIMAGE_DIR/aifx-player.desktop"
APP_RUN_TEMPLATE="$APPIMAGE_DIR/AppRun"

fail() {
  echo "Error: $*" >&2
  exit 1
}

require_file() {
  local path="$1"
  [[ -e "$path" ]] || fail "Missing required file: $path"
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || fail "Missing required command: $cmd"
}

resolve_appimagetool() {
  if [[ -n "${APPIMAGE_TOOL:-}" ]]; then
    printf '%s\n' "$APPIMAGE_TOOL"
    return
  fi

  command -v appimagetool >/dev/null 2>&1 || fail "Missing required command: appimagetool"
  command -v appimagetool
}

read_version() {
  local version

  if [[ -n "${AIFX_PLAYER_VERSION:-}" ]]; then
    printf '%s\n' "$AIFX_PLAYER_VERSION"
    return
  fi

  version="$(
    python3 - <<'PY'
from pathlib import Path
import re

text = Path("packaging/aifx_player_version.py").read_text(encoding="utf-8-sig")
match = re.search(r"StringStruct\('ProductVersion', '([^']+)'\)", text)
if not match:
    raise SystemExit(1)
print(match.group(1))
PY
  )" || fail "Unable to determine application version from packaging/aifx_player_version.py"

  printf '%s\n' "$version"
}

stage_appdir() {
  local version="$1"

  rm -rf "$APPDIR"
  mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" "$APPDIR/usr/share/icons/hicolor/512x512/apps"

  cp -R "$FROZEN_APP_DIR"/. "$APPDIR/usr/lib/$APP_NAME/"
  cp "$ICON_SOURCE" "$APPDIR/$APP_ID.png"
  cp "$ICON_SOURCE" "$APPDIR/usr/share/icons/hicolor/512x512/apps/$APP_ID.png"
  cp "$DESKTOP_TEMPLATE" "$APPDIR/$APP_ID.desktop"
  cp "$APP_RUN_TEMPLATE" "$APPDIR/AppRun"

  chmod +x "$APPDIR/AppRun"

  sed -i "s|@@APP_NAME@@|$APP_NAME|g" "$APPDIR/$APP_ID.desktop"
  sed -i "s|@@APP_ID@@|$APP_ID|g" "$APPDIR/$APP_ID.desktop"
  sed -i "s|@@APP_VERSION@@|$version|g" "$APPDIR/$APP_ID.desktop"
}

build_appimage() {
  local version="$1"
  local output_path="$DIST_DIR/${APP_NAME}-${version}-x86_64.AppImage"
  local appimage_tool

  appimage_tool="$(resolve_appimagetool)"

  ARCH=x86_64 "$appimage_tool" \
    --appimage-extract-and-run \
    "$APPDIR" \
    "$output_path"

  echo "Created AppImage: $output_path"
}

main() {
  cd "$ROOT_DIR"

  require_cmd python3
  require_cmd pyinstaller
  require_file "$ENTRYPOINT"
  require_file "$SPEC_PATH"
  require_file "$ICON_SOURCE"
  require_file "$DESKTOP_TEMPLATE"
  require_file "$APP_RUN_TEMPLATE"

  local version
  version="$(read_version)"

  mkdir -p "$DIST_DIR" "$PYI_WORK_DIR"
  rm -rf "$PYINSTALLER_DIST_DIR"

  pyinstaller \
    --noconfirm \
    --clean \
    --distpath "$PYINSTALLER_DIST_DIR" \
    --workpath "$PYI_WORK_DIR" \
    "$SPEC_PATH"

  [[ -d "$FROZEN_APP_DIR" ]] || fail "Frozen app output not found: $FROZEN_APP_DIR"

  stage_appdir "$version"
  build_appimage "$version"

  echo "Frozen bundle: $FROZEN_APP_DIR"
  echo "AppDir: $APPDIR"
}

main "$@"
