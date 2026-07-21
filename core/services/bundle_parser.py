"""Safe ZIP bundle parsing for file-enabled test-case uploads."""

from __future__ import annotations

import hashlib
import posixpath
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

from django.conf import settings

from core.services.csv_parser import parse_upload


ALLOWED_MIME_TYPES = {
    "text/plain",
    "text/csv",
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}
MANIFEST_SUFFIXES = {".csv", ".xlsx", ".xls"}


class BundleValidationError(ValueError):
    """A bundle failed validation; ``errors`` is suitable for the upload UI."""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class BundleAttachment:
    """An allowed, referenced bundle entry ready for private storage."""

    relative_path: str
    content: bytes
    mime_type: str
    sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.content)


def _normalise_path(value: str) -> str:
    """Return a safe ZIP-root-relative POSIX path or raise ``ValueError``."""
    value = (value or "").strip().replace("\\", "/")
    if not value or value.startswith("/"):
        raise ValueError("must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("must not contain '.', '..', or an absolute path")
    normalised = posixpath.normpath(str(path))
    if normalised in ("", ".") or normalised.startswith("../"):
        raise ValueError("must stay within the ZIP root")
    return normalised


def _mime_type(path: str, content: bytes) -> str | None:
    """Identify supported types by extension and inexpensive content checks."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".pdf":
        return "application/pdf" if content.startswith(b"%PDF-") else None
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg" if content.startswith(b"\xff\xd8\xff") else None
    if suffix == ".png":
        return "image/png" if content.startswith(b"\x89PNG\r\n\x1a\n") else None
    if suffix == ".gif":
        return "image/gif" if content.startswith((b"GIF87a", b"GIF89a")) else None
    if suffix == ".webp":
        return "image/webp" if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP" else None
    if suffix == ".csv":
        return "text/csv" if _is_text(content) else None
    if suffix in {".txt", ".md"}:
        return "text/plain" if _is_text(content) else None
    return None


def _is_text(content: bytes) -> bool:
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return b"\x00" not in content


def _limits() -> tuple[int, int, int]:
    """Return entry count, entry size, and total uncompressed byte limits."""
    return (
        int(getattr(settings, "BUNDLE_MAX_FILES", 500)),
        int(getattr(settings, "BUNDLE_MAX_FILE_BYTES", 25 * 1024 * 1024)),
        int(getattr(settings, "BUNDLE_MAX_TOTAL_BYTES", 100 * 1024 * 1024)),
    )


def _zip_entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    max_files, max_file_bytes, max_total_bytes = _limits()
    errors: list[str] = []
    entries: dict[str, zipfile.ZipInfo] = {}
    total_bytes = 0

    infos = [info for info in archive.infolist() if not info.is_dir()]
    if len(infos) > max_files:
        errors.append(f"ZIP contains {len(infos)} files; the limit is {max_files}.")

    for info in infos:
        try:
            path = _normalise_path(info.filename)
        except ValueError as exc:
            errors.append(f"Unsafe ZIP path '{info.filename}': {exc}.")
            continue
        is_symlink = (info.external_attr >> 16) & 0o170000 == 0o120000
        if is_symlink:
            errors.append(f"ZIP entry '{path}' is a symbolic link.")
        if info.flag_bits & 0x1:
            errors.append(f"ZIP entry '{path}' is encrypted.")
        if info.file_size > max_file_bytes:
            errors.append(f"ZIP entry '{path}' exceeds the {max_file_bytes} byte file limit.")
        total_bytes += info.file_size
        if info.compress_size and info.file_size / info.compress_size > 100:
            errors.append(f"ZIP entry '{path}' has a suspicious compression ratio.")
        if path in entries:
            errors.append(f"ZIP contains duplicate path '{path}'.")
        entries[path] = info

    if total_bytes > max_total_bytes:
        errors.append(f"ZIP expands to {total_bytes} bytes; the limit is {max_total_bytes}.")
    if errors:
        raise BundleValidationError(errors)
    return entries


def parse_bundle(
    content: bytes,
    filename: str,
    group_by_columns: list[str] | None = None,
    sort_by_column: str | None = None,
) -> tuple[dict[str, Any], list[BundleAttachment]]:
    """
    Parse one ZIP bundle into a normal parsed manifest and referenced attachments.

    Unsupported entries that are not referenced by a ``file_*`` cell are ignored.
    Referenced unsupported entries make the whole import invalid.
    """
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            entries = _zip_entries(archive)
            manifests = [
                path for path in entries
                if PurePosixPath(path).suffix.lower() in MANIFEST_SUFFIXES
            ]
            if len(manifests) != 1:
                raise BundleValidationError([
                    "ZIP must contain exactly one CSV or Excel manifest; "
                    f"found {len(manifests)}."
                ])

            manifest_path = manifests[0]
            parsed = parse_upload(
                archive.read(entries[manifest_path]),
                manifest_path,
                group_by_columns=group_by_columns,
                sort_by_column=sort_by_column,
            )
            parsed["original_filename"] = filename

            errors: list[str] = []
            requested_paths: set[str] = set()
            for row in parsed["rows"]:
                for column, raw_value in row.get("file_fields", {}).items():
                    values = raw_value if isinstance(raw_value, list) else [raw_value]
                    normalised_values: list[str] = []
                    for value in values:
                        try:
                            path = _normalise_path(str(value))
                        except ValueError as exc:
                            errors.append(
                                f"Row {row['row_number']}, column '{column}': "
                                f"'{value}' {exc}."
                            )
                            continue
                        if path not in entries:
                            errors.append(
                                f"Row {row['row_number']}, column '{column}': "
                                f"'{path}' was not found in the ZIP."
                            )
                            continue
                        requested_paths.add(path)
                        normalised_values.append(path)
                    row["file_fields"][column] = (
                        normalised_values if isinstance(raw_value, list) else
                        (normalised_values[0] if normalised_values else "")
                    )

            attachments: list[BundleAttachment] = []
            for path in sorted(requested_paths):
                file_content = archive.read(entries[path])
                mime_type = _mime_type(path, file_content)
                if mime_type not in ALLOWED_MIME_TYPES:
                    errors.append(
                        f"Referenced file '{path}' is not an allowed plain text, CSV, "
                        "PDF, or image attachment (or its content does not match its extension)."
                    )
                    continue
                attachments.append(BundleAttachment(
                    relative_path=path,
                    content=file_content,
                    mime_type=mime_type,
                    sha256=hashlib.sha256(file_content).hexdigest(),
                ))

            if errors:
                raise BundleValidationError(errors)
            return parsed, attachments
    except zipfile.BadZipFile as exc:
        raise BundleValidationError(["Upload is not a valid ZIP archive."]) from exc
