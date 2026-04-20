# CHANGELOG — Batch Fix #1

Toàn bộ thay đổi kể từ baseline. Đọc theo thứ tự commit từ cũ đến mới.

## Migrations cần chạy

Trên server:

```bash
python manage.py migrate
```

Có 2 migration mới:
- `0003_shrink_sbd_max_length_to_9` — giảm `max_length` của Candidate.sbd, Incident.reported_sbd, IncidentParticipant.sbd_snapshot về 9.
- `0004_add_sbd_indexes` — thêm `db_index` cho `reported_sbd` và `sbd_snapshot`.

**Pre-check dữ liệu cũ (nếu DB đã có bản ghi):** kiểm tra không có SBD nào dài hơn 9 ký tự trước khi chạy migrate:

```bash
python manage.py shell -c "from violations.models import *; print('Too long:', Candidate.objects.extra(where=['LENGTH(sbd) > 9']).count(), Incident.objects.extra(where=['LENGTH(reported_sbd) > 9']).count(), IncidentParticipant.objects.extra(where=['LENGTH(sbd_snapshot) > 9']).count())"
```

Tất cả phải là `0 0 0` trước khi migrate.

## Dependencies

`requirements.txt` bổ sung:
- `pymdown-extensions>=10.0` — hỗ trợ GFM strikethrough.
- `beautifulsoup4>=4.12` — DOM-aware mention context walker.
- `Pillow>=10.0` — validate + EXIF strip ảnh upload.

Cài bằng:
```bash
pip install -r requirements.txt
```

## Thay đổi theo task

### Task 1 — Xoá text hint thừa
- Bỏ `<p>` dưới form post ("Hỗ trợ Markdown — nhúng ảnh bằng...").
- Rút gọn placeholder textarea.

### Task 7 — SBD ≤ 9 ký tự (+ ADD-4 regex mới)
- `services.py`: `SBD_PATTERN` = 0–2 letters + ≥2 digits, max 9 chars (accept cả `7728`, `CT983`, `TS0032`). `MENTION_TOKEN_PATTERN` cap 9. `MAX_SBD_LENGTH = 9`.
- `models.py`: 3 field giảm `max_length=9`.
- `forms.py`: error message nêu rõ giới hạn 9.
- `views.py`: `_MAX_SBD_URL_LEN = 9`.
- `app.js`: regex tương ứng.
- Templates: `maxlength="9"` + `pattern="[A-Za-z0-9]{1,9}"` cho SBD input.

### Task 5 — Strikethrough GFM
- `render_violation` thêm extension `pymdownx.tilde` (subscript=False).
- `~~deleted~~` → `<del>deleted</del>`.
- Single `~` không còn bị hiểu nhầm.

### Task 3 + 6 — Mention context safety + missing state
- `render_violation` rewrite dùng BeautifulSoup walk DOM.
- Mention trong ancestor `<a>`, `<code>`, `<pre>` → **literal** `@{SBD}` escaped.
- Mention nằm trong **tag attribute** (src/href/alt/title của `[x](@{...})` hoặc `![x](@{...})`) → literal text, escape quote an toàn.
- Mention pending:
  - Invalid syntax → literal.
  - Valid + **có trong DB** → active link `mention-link js-open-candidate-detail`.
  - Valid + **không có trong DB** → disabled link `mention-link--missing` với `<s>SBD</s>`, `aria-disabled="true"`, `tabindex="-1"`, CSS `pointer-events:none` → không click được.
- Tạo `templatetags/__init__.py` (bug tiền sử).
- CSS mới `.mention-link--missing`: màu xám, gạch chéo, không clickable.

### ADD-1 — `<del>` conditional
- Nếu mention bên trong `~~...~~` sẽ là **active** → neutralise thành literal (tránh xung đột visual: link xanh + strike).
- Nếu mention sẽ là **missing** → giữ nguyên (đã strike sẵn).
- Nếu invalid → literal (như mọi context).

### Task 2 — Dropdown mention fix
- Bỏ `overflow: hidden` trên `.md-editor-wrap`.
- Bo góc con (`.md-toolbar` top, `.md-textarea` bottom) giữ look không đổi.
- Dropdown chuyển sang `position: fixed` với JS `positionMentionDropdown()`:
  - `z-index: 1060` (trên Bootstrap offcanvas).
  - Flip above/below theo available space.
  - Listener `scroll` (capture) + `resize` → reposition realtime.

### Task 4 — Shared card template + server-side preview
- `_incident_card.html` (router) + `_incident_card_body.html` (body dùng chung).
- 3 mode: `full` (sảnh chính), `mini` (candidate timeline), `preview` (đang soạn).
- `_incident_rows.html` và `_candidate_detail.html` delegate sang partial chung.
- Endpoint mới `POST /incidents/preview/`:
  - `@login_required` + `can_post_message` gate.
  - Build Incident in-memory (không save), resolve candidate bằng iexact.
  - Render `_incident_card.html` mode=preview → JSON `{html}`.
  - Dùng cùng `render_violation` filter → parity 100% với sảnh chính.
- JS `refreshPreview()` giờ AJAX server-side.
- Fallback `marked.js` + banner cảnh báo khi API lỗi.

