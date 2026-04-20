"""Image upload backend for markdown embedding.

Design notes:
  * Accepts only common web image formats (jpg, jpeg, png, gif, webp).
  * Max payload 10 MB (see MAX_IMAGE_SIZE).
  * Validates with Pillow — file must round-trip through Image.verify() then
    Image.open() so we are sure it is a real image (not a renamed binary or
    a polyglot). Any exception → 400.
  * Stored filename is UUID-based to prevent path traversal / name collisions;
    the original extension is re-derived from the verified image format.
  * Rate limit: per-user, in-memory sliding window. On a single-process
    Daphne / gunicorn-sync worker (e.g. PythonAnywhere free tier) this is
    correct; if multiple workers ever run, each has its own counter, which
    is acceptable as a deterrent rather than a hard security boundary.
"""

from __future__ import annotations

import io
import time
import uuid
from collections import deque
from threading import Lock

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile

try:
    from PIL import Image  # Pillow
except ImportError:  # pragma: no cover — Pillow is required
    Image = None


ALLOWED_IMAGE_FORMATS = {
    # Pillow format name → extension we will store under.
    "JPEG": "jpg",
    "PNG":  "png",
    "GIF":  "gif",
    "WEBP": "webp",
}

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB

# Rate-limit: up to N uploads per user per window, where window is seconds.
RATE_LIMIT_MAX_PER_WINDOW = 20
RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 hour

# Separate (looser) budget for the preview endpoint: a user composing a
# message naturally clicks Preview many times. The goal is only to stop
# accidental or malicious tight loops from burning CPU on markdown +
# BeautifulSoup.
PREVIEW_RATE_LIMIT_MAX_PER_WINDOW = 120
PREVIEW_RATE_LIMIT_WINDOW_SECONDS = 60  # 1 minute

# Video attachment rate limit — tuned for ~230 concurrent users on a small
# PythonAnywhere plan. Each 40 MB video is heavy on bandwidth and disk, so
# we keep the per-user budget modest. A short per-user cooldown also stops
# accidental double-submits.
VIDEO_RATE_LIMIT_MAX_PER_WINDOW = 15
VIDEO_RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 hour
VIDEO_MIN_INTERVAL_SECONDS = 5  # minimum gap between two video uploads

# Housekeeping: every N limit checks we drop entries for users who haven't
# uploaded anything within the last window. Keeps _rate_state bounded
# regardless of how many distinct users touch the endpoint.
_RATE_GC_EVERY = 128
_rate_state: dict[int, deque] = {}
_rate_lock = Lock()
_rate_check_count = 0

_preview_state: dict[int, deque] = {}
_preview_lock = Lock()
_preview_check_count = 0

_video_state: dict[int, deque] = {}
_video_lock = Lock()


class ImageUploadError(Exception):
    """User-facing error while handling an uploaded image."""


class PreviewRateLimitError(Exception):
    """User-facing error when preview is called too often."""


class VideoUploadRateLimitError(Exception):
    """User-facing error when a user uploads videos too frequently."""


def _enforce_generic_limit(state, lock, counter_ref, max_per_window,
                           window_seconds, user_id, error_cls, message):
    """Shared sliding-window limiter used by uploads and preview."""
    now = time.monotonic()
    cutoff = now - window_seconds
    with lock:
        # Periodic housekeeping — prevents state from growing without bound
        # as new users arrive.
        counter_ref[0] += 1
        if counter_ref[0] % _RATE_GC_EVERY == 0:
            for uid in [u for u, q in state.items()
                        if not q or q[-1] < cutoff]:
                state.pop(uid, None)

        q = state.setdefault(user_id, deque())
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= max_per_window:
            raise error_cls(message)
        q.append(now)


# Counters need to be mutable from within the shared helper.
_upload_counter = [0]
_preview_counter = [0]


def _enforce_rate_limit(user_id: int) -> None:
    _enforce_generic_limit(
        _rate_state, _rate_lock, _upload_counter,
        RATE_LIMIT_MAX_PER_WINDOW, RATE_LIMIT_WINDOW_SECONDS,
        user_id, ImageUploadError,
        f"Bạn đã upload quá {RATE_LIMIT_MAX_PER_WINDOW} ảnh trong 1 giờ qua. "
        "Vui lòng thử lại sau.",
    )


def enforce_preview_rate_limit(user_id: int) -> None:
    """Called by the preview endpoint to cap HTML-rendering frequency."""
    _enforce_generic_limit(
        _preview_state, _preview_lock, _preview_counter,
        PREVIEW_RATE_LIMIT_MAX_PER_WINDOW, PREVIEW_RATE_LIMIT_WINDOW_SECONDS,
        user_id, PreviewRateLimitError,
        "Bạn đang preview quá nhiều lần. Vui lòng chờ giây lát.",
    )


