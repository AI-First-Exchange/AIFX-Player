#!/usr/bin/env python3
"""
PAION Web Player (AIFM) — v0.7.0 (Path A: Browser Audio)

Goal:
- Portable/self-contained when packaged as PAION.app (no python-vlc, no VLC runtime).
- Plays audio via <audio> in browser, served from local Flask endpoints.
- Keeps: cover top-left, metadata fields, tier/mode/origin link, verify (external verify_aifm.py if present),
  asset buttons (Declaration/Prompt/Lyrics/Manifest), upload file/folder selection, auto-open browser.

Verify semantics (kept from v0.6.x):
- If ONLY manifest.json fails (bytes/hash mismatch), treat as OK with "INTACT (MANIFEST WARN)".
"""

import argparse
import hashlib
import importlib.util
import json
import os
import tempfile
import threading
import webbrowser
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Optional, Tuple, List

from flask import Flask, request, jsonify, Response, send_file
from werkzeug.utils import secure_filename

AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".aiff", ".aif", ".flac", ".ogg")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


# ---------------- Optional external verifier import ----------------

def load_verifier():
    """
    Try:
      1) from verify_aifm import verify
      2) load verify_aifm.py from same folder as this script
    Returns callable verify(Path)->(bool, list[CheckResult]) or None
    """
    try:
        from verify_aifm import verify  # type: ignore
        return verify
    except Exception:
        pass

    try:
        here = Path(__file__).resolve().parent
        candidate = here / "verify_aifm.py"
        if not candidate.exists():
            return None
        spec = importlib.util.spec_from_file_location("verify_aifm", str(candidate))
        if not spec or not spec.loader:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return getattr(mod, "verify", None)
    except Exception:
        return None


VERIFY_FN = load_verifier()


# ---------------- Helpers ----------------

def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


def guess_mimetype(filename: str) -> str:
    suf = Path(filename).suffix.lower()
    if suf in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suf == ".png":
        return "image/png"
    if suf == ".webp":
        return "image/webp"
    if suf == ".pdf":
        return "application/pdf"
    if suf == ".txt":
        return "text/plain; charset=utf-8"
    if suf == ".json":
        return "application/json; charset=utf-8"
    if suf in (".mp3",):
        return "audio/mpeg"
    if suf in (".wav",):
        return "audio/wav"
    if suf in (".m4a", ".aac"):
        return "audio/mp4"
    if suf in (".flac",):
        return "audio/flac"
    if suf in (".ogg",):
        return "audio/ogg"
    return "application/octet-stream"


def read_manifest_from_aifm(aifm_path: Path) -> dict:
    with zipfile.ZipFile(aifm_path, "r") as z:
        if "manifest.json" not in z.namelist():
            return {}
        raw = z.read("manifest.json")
        return json.loads(raw.decode("utf-8"))


def safe_get(d: Any, path: str, default=None):
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def normalize_verify(details: List[dict], original_ok: bool) -> Tuple[bool, str, bool]:
    """
    v0.6+ rule:
      - If only failing path is manifest.json -> OK but WARN badge.
    Returns: (ok, status_label, manifest_warn)
    """
    fails = [d for d in (details or []) if not bool(d.get("ok"))]
    if not fails:
        return (True if original_ok else False), ("INTACT" if original_ok else "TAMPERED"), False

    non_manifest_fails = [f for f in fails if str(f.get("path", "")) != "manifest.json"]
    manifest_fails = [f for f in fails if str(f.get("path", "")) == "manifest.json"]

    if non_manifest_fails:
        return False, "TAMPERED", False

    if manifest_fails:
        return True, "INTACT (MANIFEST WARN)", True

    return (True if original_ok else False), ("INTACT" if original_ok else "TAMPERED"), False


# ---------------- Built-in verifier fallback ----------------

class BuiltinCheck:
    def __init__(self, ok: bool, path: str, reason: str = ""):
        self.ok = ok
        self.path = path
        self.reason = reason