### ADD-5 — Undo/Redo
- `insertTextAt()` dùng `document.execCommand('insertText')` → undo stack native hoạt động.
- `insertMarkdown`, link, image, mention đều đi qua pipeline này.
- 2 nút UI mới (`bi-arrow-counterclockwise`, `bi-arrow-clockwise`) đầu toolbar.
- Ctrl+Z / Ctrl+Y fall through đến native undo (không preventDefault).

### ADD-3 — LightGallery zoom
- Config thêm `zoom: true`, `showZoomInOutIcons: true`, `actualSize: true`, `scale: 1`, `enableZoomAfter: 300`.
- Giải quyết bug không zoom được ảnh nhỏ/GIF.

### ADD-2 — Image upload GitHub-style
- Backend `violations/image_uploads.py`:
  - Pillow verify + full load → reject polyglot.
  - Chấp nhận JPG/PNG/GIF/WebP, tối đa 10MB.
  - **Re-encode** → strip EXIF/GPS metadata.
  - UUID filename, path `incident-images/YYYY/MM/DD/`.
  - Rate limit 20 uploads/giờ/user (sliding window deque + Lock), có GC dọn dead entries.
- Endpoint `POST /incidents/upload-image/`:
  - `@login_required` + `can_post_message` gate.
  - JSON response: 200 `{url}`, 400 `{error}`, 403, 500 (không leak).
- Frontend:
  - Nút toolbar `bi-cloud-upload`.
  - Ctrl+V clipboard image.
  - Drag & drop.
  - Placeholder `![Uploading {name}…]()` → replace `![{alt}]({url})` khi xong.
  - Alt = text bôi đen hoặc filename.
  - Upload fail → placeholder thành `![Upload failed]()` + toast.
- `base.html`: toast container luôn render (để JS gắn vào).
- `chatbox_vi_pham/urls.py`: serve MEDIA_URL trong DEBUG.

## Audit fixes (commit cuối)

### BUG #1 — N+1 trong candidate_detail
- `select_related("reported_candidate")` cho incidents query.

### BUG #2 — Preview crash với evidence
- Guard `{% if incident.pk %}` trong `_incident_card_body.html`.
- Preview mode show info note thay vì crash `NoReverseMatch`.

### BUG #3 — Rate state memory leak
- Periodic GC mỗi 128 lần check, dọn user có entries hết hạn.

### BUG #4 — Preview endpoint không rate-limit
- `PREVIEW_RATE_LIMIT_MAX_PER_WINDOW = 120/phút/user`.
- Dùng chung helper `_enforce_generic_limit`.
- 429 response với JSON error.

### BUG #5 — EXIF/GPS leak (CRITICAL privacy)
- Re-encode ảnh bằng Pillow với format gốc → drop toàn bộ EXIF.
- JPEG q=90, PNG optimize, GIF preserve animation, WebP q=90.
- RGBA → RGB flatten khi dest là JPEG.

### BUG #6 — Thiếu DB indexes
- `db_index=True` cho `Incident.reported_sbd` và `IncidentParticipant.sbd_snapshot`.
- Migration 0004.

### Settings hardening
- `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS` từ env.
- `DJANGO_FORCE_HTTPS`, `DJANGO_HSTS_SECONDS` optional.
- `CSRF_TRUSTED_ORIGINS` auto-derived.
- Khi `DEBUG=False`: `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_CONTENT_TYPE_NOSNIFF`, `SECURE_REFERRER_POLICY=same-origin`, `X_FRAME_OPTIONS=DENY`.
- `DEFAULT_AUTO_FIELD = BigAutoField`.
- `STATIC_ROOT` cho collectstatic.
- `FILE_UPLOAD_MAX_MEMORY_SIZE = 5MB`, `DATA_UPLOAD_MAX_MEMORY_SIZE = 12MB`.
- `SESSION_COOKIE_AGE = 8h`, expires on browser close, refreshed on activity.

### Cleanup
- Remove unused imports (`require_http_methods`, `IncidentParticipant` từ views).

## Verification

Harness `_harness.py` (không cần Django, 22 test cases):
```bash
python3 _harness.py
# Total: 22 passed, 0 failed
```

Pytest (cần Django + pytest-django):
```bash
pip install pytest pytest-django
pytest -v
```

Files test:
- `violations/tests/test_services_regex.py` — SBD pattern, mention token, helpers.
- `violations/tests/test_render_violation.py` — 25+ case bao phủ Task 3+6 + ADD-1.
- `violations/tests/test_preview_endpoint.py` — auth, role, rate limit.
- `violations/tests/test_image_uploads.py` — 4 format, reject, rate limit, EXIF strip.

## Checklist deploy

1. `pip install -r requirements.txt`
2. Set `DJANGO_SECRET_KEY` env var (tạo mới bằng `python -c "import secrets; print(secrets.token_urlsafe(50))"`)
3. Set `DJANGO_DEBUG=0`
4. Set `DJANGO_ALLOWED_HOSTS=yourdomain.com`
5. `python manage.py migrate`
6. `python manage.py collectstatic --noinput`
7. `python manage.py createsuperuser`
8. `python manage.py set_user_role <user> --role super_admin`
9. Configure web server WSGI entry → `chatbox_vi_pham.wsgi:application`
10. Map `/static/` → `STATIC_ROOT`, `/media/` → `MEDIA_ROOT` in web server config

Xem chi tiết trong `docs/DEPLOY_PYTHONANYWHERE.md`.
