import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", PROJECT_ROOT / "uploads")).expanduser().resolve()
PROFILE_PHOTOS_DIR = UPLOADS_DIR / "profile_photos"

PROFILE_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)


def delete_profile_photo(photo_url: str | None):
    if not photo_url or not photo_url.startswith("/uploads/profile_photos/"):
        return

    photo_path = (PROFILE_PHOTOS_DIR / Path(photo_url).name).resolve()
    if photo_path.parent != PROFILE_PHOTOS_DIR.resolve():
        return

    photo_path.unlink(missing_ok=True)