def builtin_verify_aifm(aifm_path: Path) -> Tuple[bool, List[BuiltinCheck]]:
    """
    Uses manifest.integrity.hashed_files:
      { "path/in/zip": { "sha256": "...", "bytes": N } }

    v0.6+ semantics:
      ✅ If ONLY manifest.json fails, still OK (WARN)
    """
    checks: List[BuiltinCheck] = []
    try:
        with zipfile.ZipFile(aifm_path, "r") as z:
            names = set(z.namelist())
            if "manifest.json" not in names:
                return False, [BuiltinCheck(False, "manifest.json", "missing manifest.json")]

            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            integrity = manifest.get("integrity", {}) if isinstance(manifest, dict) else {}
            algo = integrity.get("algorithm", "sha256")
            hashed_files = integrity.get("hashed_files", {})

            if str(algo).lower() != "sha256":
                return False, [BuiltinCheck(False, "integrity.algorithm", f"unsupported algorithm: {algo}")]

            if not isinstance(hashed_files, dict) or not hashed_files:
                return False, [BuiltinCheck(False, "integrity.hashed_files", "missing/empty hashed_files")]

            non_manifest_failed = False
            manifest_failed = False

            def record_fail(path: str, reason: str):
                nonlocal non_manifest_failed, manifest_failed
                if path == "manifest.json":
                    manifest_failed = True
                else:
                    non_manifest_failed = True
                checks.append(BuiltinCheck(False, path, reason))

            for relpath, meta in hashed_files.items():
                if not isinstance(relpath, str):
                    non_manifest_failed = True
                    checks.append(BuiltinCheck(False, str(relpath), "invalid path key"))
                    continue

                if relpath not in names:
                    record_fail(relpath, "missing file")
                    continue

                expected_hash = ""
                expected_bytes = None
                if isinstance(meta, dict):
                    expected_hash = str(meta.get("sha256", "")).lower()
                    expected_bytes = meta.get("bytes", None)

                data = z.read(relpath)
                actual_bytes = len(data)

                if isinstance(expected_bytes, int) and actual_bytes != expected_bytes:
                    record_fail(relpath, f"bytes mismatch: {actual_bytes} != {expected_bytes}")
                    continue

                h = hashlib.sha256(data).hexdigest().lower()
                if expected_hash and h != expected_hash:
                    record_fail(relpath, "sha256 mismatch")
                    continue

                checks.append(BuiltinCheck(True, relpath, ""))

            if non_manifest_failed:
                return False, checks
            if manifest_failed:
                return True, checks
            return True, checks

    except Exception as e:
        return False, [BuiltinCheck(False, "builtin_verify", str(e))]


# ---------------- Asset selection ----------------

def choose_member_by_ext(
    z: zipfile.ZipFile,
    prefixes: Tuple[str, ...],
    exts_priority: Tuple[str, ...],
    hint_substrings: Tuple[str, ...] = (),
) -> Optional[str]:
    names = [n for n in z.namelist() if n and not n.endswith("/")]
    candidates: list[str] = []
    for n in names:
        ln = n.lower()
        if not any(ln.startswith(p) for p in prefixes):
            continue
        suf = Path(ln).suffix
        if suf in exts_priority:
            candidates.append(n)

    if not candidates:
        return None

    def has_hint(n: str) -> bool:
        ln = n.lower()
        return any(h in ln for h in hint_substrings)

    def ext_rank(n: str) -> int:
        ext = Path(n.lower()).suffix
        try:
            return exts_priority.index(ext)
        except ValueError:
            return 999

    hinted = [n for n in candidates if hint_substrings and has_hint(n)]
    pool = hinted if hinted else candidates
    pool_sorted = sorted(pool, key=lambda s: (ext_rank(s), s.lower()))
    return pool_sorted[0] if pool_sorted else None


def find_cover_member(z: zipfile.ZipFile) -> Optional[str]:
    names = set(z.namelist())
    preferred = [
        "metadata/cover.png", "metadata/cover.jpg", "metadata/cover.jpeg", "metadata/cover.webp",
        "cover.png", "cover.jpg", "cover.jpeg", "cover.webp",
    ]
    for p in preferred:
        if p in names:
            return p

    for n in z.namelist():
        ln = n.lower()
        if ln.endswith(IMG_EXTS) and ("/" not in ln or ln.startswith("metadata/")):
            return n
    return None


