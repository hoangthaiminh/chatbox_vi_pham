"""Tests for ADD-2: image upload backend.

Covers:
  * save_uploaded_image() happy path for each allowed format.
  * Rejection of oversize files, non-image bytes, disallowed formats.
  * Rate-limit enforcement.
  * Endpoint auth/role gate.
  * Endpoint response shape for success + error cases.
"""

import io
import time

import pytest
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile

pytestmark = pytest.mark.django_db


# ─── Helpers ───────────────────────────────────────────────────────────────

def _png_bytes(size=(8, 8), colour=(200, 100, 50)):
    from PIL import Image
    img = Image.new("RGB", size, colour)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _gif_bytes():
    from PIL import Image
    img = Image.new("P", (4, 4), 0)
    buf = io.BytesIO()
    img.save(buf, format="GIF")
    return buf.getvalue()


def _jpeg_bytes():
    from PIL import Image
    img = Image.new("RGB", (8, 8), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _webp_bytes():
    from PIL import Image
    img = Image.new("RGB", (8, 8), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="WEBP")
    return buf.getvalue()


def _mk_upload(name, content, content_type="image/png"):
    return SimpleUploadedFile(name, content, content_type=content_type)


@pytest.fixture
def room_admin_user(db):
    user = User.objects.create_user(username="ra1", password="x")
    grp, _ = Group.objects.get_or_create(name="room_admin")
    user.groups.add(grp)
    return user


@pytest.fixture
def viewer_user(db):
    return User.objects.create_user(username="viewer1", password="x")


# ─── Unit tests for image_uploads module ───────────────────────────────────

class TestSaveUploadedImage:
    def test_png_roundtrip(self, room_admin_user, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations.image_uploads import save_uploaded_image
        up = _mk_upload("photo.png", _png_bytes())
        url = save_uploaded_image(up, user_id=room_admin_user.id)
        assert url.startswith("/media/incident-images/")
        assert url.endswith(".png")

    def test_exif_is_stripped(self, room_admin_user, settings, tmp_path):
        """Privacy: EXIF (incl. GPS) must be removed from saved JPEGs."""
        settings.MEDIA_ROOT = str(tmp_path)
        from PIL import Image
        from PIL.ExifTags import Base
        from violations.image_uploads import save_uploaded_image

        # Build a JPEG with a fake EXIF GPS tag.
        img = Image.new("RGB", (32, 32), (1, 2, 3))
        exif = img.getexif()
        # Tag 0x0110 = Model. Pillow may not allow GPS sub-IFD writes
        # easily, so we test with a regular EXIF tag — same code path.
        exif[Base.Model.value] = "FakePhoneCamera"
        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif.tobytes(), quality=90)
        original = buf.getvalue()
        assert b"FakePhoneCamera" in original  # sanity: tag present pre-upload

        up = _mk_upload("phone.jpg", original, "image/jpeg")
        url = save_uploaded_image(up, user_id=room_admin_user.id)

        # Read back the saved file and verify the tag is gone.
        from django.core.files.storage import default_storage
        rel = url.replace("/media/", "", 1)
        with default_storage.open(rel, "rb") as f:
            saved = f.read()
        assert b"FakePhoneCamera" not in saved

    def test_jpeg_roundtrip(self, room_admin_user, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations.image_uploads import save_uploaded_image
        up = _mk_upload("p.jpg", _jpeg_bytes(), "image/jpeg")
        assert save_uploaded_image(up, user_id=room_admin_user.id).endswith(".jpg")

    def test_gif_roundtrip(self, room_admin_user, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations.image_uploads import save_uploaded_image
        up = _mk_upload("p.gif", _gif_bytes(), "image/gif")
        assert save_uploaded_image(up, user_id=room_admin_user.id).endswith(".gif")

    def test_webp_roundtrip(self, room_admin_user, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations.image_uploads import save_uploaded_image
        up = _mk_upload("p.webp", _webp_bytes(), "image/webp")
        assert save_uploaded_image(up, user_id=room_admin_user.id).endswith(".webp")

    def test_rejects_non_image_bytes(self, room_admin_user, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations.image_uploads import ImageUploadError, save_uploaded_image
        up = _mk_upload("evil.png", b"not an image at all")
        with pytest.raises(ImageUploadError):
            save_uploaded_image(up, user_id=room_admin_user.id)

    def test_rejects_empty_file(self, room_admin_user, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations.image_uploads import ImageUploadError, save_uploaded_image
        up = _mk_upload("empty.png", b"")
        with pytest.raises(ImageUploadError):
            save_uploaded_image(up, user_id=room_admin_user.id)

    def test_rejects_oversize(self, room_admin_user, settings, tmp_path, monkeypatch):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations import image_uploads
        monkeypatch.setattr(image_uploads, "MAX_IMAGE_SIZE", 100)  # 100 bytes
        up = _mk_upload("big.png", _png_bytes((32, 32)))
        with pytest.raises(image_uploads.ImageUploadError):
            image_uploads.save_uploaded_image(up, user_id=room_admin_user.id)

    def test_filename_is_uuid_not_client_supplied(self, room_admin_user, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations.image_uploads import save_uploaded_image
        # Path-traversal attempt in filename must NOT survive.
        up = _mk_upload("../../etc/passwd.png", _png_bytes())
        url = save_uploaded_image(up, user_id=room_admin_user.id)
        assert "passwd" not in url
        assert ".." not in url


class TestRateLimit:
    def test_blocks_after_window_limit(self, room_admin_user, settings, tmp_path, monkeypatch):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations import image_uploads
        monkeypatch.setattr(image_uploads, "RATE_LIMIT_MAX_PER_WINDOW", 2)
        # Clear state for test isolation
        image_uploads._rate_state.clear()

        up1 = _mk_upload("a.png", _png_bytes())
        up2 = _mk_upload("b.png", _png_bytes())
        up3 = _mk_upload("c.png", _png_bytes())

        image_uploads.save_uploaded_image(up1, user_id=room_admin_user.id)
        image_uploads.save_uploaded_image(up2, user_id=room_admin_user.id)
        with pytest.raises(image_uploads.ImageUploadError):
            image_uploads.save_uploaded_image(up3, user_id=room_admin_user.id)

    def test_window_expiry_allows_new_uploads(self, room_admin_user, settings,
                                              tmp_path, monkeypatch):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations import image_uploads
        monkeypatch.setattr(image_uploads, "RATE_LIMIT_MAX_PER_WINDOW", 1)
        monkeypatch.setattr(image_uploads, "RATE_LIMIT_WINDOW_SECONDS", 0.01)
        image_uploads._rate_state.clear()

        image_uploads.save_uploaded_image(_mk_upload("a.png", _png_bytes()),
                                          user_id=room_admin_user.id)
        time.sleep(0.02)
        # After window expiry, should succeed again.
        image_uploads.save_uploaded_image(_mk_upload("b.png", _png_bytes()),
                                          user_id=room_admin_user.id)


# ─── Endpoint tests ────────────────────────────────────────────────────────

class TestUploadEndpoint:
    url = "/incidents/upload-image/"

    def test_requires_login(self, client):
        res = client.post(self.url)
        # login_required redirects to the login page
        assert res.status_code in (302, 403)

    def test_viewer_forbidden(self, client, viewer_user, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        client.force_login(viewer_user)
        up = _mk_upload("a.png", _png_bytes())
        res = client.post(self.url, {"image": up})
        assert res.status_code == 403

    def test_room_admin_success(self, client, room_admin_user, settings, tmp_path, monkeypatch):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations import image_uploads
        image_uploads._rate_state.clear()

        client.force_login(room_admin_user)
        up = _mk_upload("photo.png", _png_bytes())
        res = client.post(self.url, {"image": up})
        assert res.status_code == 200
        body = res.json()
        assert "url" in body
        assert body["url"].startswith("/media/incident-images/")

    def test_missing_file_returns_400(self, client, room_admin_user):
        client.force_login(room_admin_user)
        res = client.post(self.url, {})
        assert res.status_code == 400
        assert "error" in res.json()

    def test_invalid_image_returns_400(self, client, room_admin_user, settings, tmp_path):
        settings.MEDIA_ROOT = str(tmp_path)
        from violations import image_uploads
        image_uploads._rate_state.clear()

        client.force_login(room_admin_user)
        up = _mk_upload("bad.png", b"this is not a png")
        res = client.post(self.url, {"image": up})
        assert res.status_code == 400
        assert "error" in res.json()
