# AIFX Player

Read-only viewer + playback app for AIFX packages:
- `.aifm` (music)
- `.aifv` (video)
- `.aifi` (image)
- `.aifp` (project)

## Non-negotiable rules (v0)
- Player is **read-only**: no mutation of packages.
- Player does **not** validate integrity and does **not** show PASS/FAIL verdicts.
- Minimal safe-open checks only (manifest exists, primary media exists, safe extraction).
- Developer mode is **CLI-only** (`--dev`). No GUI toggle.

## Dev (macOS/Linux)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python ui/player/app.py
