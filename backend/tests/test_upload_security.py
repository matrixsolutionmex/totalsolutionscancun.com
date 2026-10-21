from io import BytesIO
import asyncio
import struct
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile
from unittest.mock import patch

from app.core.upload_security import (
    UploadBodyLimitMiddleware,
    atomic_write,
    content_matches,
    read_limited,
    rollback_file_deletion,
    safe_client_filename,
    stage_file_deletion,
    validate_upload_content,
    upload_body_limit,
)
from app.services import customer_portal_service
from app.routes import pablo_routes


def png(width=1, height=1):
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00IEND\x00\x00\x00\x00"
    )


def jpeg(width=1, height=1):
    return (
        b"\xff\xd8\xff\xc0\x00\x11\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00\xff\xd9"
    )


def office_zip(prefix):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(f"{prefix}/document.xml", "<document/>")
    return output.getvalue()


@pytest.mark.parametrize(
    ("data", "mime", "name"),
    [
        (b"<html><script>alert(1)</script>", "image/jpeg", "proof.jpg"),
        (b"%PDF-", "application/pdf", "proof.pdf"),
        (b"%PDF-1.4\n/OpenAction 1 0 R\n%%EOF", "application/pdf", "active.pdf"),
        (b"\x89PNG\r\n\x1a\n", "image/png", "proof.png"),
        (jpeg(), "image/jpeg", "proof.png"),
        (office_zip("word")[:-8], "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "proof.docx"),
    ],
)
def test_active_mismatched_and_truncated_content_is_rejected(data, mime, name):
    with pytest.raises(HTTPException) as blocked:
        validate_upload_content(data, mime, name)
    assert blocked.value.status_code == 415


def test_supported_synthetic_formats_require_matching_content_and_suffix():
    assert content_matches(png(), "image/png", ".png")
    assert content_matches(jpeg(), "image/jpeg", ".jpg")
    assert content_matches(b"%PDF-1.4\n%%EOF\n", "application/pdf", ".pdf")
    assert content_matches(office_zip("word"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx")
    assert content_matches(office_zip("xl"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx")
    assert content_matches(b"\x1aE\xdf\xa3synthetic", "audio/webm; codecs=opus", ".webm")


def test_excessive_image_dimensions_are_rejected_before_processing():
    assert not content_matches(png(100_000, 100_000), "image/png", ".png")
    assert not content_matches(jpeg(65535, 65535), "image/jpeg", ".jpg")


def test_client_filename_removes_both_path_separator_styles_and_controls():
    assert safe_client_filename("../../secret.pdf") == "secret.pdf"
    assert safe_client_filename(r"C:\\temp\\secret.pdf") == "secret.pdf"
    assert safe_client_filename("foto\x00.jpg") == "foto.jpg"
    assert safe_client_filename("evidência.final.jpg") == "evidência.final.jpg"


class ChunkedSource:
    def __init__(self, size):
        self.remaining = size
        self.requests = []

    def read(self, size=-1):
        self.requests.append(size)
        amount = min(self.remaining, size)
        self.remaining -= amount
        return b"x" * amount


def test_limit_is_applied_while_reading_without_content_length():
    source = ChunkedSource(1025)
    with pytest.raises(HTTPException) as blocked:
        read_limited(source, 1024)
    assert blocked.value.status_code == 413
    assert max(source.requests) <= 1025
    assert source.remaining == 0


def test_request_limits_cover_every_multipart_entry_point():
    paths = [
        "/public/professional-applications", "/public/service-requests",
        "/customer-portal/me/service-requests", "/leads/42/documents",
        "/users/9/profile-photo", "/pablo/transcribe", "/pablo/vision/analyze",
        "/pablo/actions/evidence",
    ]
    assert all(upload_body_limit(path, "POST") for path in paths)
    assert upload_body_limit("/health", "GET") is None


def test_request_limit_counts_streamed_chunks_without_content_length(monkeypatch):
    async def scenario():
        monkeypatch.setattr("app.core.upload_security.upload_body_limit", lambda *_args: 5)
        app_called = False

        async def downstream(_scope, receive, _send):
            nonlocal app_called
            app_called = True
            await receive()
            await receive()

        chunks = iter([
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ])
        sent = []

        async def receive():
            return next(chunks)

        async def send(message):
            sent.append(message)

        middleware = UploadBodyLimitMiddleware(downstream)
        await middleware({"type": "http", "method": "POST", "path": "/public/service-requests", "headers": []}, receive, send)
        assert app_called
        assert any(message.get("status") == 413 for message in sent)

    asyncio.run(scenario())


def test_atomic_write_refuses_collision_and_removes_temporary_file(tmp_path):
    destination = tmp_path / "same-name.pdf"
    atomic_write(destination, b"first")
    with pytest.raises(FileExistsError):
        atomic_write(destination, b"second")
    assert destination.read_bytes() == b"first"
    assert list(tmp_path.glob("*.part")) == []
    assert list(tmp_path.glob(".*.part")) == []


def test_concurrent_writes_never_overwrite_each_other(tmp_path):
    destination = tmp_path / "collision.pdf"

    def write(payload):
        try:
            atomic_write(destination, payload)
            return "stored"
        except FileExistsError:
            return "collision"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, (b"first", b"second")))
    assert sorted(results) == ["collision", "stored"]
    assert destination.read_bytes() in {b"first", b"second"}
    assert not list(tmp_path.glob(".*.part"))


def test_staged_delete_can_be_rolled_back_before_database_commit(tmp_path):
    original = tmp_path / "evidence.pdf"
    original.write_bytes(b"preserve-me")
    staged = stage_file_deletion(original)
    assert staged and staged.is_file() and not original.exists()
    rollback_file_deletion(staged, original)
    assert original.read_bytes() == b"preserve-me"


class Upload:
    def __init__(self, name, mime, data):
        self.filename = name
        self.content_type = mime
        self.file = BytesIO(data)


def test_portal_enforces_file_count_and_total_before_database_or_storage(monkeypatch):
    uploads = [Upload(f"{index}.png", "image/png", png()) for index in range(3)]
    monkeypatch.setattr(customer_portal_service, "MAX_UPLOAD_FILES", 2)
    with pytest.raises(HTTPException) as too_many:
        customer_portal_service._validate_uploads_before_persisting(uploads)
    assert too_many.value.status_code == 413

    monkeypatch.setattr(customer_portal_service, "MAX_UPLOAD_FILES", 8)
    monkeypatch.setattr(customer_portal_service, "MAX_TOTAL_UPLOAD_BYTES", len(png()) + 1)
    with pytest.raises(HTTPException) as too_large:
        customer_portal_service._validate_uploads_before_persisting(uploads[:2])
    assert too_large.value.status_code == 413


def test_pending_upload_cleanup_tracks_commit_and_rollback(tmp_path):
    class Database:
        info = {}

    db = Database()
    stored = tmp_path / "pending.pdf"
    stored.write_bytes(b"pending")
    db.info["pending_upload_paths"] = [stored]
    customer_portal_service.cleanup_pending_uploads(db)
    assert not stored.exists()
    assert "pending_upload_paths" not in db.info

    committed = tmp_path / "committed.pdf"
    committed.write_bytes(b"committed")
    db.info["pending_upload_paths"] = [committed]
    customer_portal_service.finalize_pending_uploads(db)
    assert committed.exists()


def test_pablo_rejects_invalid_media_before_calling_external_provider():
    audio = UploadFile(
        file=BytesIO(b"not audio"), filename="recording.webm",
        headers=Headers({"content-type": "audio/webm"}),
    )
    with patch.object(pablo_routes, "transcribe_audio") as transcribe:
        with pytest.raises(HTTPException) as invalid_audio:
            asyncio.run(pablo_routes.pablo_transcribe(audio, actor=object()))
    assert invalid_audio.value.status_code == 415
    transcribe.assert_not_called()

    image = UploadFile(
        file=BytesIO(b"\x89PNG\r\n\x1a\n"), filename="image.png",
        headers=Headers({"content-type": "image/png"}),
    )
    with patch.object(pablo_routes, "analyze_image") as analyze:
        with pytest.raises(HTTPException) as invalid_image:
            asyncio.run(pablo_routes.pablo_vision_analyze(image, actor=object()))
    assert invalid_image.value.status_code == 415
    analyze.assert_not_called()
