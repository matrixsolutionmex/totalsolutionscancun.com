"""Small, dependency-free upload validation primitives.

The checks here are format validation and resource guards, not antivirus scanning.
They intentionally do not trust the browser supplied MIME type or filename alone.
"""

from __future__ import annotations

import io
import os
import re
import struct
import zipfile
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from starlette.responses import JSONResponse


MAX_IMAGE_PIXELS = 40_000_000
READ_CHUNK_BYTES = 256 * 1024
MULTIPART_OVERHEAD_BYTES = 2 * 1024 * 1024


def normalized_content_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def upload_body_limit(path: str, method: str) -> int | None:
    if method.upper() != "POST":
        return None
    exact = {
        "/public/professional-applications": 25 * 1024 * 1024,
        "/public/service-requests": 50 * 1024 * 1024,
        "/customer-portal/me/service-requests": 50 * 1024 * 1024,
        "/pablo/transcribe": 10 * 1024 * 1024,
        "/pablo/vision/analyze": 10 * 1024 * 1024,
        "/pablo/actions/evidence": 50 * 1024 * 1024,
    }
    payload_limit = exact.get(path)
    if payload_limit is None and re.fullmatch(r"/leads/\d+/documents", path):
        payload_limit = 50 * 1024 * 1024
    if payload_limit is None and re.fullmatch(r"/users/\d+/profile-photo", path):
        payload_limit = 5 * 1024 * 1024
    return payload_limit + MULTIPART_OVERHEAD_BYTES if payload_limit is not None else None


class _BodyTooLarge(Exception):
    pass


class UploadBodyLimitMiddleware:
    """Bound multipart request bytes before Starlette finishes form parsing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        limit = upload_body_limit(scope.get("path", ""), scope.get("method", ""))
        if limit is None:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = 0
        if declared > limit:
            await JSONResponse({"detail": "Requisicao excede o tamanho maximo permitido"}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await JSONResponse({"detail": "Requisicao excede o tamanho maximo permitido"}, status_code=413)(scope, receive, send)


def safe_client_filename(value: str | None, default: str = "arquivo") -> str:
    # Path.name on POSIX does not treat a Windows backslash as a separator.
    name = (value or default).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    return name[:160] or default


def read_limited(file_obj, maximum: int) -> bytes:
    """Read at most maximum+1 bytes, independent of Content-Length."""
    chunks: list[bytes] = []
    total = 0
    while total <= maximum:
        chunk = file_obj.read(min(READ_CHUNK_BYTES, maximum + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > maximum:
        raise HTTPException(status_code=413, detail="Arquivo excede o tamanho maximo permitido")
    return b"".join(chunks)


async def read_upload_limited(upload, maximum: int) -> bytes:
    data = await upload.read(maximum + 1)
    if len(data) > maximum:
        raise HTTPException(status_code=413, detail="Arquivo excede o tamanho maximo permitido")
    return data


def _valid_png(data: bytes) -> bool:
    if len(data) < 33 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    if data[12:16] != b"IHDR" or b"IEND" not in data[-32:]:
        return False
    width, height = struct.unpack(">II", data[16:24])
    return width > 0 and height > 0 and width * height <= MAX_IMAGE_PIXELS


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
        return None
    offset = 2
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            return None
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            return None
        if marker in sof and length >= 7:
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            return width, height
        offset += length
    return None


def _valid_jpeg(data: bytes) -> bool:
    dimensions = _jpeg_dimensions(data)
    return bool(dimensions and dimensions[0] > 0 and dimensions[1] > 0 and dimensions[0] * dimensions[1] <= MAX_IMAGE_PIXELS)


def _valid_webp(data: bytes) -> bool:
    if len(data) < 16 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False
    declared = int.from_bytes(data[4:8], "little") + 8
    return declared <= len(data) and data[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}


def _valid_pdf(data: bytes) -> bool:
    if len(data) < 12 or not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
        return False
    # These features can launch code, submit data, or embed another payload.
    active_tokens = (b"/JavaScript", b"/JS", b"/Launch", b"/OpenAction", b"/EmbeddedFile", b"/RichMedia")
    return not any(token in data for token in active_tokens)


def _valid_iso_media(data: bytes) -> bool:
    return (
        len(data) >= 16
        and data[4:8] == b"ftyp"
        and 8 <= int.from_bytes(data[:4], "big") <= len(data)
    )


def _valid_zip_office(data: bytes, kind: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if archive.testzip() is not None or "[Content_Types].xml" not in names:
                return False
            prefix = "word/" if kind == "docx" else "xl/"
            return any(name.startswith(prefix) for name in names)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return False


def content_matches(content: bytes, content_type: str | None, suffix: str) -> bool:
    mime = normalized_content_type(content_type)
    suffix = suffix.lower()
    if not content:
        return False
    if mime == "image/png" and suffix == ".png":
        return _valid_png(content)
    if mime == "image/jpeg" and suffix in {".jpg", ".jpeg"}:
        return _valid_jpeg(content)
    if mime == "image/webp" and suffix == ".webp":
        return _valid_webp(content)
    if mime == "application/pdf" and suffix == ".pdf":
        return _valid_pdf(content)
    if mime in {"video/mp4", "video/quicktime", "audio/mp4"} and suffix in {".mp4", ".mov", ".m4a"}:
        return _valid_iso_media(content) and any(marker in content[12:] for marker in (b"moov", b"mdat", b"moof"))
    if mime == "video/webm" and suffix == ".webm":
        return content.startswith(b"\x1aE\xdf\xa3")
    if mime == "audio/webm" and suffix == ".webm":
        return content.startswith(b"\x1aE\xdf\xa3")
    if mime == "audio/wav" and suffix == ".wav":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WAVE"
    if mime == "audio/mpeg" and suffix == ".mp3":
        return content.startswith(b"ID3") or (len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0)
    if mime in {"image/heic", "image/heif"} and suffix in {".heic", ".heif"}:
        return _valid_iso_media(content) and content[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" and suffix == ".docx":
        return _valid_zip_office(content, "docx")
    if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" and suffix == ".xlsx":
        return _valid_zip_office(content, "xlsx")
    if mime == "application/msword" and suffix == ".doc":
        return content.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
    return False


def validate_upload_content(content: bytes, content_type: str | None, filename: str | None) -> tuple[str, str]:
    safe_name = safe_client_filename(filename)
    suffix = Path(safe_name).suffix.lower()
    if not content_matches(content, content_type, suffix):
        raise HTTPException(status_code=415, detail="Conteudo de arquivo invalido")
    return safe_name, suffix


def atomic_write(destination: Path, content: bytes) -> None:
    """Publish a complete new file without exposing a partial write."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid4().hex}.part"
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link publication is atomic and refuses a collision instead of
        # replacing an existing customer file.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def stage_file_deletion(path: Path) -> Path | None:
    """Hide a file before a DB delete while keeping rollback possible."""
    if not path.is_file():
        return None
    staged = path.parent / f".{path.name}.{uuid4().hex}.deleting"
    path.replace(staged)
    return staged


def rollback_file_deletion(staged: Path | None, original: Path) -> None:
    if staged is not None and staged.exists():
        staged.replace(original)
