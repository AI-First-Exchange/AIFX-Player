# Linux Packaging

This project ships Linux packaging through AppImage only. The packaging layer is isolated under `packaging/linux/` and does not modify application behavior.

## Output

Running the build produces artifacts in `dist/`:

- `dist/pyinstaller/AIFX Player/`
- `dist/AIFX Player-<version>-x86_64.AppImage`

## Requirements

- Linux `x86_64`
- `python3`
- `pyinstaller`
- `appimagetool`
- An environment with the project dependencies installed, including `PySide6`

Example setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pyinstaller
```

Install `appimagetool` separately and either:

- put it on `PATH`, or
- export `APPIMAGETOOL=/absolute/path/to/appimagetool`

## Build

```bash
chmod +x packaging/linux/build_appimage.sh
packaging/linux/build_appimage.sh
```

The script:

1. builds the frozen Linux app with PyInstaller using `packaging/linux/appimage/aifx_player_linux.spec`
2. stages an AppDir in `build/linux/AppDir`
3. creates the final AppImage in `dist/`

## Versioning

The AppImage version defaults to the `ProductVersion` value in [packaging/aifx_player_version.py](/home/daddyyo/Dev/AIFX/aifx-player/packaging/aifx_player_version.py). You can override it for a build with:

```bash
AIFX_PLAYER_VERSION=0.3.0 packaging/linux/build_appimage.sh
```

## Notes

- Packaging is Linux-only and reuses the existing `assets/icon/AIFX_Player_1024.png` asset.
- The build pipeline does not modify application logic or the app’s read-only behavior.
- `--dev` remains an application CLI concern; the packaging layer does not add GUI toggles or runtime mutations.
