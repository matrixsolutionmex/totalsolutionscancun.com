import asyncio
from io import BytesIO
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers, UploadFile

from app.core.security import hash_password
from app.database.connection import Base
from app.models.user import User
from app.routes import user_routes


def test_profile_photo_upload_and_remove(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine, tables=[User.__table__])
    session = sessionmaker(bind=engine)()

    user = User(
        username="broker-photo",
        email="broker-photo@example.com",
        password_hash=hash_password("secret"),
        role="BROKER",
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    monkeypatch.setattr(user_routes, "PROFILE_PHOTOS_DIR", tmp_path)

    def delete_test_photo(photo_url):
        if photo_url:
            (tmp_path / Path(photo_url).name).unlink(missing_ok=True)

    monkeypatch.setattr(user_routes, "delete_profile_photo", delete_test_photo)

    photo = UploadFile(
        file=BytesIO(b"\x89PNG\r\n\x1a\nprofile-photo"),
        filename="avatar.png",
        headers=Headers({"content-type": "image/png"}),
    )

    updated = asyncio.run(user_routes.upload_profile_photo(user.id, photo, actor=user, db=session))

    assert updated.profile_photo_url.startswith("/uploads/profile_photos/")
    stored_photo = tmp_path / Path(updated.profile_photo_url).name
    assert stored_photo.exists()

    removed = user_routes.remove_profile_photo(user.id, actor=user, db=session)

    assert removed.profile_photo_url is None
    assert not stored_photo.exists()

    session.close()