def enforce_video_rate_limit(user_id: int) -> None:
    """Per-user rate limit for video attachments.

    Two complementary rules:
      • a sliding window of VIDEO_RATE_LIMIT_MAX_PER_WINDOW uploads per
        VIDEO_RATE_LIMIT_WINDOW_SECONDS (absolute ceiling), and
      • a small VIDEO_MIN_INTERVAL_SECONDS floor so that accidental double
        clicks / double submissions can't both go through.
    """
    now = time.monotonic()
    cutoff = now - VIDEO_RATE_LIMIT_WINDOW_SECONDS
    with _video_lock:
        q = _video_state.setdefault(user_id, deque())
        while q and q[0] < cutoff:
            q.popleft()

        if q and (now - q[-1]) < VIDEO_MIN_INTERVAL_SECONDS:
            wait = int(VIDEO_MIN_INTERVAL_SECONDS - (now - q[-1])) + 1
            raise VideoUploadRateLimitError(
                f"Vui lòng chờ {wait}s trước khi upload video kế tiếp."
            )

        if len(q) >= VIDEO_RATE_LIMIT_MAX_PER_WINDOW:
            raise VideoUploadRateLimitError(
                f"Bạn đã upload quá {VIDEO_RATE_LIMIT_MAX_PER_WINDOW} video "
                "trong 1 giờ qua. Vui lòng thử lại sau."
            )
        q.append(now)


def _validate_and_normalise(file_obj) -> tuple[bytes, str]:
    """Return (image_bytes, extension) if the upload is a valid image.

    The returned bytes are RE-ENCODED by Pillow so that any EXIF metadata
    (including GPS coordinates that may have been captured by a phone
    camera) is stripped. This protects supervisors and candidates from
    inadvertently leaking location data in uploaded evidence.

    Raises ImageUploadError with a user-friendly message on any problem.
    """
    if Image is None:
        raise ImageUploadError("Image processing library is not installed on the server.")

    # 1. Size check, relying on UploadedFile.size (Django sets this from
    # Content-Length / the in-memory/tmpfile size; we don't trust client
    # headers but do trust Django here).
    if file_obj.size > MAX_IMAGE_SIZE:
        raise ImageUploadError(
            f"Ảnh quá lớn: tối đa {MAX_IMAGE_SIZE // (1024 * 1024)} MB."
        )
    if file_obj.size <= 0:
        raise ImageUploadError("File ảnh rỗng.")

    # 2. Read bytes once, use twice (Pillow.verify destroys the object so we
    # re-open for a real decode).
    raw = file_obj.read()
    if len(raw) > MAX_IMAGE_SIZE:
        raise ImageUploadError("Ảnh vượt quá giới hạn 10 MB.")

    # 3. verify() — lightweight header check
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
    except Exception:
        raise ImageUploadError("File không phải là ảnh hợp lệ.")

    # 4. Full open() — catches some polyglots that verify() misses.
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise ImageUploadError("Ảnh bị hỏng hoặc định dạng không hỗ trợ.")

    fmt = (img.format or "").upper()
    if fmt not in ALLOWED_IMAGE_FORMATS:
        raise ImageUploadError(
            "Chỉ chấp nhận định dạng JPG / PNG / GIF / WebP."
        )

    # 5. Re-encode to drop EXIF/GPS/metadata. We use the same detected
    # format so lossless images stay lossless; JPEGs are saved at high
    # quality to avoid visible re-compression.
    out = io.BytesIO()
    save_kwargs = {}
    if fmt == "JPEG":
        # Re-save without the exif kwarg → EXIF is dropped. Keep quality
        # high since this is evidence material, not a thumbnail.
        save_kwargs = {"quality": 90, "optimize": True}
        # JPEG does not support alpha; if the image has one, flatten to RGB.
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
    elif fmt == "PNG":
        save_kwargs = {"optimize": True}
    elif fmt == "GIF":
        # Preserve animation if present.
        save_kwargs = {"save_all": getattr(img, "is_animated", False)}
    elif fmt == "WEBP":
        save_kwargs = {"quality": 90}

    try:
        img.save(out, format=fmt, **save_kwargs)
    except Exception:
        raise ImageUploadError("Không thể xử lý ảnh này. Vui lòng thử ảnh khác.")

    return out.getvalue(), ALLOWED_IMAGE_FORMATS[fmt]


def save_uploaded_image(file_obj, user_id: int) -> str:
    """Persist an uploaded image and return its media URL.

    The caller must supply user_id for rate limiting. No trust is placed in
    the filename from the client.
    """
    _enforce_rate_limit(user_id)

    raw, ext = _validate_and_normalise(file_obj)

    # Date-partitioned path so a single directory never grows unbounded.
    today = time.gmtime()
    rel_path = (
        f"incident-images/{today.tm_year:04d}/{today.tm_mon:02d}/"
        f"{today.tm_mday:02d}/{uuid.uuid4().hex}.{ext}"
    )

    saved_name = default_storage.save(rel_path, ContentFile(raw))
    # default_storage may rename on collision (appends _<suffix>); always use
    # what it actually stored.
    return default_storage.url(saved_name)
