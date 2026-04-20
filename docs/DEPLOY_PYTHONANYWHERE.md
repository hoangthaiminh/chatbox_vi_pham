# Deploying on PythonAnywhere (Free tier)

This document covers the **non-obvious** constraints of PythonAnywhere's
free tier and how to work around them. The app was built assuming
Daphne + Django Channels + WebSocket — PA free does **not** support that,
so you will need the adjustments below.

---

## 1. Critical: WebSockets do not work on PA free

PA free only serves WSGI over HTTP; long-lived WebSocket connections are
dropped. The app already includes an HTTP polling fallback:

* `GET /api/incidents/updates/?after=<id>` — returns newer incidents.
* `GET /api/live/` — same payload as the WS snapshot.
* `GET /api/incidents/history/?before=<id>` — older incidents.

Client-side behaviour (`app.js`): if the WebSocket fails to connect, the
dashboard falls back to calling `/api/incidents/updates/` on a timer.
Confirm this by checking `live-connection-status` at the top of the page:
it should read something like *"Polling every 5 s"* after the WS attempt
fails.

### What to change for PA free
1. **Do not run Daphne.** On PA free the WSGI entry-point is already
   `chatbox_vi_pham/wsgi.py` — just configure the web-app's WSGI file to
   import `application` from there.
2. **Remove `daphne` and `channels` from actively-running processes.**
   You can leave them installed (won't hurt), they just won't serve
   anything.
3. **Make sure `DEBUG = False`** in production and add your PA domain
   to `ALLOWED_HOSTS` in `chatbox_vi_pham/settings.py`:
   ```python
   ALLOWED_HOSTS = ["yourname.pythonanywhere.com"]
   ```
4. **Regenerate `SECRET_KEY`.** The baseline uses
   `django-insecure-...` which must not ship to production.

---

## 2. Disk budget (512 MB)

Rough allowance:

| Item                         | Size      |
|------------------------------|-----------|
| Python packages + venv       | ~80–100 MB |
| App code + static vendor     | ~3 MB     |
| `db.sqlite3` (a few hundred rows) | <5 MB |
| Uploaded images (10 MB/file) | **the rest — budget this** |

At ~20 MB per incident's video + average 2–3 image uploads per incident,
100 incidents can easily push past 300 MB. Mitigations:

* Lower `MAX_IMAGE_SIZE` (in `violations/image_uploads.py`) from 10 MB to
  e.g. 3 MB during the exam.
* Video evidence already goes to `evidence/%Y/%m/%d/` and can be purged
  manually after the exam.
* Set up a PA scheduled task (free tier allows one daily task) to clean
  up images older than N days.

---

## 3. Serving uploaded media on PA

Django's `static()` shortcut (in `chatbox_vi_pham/urls.py`) serves media
**only when `DEBUG=True`**. In production you must map static/media
directly to the filesystem via PA's Web tab:

| URL prefix   | Directory on PA                                  |
|--------------|--------------------------------------------------|
| `/static/`   | `/home/<user>/chatbox_vi_pham/staticfiles/`      |
| `/media/`    | `/home/<user>/chatbox_vi_pham/media/`            |

Then run `python manage.py collectstatic` once after each deploy.

---

## 4. Outbound network

PA free blocks arbitrary outbound HTTP except to a whitelist. This app
does not make any outbound calls at runtime, so you are fine.

The **front-end** loads CDNs (Bootstrap, LightGallery, marked.js) from
`cdnjs.cloudflare.com`. Those are loaded by the browser, not the server,
so PA's server-side whitelist does not apply.

---

## 5. Install checklist (one-off)

From the PA bash console:

```bash
# Create venv
mkvirtualenv --python=/usr/bin/python3.12 chatbox_env
workon chatbox_env

# Install deps (pymdown-extensions, Pillow, beautifulsoup4 were added
# during this patch series)
cd ~/chatbox_vi_pham
pip install -r requirements.txt

# One-time DB migration (includes 0003_shrink_sbd_max_length_to_9)
python manage.py migrate

# Create the first super-admin
python manage.py createsuperuser
python manage.py set_user_role <username> --role super_admin

# Collect static
python manage.py collectstatic --noinput
```

Then in the PA Web tab:
1. Source code: `/home/<user>/chatbox_vi_pham`.
2. Virtualenv: `/home/<user>/.virtualenvs/chatbox_env`.
3. WSGI file: point it at `chatbox_vi_pham/wsgi.py`.
4. Static / Media mappings as in §3.

---

## 6. Rate-limit state

`violations/image_uploads.py` uses an in-process dict for the per-user
upload rate limit. PA free runs a single process per web app, so this is
correct. If you upgrade to a paid tier with multiple workers, each worker
will track its own counters (still functional as deterrent, but not a
hard quota). Swap for a Redis-backed limiter in that case.

---

## 7. Running tests

```bash
pip install pytest pytest-django
pytest -v
```

All tests are under `violations/tests/`. The suite does not touch the
production DB — pytest-django uses an isolated test database.