def find_declaration_member_anyname(z: zipfile.ZipFile) -> Optional[str]:
    return choose_member_by_ext(
        z, prefixes=("metadata/",), exts_priority=(".pdf", ".txt"),
        hint_substrings=("declar", "legal", "license", "statement"),
    )


def find_prompt_member_anyname(z: zipfile.ZipFile) -> Optional[str]:
    return choose_member_by_ext(
        z, prefixes=("metadata/",), exts_priority=(".txt",),
        hint_substrings=("prompt", "suno", "udio", "instruction"),
    )


def find_lyrics_member_anyname(z: zipfile.ZipFile) -> Optional[str]:
    return choose_member_by_ext(
        z, prefixes=("metadata/",), exts_priority=(".txt",),
        hint_substrings=("lyric", "lyrics", "words", "verse"),
    )


def pick_asset_member(z: zipfile.ZipFile, kind: str) -> Optional[str]:
    kind = (kind or "").lower().strip()
    if kind == "cover":
        return find_cover_member(z)
    if kind == "declaration":
        return find_declaration_member_anyname(z)
    if kind == "prompt":
        return find_prompt_member_anyname(z)
    if kind == "lyrics":
        return find_lyrics_member_anyname(z)
    if kind == "manifest":
        return "manifest.json" if "manifest.json" in set(z.namelist()) else None
    return None


# ---------------- AIFM payload audio caching ----------------

class PayloadCache:
    """
    Extract payload audio to a temp folder so the browser can request /api/audio/<index>.
    Keeps extracted files alive as long as the app is running.
    """
    def __init__(self):
        self.temp_root = Path(tempfile.mkdtemp(prefix="paion_audio_cache_"))
        self.extracted: dict[Path, Path] = {}

    def clear(self):
        self.extracted.clear()
        # Keep temp_root; no need to delete (safer while app runs)

    def get_audio_file(self, aifm_path: Path) -> Path:
        if aifm_path in self.extracted:
            return self.extracted[aifm_path]

        with zipfile.ZipFile(aifm_path, "r") as z:
            members = [
                m for m in z.namelist()
                if m.startswith("payload/")
                and m.lower().endswith(AUDIO_EXTS)
                and not m.endswith("/")
            ]
            if not members:
                raise RuntimeError("No audio found in payload/")

            audio_member = members[0]
            ext = Path(audio_member).suffix.lower()
            out = self.temp_root / f"{aifm_path.stem}{ext}"

            with z.open(audio_member) as src, open(out, "wb") as dst:
                dst.write(src.read())

        self.extracted[aifm_path] = out
        return out


# ---------------- Core state (no VLC) ----------------

