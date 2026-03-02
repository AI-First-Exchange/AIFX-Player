from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Sequence
import zipfile


PackageType = Literal["aifm", "aifv", "aifi", "aifp"]


class SafeOpenError(Exception):
    pass


class UnsupportedPackageTypeError(SafeOpenError):
    pass


class InvalidArchiveError(SafeOpenError):
    pass


class UnsafeArchivePathError(SafeOpenError):
    pass


class SymlinkEntryNotAllowedError(SafeOpenError):
    pass


class MissingManifestError(SafeOpenError):
    pass


class PrimaryMediaNotFoundError(SafeOpenError):
    pass


class MultiplePrimaryMediaError(SafeOpenError):
    pass


@dataclass(frozen=True)
class SafeOpenResult:
    package_path: Path
    package_type: PackageType
    manifest_path: str
    manifest_bytes: bytes
    primary_media_path: str | None
    primary_media_bytes: bytes | None
    file_paths: tuple[str, ...]


_PRIMARY_MEDIA_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "aifm": (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"),
    "aifv": (".mp4", ".mov", ".mkv", ".webm", ".m4v"),
    "aifi": (".png", ".jpg", ".jpeg", ".webp"),
}


def _detect_package_type(path: Path) -> PackageType:
    suffix = path.suffix.lower()
    if suffix == ".aifm":
        return "aifm"
    if suffix == ".aifv":
        return "aifv"
    if suffix == ".aifi":
        return "aifi"
    if suffix == ".aifp":
        return "aifp"
    raise UnsupportedPackageTypeError(f"Unsupported package extension: {suffix or '<none>'}")


def _normalize_member_path(raw_name: str) -> str:
    normalized = raw_name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _validate_member_safety(info: zipfile.ZipInfo) -> None:
    normalized = _normalize_member_path(info.filename)
    if not normalized:
        raise UnsafeArchivePathError(f"Unsafe archive member path: {info.filename!r}")

    if normalized.startswith("/"):
        raise UnsafeArchivePathError(f"Unsafe archive member path: {info.filename!r}")

    if len(normalized) >= 2 and normalized[1] == ":":
        raise UnsafeArchivePathError(f"Unsafe archive member path: {info.filename!r}")

    if ".." in PurePosixPath(normalized).parts:
        raise UnsafeArchivePathError(f"Unsafe archive member path: {info.filename!r}")

    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise SymlinkEntryNotAllowedError(f"Symlink entry not allowed: {info.filename!r}")


def _collect_file_paths(zf: zipfile.ZipFile) -> tuple[str, ...]:
    file_paths: list[str] = []
    for info in zf.infolist():
        _validate_member_safety(info)
        if not info.is_dir():
            file_paths.append(_normalize_member_path(info.filename))
    return tuple(file_paths)


def _find_manifest_path(file_paths: Sequence[str]) -> str:
    if "manifest.json" in file_paths:
        return "manifest.json"
    raise MissingManifestError("Missing required manifest.json at archive root")


def _select_primary_media_path(package_type: str, file_paths: Sequence[str]) -> str | None:
    if package_type == "aifp":
        return None

    allowed_exts = _PRIMARY_MEDIA_EXTENSIONS[package_type]
    candidates = [
        path
        for path in file_paths
        if (path.startswith("assets/") or path.startswith("payload/")) and Path(path).suffix.lower() in allowed_exts
    ]
    if not candidates:
        raise PrimaryMediaNotFoundError(
            f"No primary media found under assets/ for package type {package_type}"
        )
    if len(candidates) > 1:
        joined = ", ".join(candidates)
        raise MultiplePrimaryMediaError(
            f"Multiple primary media files found for package type {package_type}: {joined}"
        )
    return candidates[0]


def safe_open_package(package_path: str | Path) -> SafeOpenResult:
    path_obj = Path(package_path)
    package_type = _detect_package_type(path_obj)

    try:
        with zipfile.ZipFile(path_obj, "r") as zf:
            file_paths = _collect_file_paths(zf)
            manifest_path = _find_manifest_path(file_paths)
            primary_media_path = _select_primary_media_path(package_type, file_paths)

            # Keep reads minimal and in-memory only.
            normalized_to_raw: dict[str, str] = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                normalized = _normalize_member_path(info.filename)
                normalized_to_raw.setdefault(normalized, info.filename)

            try:
                manifest_bytes = zf.read(normalized_to_raw[manifest_path])
            except KeyError as exc:
                raise MissingManifestError("Missing required manifest.json at archive root") from exc

            primary_media_bytes: bytes | None = None
            if primary_media_path is not None:
                try:
                    primary_media_bytes = zf.read(normalized_to_raw[primary_media_path])
                except KeyError as exc:
                    raise PrimaryMediaNotFoundError(
                        f"No primary media found under assets/ for package type {package_type}"
                    ) from exc

    except UnsupportedPackageTypeError:
        raise
    except SafeOpenError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        raise InvalidArchiveError(f"Invalid or unreadable archive: {path_obj}") from exc

    return SafeOpenResult(
        package_path=path_obj,
        package_type=package_type,
        manifest_path=manifest_path,
        manifest_bytes=manifest_bytes,
        primary_media_path=primary_media_path,
        primary_media_bytes=primary_media_bytes,
        file_paths=file_paths,
    )