class PaionState:
    def __init__(self):
        self.tracks: list[Path] = []
        self.playlist_version = 0
        self.info_cache: dict[Path, dict] = {}
        self.payload_cache = PayloadCache()

    def load_tracks(self, tracks: list[Path]):
        self.tracks = tracks
        self.playlist_version += 1
        self.info_cache.clear()
        self.payload_cache.clear()

    def clear(self):
        self.tracks = []
        self.playlist_version += 1
        self.info_cache.clear()
        self.payload_cache.clear()

    def get_track_info(self, aifm_path: Path) -> dict:
        if aifm_path in self.info_cache:
            return self.info_cache[aifm_path]

        info: dict[str, Any] = {
            "fields": {"author": "—", "ai_system": "—", "tier": "—", "mode": "—", "origin_url": ""},
            "verify": {"ok": False, "status": "UNKNOWN", "details": [], "available": False, "engine": "builtin"},
            "assets": {
                "cover": {"exists": False, "member": "", "ext": ""},
                "declaration": {"exists": False, "member": "", "ext": ""},
                "prompt": {"exists": False, "member": "", "ext": ""},
                "lyrics": {"exists": False, "member": "", "ext": ""},
                "manifest": {"exists": False, "member": "", "ext": "json"},
            },
        }

        try:
            manifest = read_manifest_from_aifm(aifm_path)
        except Exception:
            manifest = {}

        author = safe_get(manifest, "creator.name") or safe_get(manifest, "author") or "—"
        ai_system = (
            safe_get(manifest, "origin.ai_platform")
            or safe_get(manifest, "ai.system")
            or safe_get(manifest, "ai_system")
            or "—"
        )
        tier = safe_get(manifest, "verification.tier") or safe_get(manifest, "tier") or "—"
        mode = safe_get(manifest, "mode") or safe_get(manifest, "aifx.governance.mode") or "—"
        origin_url = (
            safe_get(manifest, "origin.primary_url")
            or safe_get(manifest, "origin_url")
            or safe_get(manifest, "origin.url")
            or ""
        )

        info["fields"].update({
            "author": str(author) if author else "—",
            "ai_system": str(ai_system) if ai_system else "—",
            "tier": str(tier) if tier else "—",
            "mode": str(mode) if mode else "—",
            "origin_url": str(origin_url) if origin_url else "",
        })

        try:
            with zipfile.ZipFile(aifm_path, "r") as z:
                for k in ("cover", "declaration", "prompt", "lyrics", "manifest"):
                    member = pick_asset_member(z, k)
                    if member:
                        info["assets"][k] = {
                            "exists": True,
                            "member": member,
                            "ext": Path(member).suffix.lstrip(".").lower() if k != "manifest" else "json",
                        }
        except Exception:
            pass

        if VERIFY_FN is not None:
            try:
                ok, results = VERIFY_FN(aifm_path)
                details = [{
                    "ok": bool(getattr(r, "ok", False)),
                    "path": str(getattr(r, "path", "")),
                    "reason": str(getattr(r, "reason", "")) if getattr(r, "reason", "") else "",
                } for r in results]
                norm_ok, status, _ = normalize_verify(details, original_ok=bool(ok))
                info["verify"] = {"ok": bool(norm_ok), "status": status, "details": details, "available": True, "engine": "verify_aifm.py"}
            except Exception as e:
                info["verify"] = {"ok": False, "status": "ERROR", "details": [{"ok": False, "path": "verify_aifm.py", "reason": str(e)}], "available": True, "engine": "verify_aifm.py"}
        else:
            ok, checks = builtin_verify_aifm(aifm_path)
            details = [{"ok": c.ok, "path": c.path, "reason": c.reason} for c in checks]
            norm_ok, status, _ = normalize_verify(details, original_ok=bool(ok))
            info["verify"] = {"ok": bool(norm_ok), "status": status, "details": details, "available": True, "engine": "builtin"}

        self.info_cache[aifm_path] = info
        return info


# ---------------- Web UI ----------------

HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>PAION</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:24px;max-width:1100px}
    .card{border:1px solid #3333;border-radius:16px;padding:18px}
    .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
    .grid{display:grid;grid-template-columns:200px 1fr;gap:16px;align-items:start}
    button{padding:10px 14px;border-radius:10px;border:1px solid #3334;background:#111;color:#fff;cursor:pointer}
    button:hover{opacity:.9}
    button.secondary{background:#222;color:#fff;border:1px solid #444}
    button.secondary:hover{background:#2a2a2a}
    button:disabled{opacity:.45;cursor:not-allowed}
    .muted{opacity:.75}
    .title{font-size:18px;font-weight:800}
    .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace}
    .pill{font-size:12px;border:1px solid #3334;border-radius:999px;padding:6px 10px}
    .picker{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
    input[type="file"]{border:1px solid #3334;border-radius:10px;padding:8px}
    .list{margin-top:14px;max-height:320px;overflow:auto;border-top:1px solid #3332;padding-top:12px}
    .track{padding:8px 10px;border-radius:10px;cursor:pointer}
    .track:hover{background:#3331}
    .track.active{background:#3b82f633}
    #cover{width:200px;height:200px;border-radius:16px;object-fit:cover;border:1px solid #3333;display:none}
    .kv{display:grid;grid-template-columns:140px 1fr;gap:8px 12px;margin-top:10px}
    .k{opacity:.7}
    .help{margin-top:10px}
    .toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:10px}
    .small{padding:8px 12px}
    a{color:#7dd3fc;text-decoration:none}
    a:hover{text-decoration:underline}
    audio{width:100%; margin-top:10px}
  </style>
</head>
<body>
  <h2>PAION <span class="muted">(AIFM Player)</span></h2>

  <div class="card">
    <div class="picker">
      <label class="mono">Load .aifm files:</label>
      <input id="filePick" type="file" multiple accept=".aifm" />
      <label class="mono">Load folder:</label>
      <input id="folderPick" type="file" webkitdirectory directory />
      <button onclick="uploadPicked()">Load Selected</button>
      <button class="secondary" onclick="clearPlaylist()">Clear</button>
      <span class="muted">Folder pick works in Chromium + Safari (webkitdirectory).</span>
    </div>

    <div class="help muted">
      Local-only web app. Audio plays in your browser (no VLC dependency).
    </div>

    <audio id="audio" controls preload="metadata"></audio>

    <div style="height:14px"></div>

    <div class="grid">
      <div>
        <img id="cover" src="" alt="cover" />
      </div>

      <div>
        <div class="row" style="justify-content:space-between; align-items:flex-start">
          <div>
            <div class="title" id="title">No playlist loaded</div>
            <div class="muted mono" id="metaLine"></div>
          </div>
          <div class="row">
            <div class="pill mono" id="verifyBadge">VERIFY: —</div>
            <button class="small" onclick="showVerifyDetails()">Verify details</button>
          </div>
        </div>

        <div class="toolbar">
          <button id="btnDecl" class="small" onclick="openAsset('declaration')" disabled>Declaration</button>
          <button id="btnPrompt" class="small" onclick="openAsset('prompt')" disabled>Prompt</button>
          <button id="btnLyrics" class="small" onclick="openAsset('lyrics')" disabled>Lyrics</button>
          <button id="btnManifest" class="small" onclick="openAsset('manifest')" disabled>Manifest</button>
        </div>

        <div class="kv mono">
          <div class="k">Author</div><div id="m_author">—</div>
          <div class="k">AI System</div><div id="m_ai">—</div>
          <div class="k">Tier</div><div id="m_tier">—</div>
          <div class="k">Mode</div><div id="m_mode">—</div>
          <div class="k">Origin</div><div id="m_origin">—</div>
        </div>

        <div class="row" style="margin-top:12px">
          <button onclick="prev()">⏮ Prev</button>
          <button onclick="next()">⏭ Next</button>
        </div>
      </div>
    </div>

    <div class="list" id="list"></div>
  </div>

<script>
let tracks = [];
let idx = 0;
let verifyDetails = [];

const audio = document.getElementById('audio');

audio.addEventListener('ended', () => {
  next(true);
});

function basename(path){
  if(!path) return "";
  const parts = path.split("/");
  return parts[parts.length-1] || path;
}

function setBtn(btn, base, asset){
  if(asset && asset.exists){
    btn.disabled = false;
    const name = basename(asset.member);
    btn.textContent = name ? `${base} (${name})` : base;
  } else {
    btn.disabled = true;
    btn.textContent = base;
  }
}

function setOriginLink(el, url){
  if(!url){
    el.textContent = "—";
    return;
  }
  el.innerHTML = `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`;
}

function showVerifyDetails(){
  const rows = (verifyDetails || []).map(r=>{
    const s = r.ok ? "OK" : "FAIL";
    const reason = r.reason ? ` — ${r.reason}` : "";
    return `[${s}] ${r.path}${reason}`;
  }).join("\\n");
  alert(rows || "No verify details available.");
}

function openAsset(kind){
  const url = `/api/asset/${idx}/${kind}?t=${Date.now()}`;
  window.open(url, "_blank");
}

function updateActive(){
  document.querySelectorAll('.track').forEach((el,i)=>{
    el.classList.toggle('active', i===idx);
  });
}

async function fetchTracks(){
  const res = await fetch('/api/tracks');
  const data = await res.json();
  tracks = data.tracks || [];
}

async function fetchInfo(i){
  const res = await fetch(`/api/track_info/${i}?t=${Date.now()}`);
  return res.json();
}

function renderList(){
  const list = document.getElementById('list');
  list.innerHTML = "";
  if(!tracks.length){
    const empty = document.createElement('div');
    empty.className = 'muted';
    empty.textContent = "No tracks loaded. Use the file/folder picker above.";
    list.appendChild(empty);
    return;
  }
  tracks.forEach((t,i)=>{
    const div = document.createElement('div');
    div.className = 'track' + (i===idx ? ' active' : '');
    div.textContent = `${String(i+1).padStart(2,'0')}. ${t.title}`;
    div.onclick = async ()=>{ await setIndex(i, true); };
    list.appendChild(div);
  });
}

async function setIndex(i, autoplay){
  if(!tracks.length) return;
  idx = Math.max(0, Math.min(i, tracks.length-1));

  const info = await fetchInfo(idx);

  document.getElementById('title').textContent = tracks[idx].title || "—";
  document.getElementById('metaLine').textContent = `${idx+1}/${tracks.length}`;

  const fields = info.fields || {};
  const assets = info.assets || {};
  const verify = info.verify || {};
  verifyDetails = verify.details || [];

  const engine = verify.engine ? ` (${verify.engine})` : "";
  document.getElementById('verifyBadge').textContent = `VERIFY: ${verify.status || '—'}${engine}`;

  document.getElementById('m_author').textContent = fields.author || "—";
  document.getElementById('m_ai').textContent = fields.ai_system || "—";
  document.getElementById('m_tier').textContent = fields.tier || "—";
  document.getElementById('m_mode').textContent = fields.mode || "—";
  setOriginLink(document.getElementById('m_origin'), fields.origin_url || "");

  setBtn(document.getElementById('btnDecl'), 'Declaration', assets.declaration);
  setBtn(document.getElementById('btnPrompt'), 'Prompt', assets.prompt);
  setBtn(document.getElementById('btnLyrics'), 'Lyrics', assets.lyrics);
  setBtn(document.getElementById('btnManifest'), 'Manifest', assets.manifest);

  const cover = document.getElementById('cover');
  if(assets.cover && assets.cover.exists){
    cover.src = `/api/cover/${idx}?t=${Date.now()}`;
    cover.style.display = "block";
  } else {
    cover.style.display = "none";
    cover.src = "";
  }

  // Audio source (browser playback)
  audio.src = `/api/audio/${idx}?t=${Date.now()}`;
  if(autoplay){
    try { await audio.play(); } catch(e) {}
  }

  updateActive();
}

async function prev(autoplay=false){
  if(!tracks.length) return;
  const i = (idx - 1 + tracks.length) % tracks.length;
  await setIndex(i, autoplay);
}

async function next(autoplay=false){
  if(!tracks.length) return;
  const i = (idx + 1) % tracks.length;
  await setIndex(i, autoplay);
}

async function uploadPicked(){
  const files1 = document.getElementById('filePick').files;
  const files2 = document.getElementById('folderPick').files;

  const all = [];
  for (const f of files1) all.push(f);
  for (const f of files2) all.push(f);

  const onlyAifm = all.filter(f => (f.name || "").toLowerCase().endsWith(".aifm"));
  if(!onlyAifm.length){
    alert("Pick at least one .aifm file (or a folder containing .aifm files).");
    return;
  }

  const fd = new FormData();
  for(const f of onlyAifm){
    fd.append("files", f, f.name);
  }

  const res = await fetch("/api/load_upload", { method:"POST", body: fd });
  const data = await res.json();
  if(!data.ok){
    alert(data.error || "Load failed");
    return;
  }

  document.getElementById('filePick').value = "";
  document.getElementById('folderPick').value = "";

  await bootstrap(true);
}

async function clearPlaylist(){
  await fetch("/api/clear", { method:"POST" });
  tracks = [];
  idx = 0;
  audio.pause();
  audio.src = "";
  document.getElementById('title').textContent = "No playlist loaded";
  document.getElementById('metaLine').textContent = "";
  document.getElementById('verifyBadge').textContent = "VERIFY: —";
  document.getElementById('cover').style.display = "none";
  document.getElementById('cover').src = "";
  renderList();
}

async function bootstrap(autoplayFirst){
  await fetchTracks();
  renderList();
  if(tracks.length){
    await setIndex(0, autoplayFirst);
  }
}

bootstrap(false);
</script>
</body>
</html>
"""


# ---------------- Flask app ----------------

def create_app(state: PaionState) -> Flask:
    app = Flask(__name__)
    UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="paion_uploads_"))

    @app.get("/")
    def home():
        return Response(HTML, mimetype="text/html")

    @app.get("/api/tracks")
    def api_tracks():
        out = [{"title": p.stem, "path": str(p)} for p in state.tracks]
        return jsonify({"tracks": out, "playlist_version": state.playlist_version})

    @app.get("/api/track_info/<int:index>")
    def api_track_info(index: int):
        if not state.tracks:
            return jsonify({"error": "no tracks"}), 404
        index = clamp(index, 0, len(state.tracks) - 1)
        aifm = state.tracks[index]
        info = state.get_track_info(aifm)
        return jsonify(info)

    @app.get("/api/cover/<int:index>")
    def api_cover(index: int):
        if not state.tracks:
            return ("", 404)

        index = clamp(index, 0, len(state.tracks) - 1)
        aifm = state.tracks[index]
        info = state.get_track_info(aifm)
        member = info.get("assets", {}).get("cover", {}).get("member") or ""
        if not member:
            return ("", 404)

        try:
            with zipfile.ZipFile(aifm, "r") as z:
                data = z.read(member)
                return send_file(BytesIO(data), mimetype=guess_mimetype(member), download_name=Path(member).name)
        except Exception:
            return ("", 404)

    @app.get("/api/audio/<int:index>")
    def api_audio(index: int):
        if not state.tracks:
            return ("", 404)

        index = clamp(index, 0, len(state.tracks) - 1)
        aifm = state.tracks[index]

        try:
            audio_path = state.payload_cache.get_audio_file(aifm)
            return send_file(
                str(audio_path),
                mimetype=guess_mimetype(audio_path.name),
                download_name=audio_path.name,
                as_attachment=False
            )
        except Exception as e:
            return (f"audio error: {e}", 500)

    @app.get("/api/asset/<int:index>/<kind>")
    def api_asset(index: int, kind: str):
        if not state.tracks:
            return ("", 404)

        kind = (kind or "").lower().strip()
        if kind not in ("declaration", "prompt", "lyrics", "manifest"):
            return ("", 404)

        index = clamp(index, 0, len(state.tracks) - 1)
        aifm = state.tracks[index]
        info = state.get_track_info(aifm)
        asset = info.get("assets", {}).get(kind, {})
        member = asset.get("member") if isinstance(asset, dict) else ""
        if not member:
            return ("", 404)

        try:
            with zipfile.ZipFile(aifm, "r") as z:
                data = z.read(member)
                mt = guess_mimetype(member)
                return send_file(BytesIO(data), mimetype=mt, download_name=Path(member).name, as_attachment=False)
        except Exception:
            return ("", 404)

    @app.post("/api/load_upload")
    def api_load_upload():
        files = request.files.getlist("files")
        if not files:
            return jsonify({"ok": False, "error": "No files received"}), 400

        saved: list[Path] = []
        for f in files:
            name = secure_filename(f.filename or "")
            if not name.lower().endswith(".aifm"):
                continue
            out = UPLOAD_DIR / name
            f.save(out)
            saved.append(out)

        if not saved:
            return jsonify({"ok": False, "error": "No .aifm files uploaded"}), 400

        saved = sorted(saved)
        state.load_tracks(saved)
        return jsonify({"ok": True, "count": len(saved)})

    @app.post("/api/clear")
    def api_clear():
        state.clear()
        return jsonify({"ok": True})

    return app


# ---------------- Browser open ----------------

def open_browser(host: str, port: int):
    url = f"http://{host}:{port}"
    try:
        webbrowser.open_new_tab(url)
    except Exception:
        pass


# ---------------- Main ----------------

def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--no-browser", action="store_true",
                    help="Do not auto-open the browser")
    args = ap.parse_args()

    state = PaionState()
    app = create_app(state)

    if not args.no_browser:
        threading.Timer(0.8, open_browser, args=(args.host, args.port)).start()

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
