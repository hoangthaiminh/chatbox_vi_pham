# Project Structure

This document explains how the codebase is organized and where to modify each feature.

## Top-level layout

```text
chatbox_vi_pham/
├── manage.py
├── db.sqlite3
├── README.md
├── requirements.txt
├── pytest.ini
├── conftest.py
├── sample_candidates.csv
├── docs/
│   ├── PROJECT_STRUCTURE.md
│   ├── USAGE_GUIDE.md
│   └── DEPLOY_PYTHONANYWHERE.md    # PA free-tier specific notes
├── chatbox_vi_pham/
│   ├── settings.py                  # env-var driven, prod hardening
│   ├── urls.py                      # serves /media/ in DEBUG
│   ├── asgi.py
│   └── wsgi.py                      # use this on PA (WSGI only)
└── violations/
    ├── admin.py
    ├── apps.py
    ├── forms.py
    ├── models.py
    ├── services.py                  # SBD regex, mention extraction, sync
    ├── image_uploads.py              # Pillow validation, EXIF strip, rate limit
    ├── urls.py
    ├── views.py                     # incident CRUD, preview, upload, CSV import
    ├── realtime.py                  # payload builders, WS + polling helpers
    ├── consumers.py                 # Django Channels WS consumer
    ├── ws_events.py
    ├── routing.py
    ├── migrations/
    │   ├── 0001_initial.py
    │   ├── 0002_create_default_groups.py
    │   ├── 0003_shrink_sbd_max_length_to_9.py
    │   └── 0004_add_sbd_indexes.py
    ├── management/commands/
    │   └── set_user_role.py
    ├── templates/violations/
    │   ├── base.html
    │   ├── dashboard.html
    │   ├── statistics.html
    │   ├── login.html
    │   ├── edit_incident.html
    │   ├── _incident_list.html
    │   ├── _incident_rows.html       # iterates incidents → _incident_card.html
    │   ├── _incident_card.html       # SHARED card router (full|mini|preview)
    │   ├── _incident_card_body.html  # SHARED card body
    │   ├── _stats_table.html
    │   └── _candidate_detail.html
    ├── templatetags/
    │   ├── __init__.py
    │   └── violations_extras.py      # render_violation filter (markdown + mention)
    ├── tests/
    │   ├── __init__.py
    │   ├── test_services_regex.py
    │   ├── test_render_violation.py
    │   ├── test_preview_endpoint.py
    │   └── test_image_uploads.py
    └── static/violations/
        ├── css/app.css
        └── js/app.js
```

## Responsibilities by file

### Core Django project

- `chatbox_vi_pham/settings.py`
  - Global configuration
  - Installed apps
  - Static/media settings
  - Timezone and auth redirects
- `chatbox_vi_pham/urls.py`
  - Root URL routing
  - Includes app routes

### Domain and business logic

- `violations/models.py`
  - `Candidate`: candidate master data (SBD, name, school, room, supervisor)
  - `Incident`: reported violation message + optional evidence
  - `IncidentParticipant`: links all SBDs involved in one incident (reported + mentioned)
  - `RoomAdminProfile`: room binding for room admins
- `violations/services.py`
  - SBD extraction from violation text
  - Incident reference synchronization
  - Auto-link logic so both sides are counted in stats

### Request handling

- `violations/forms.py`
  - Validation for incident create/edit
  - Evidence type/size checks
  - Candidate CSV import form
- `violations/views.py`
  - Dashboard view
  - Incident create/edit flow
  - Live refresh API endpoint
  - Candidate detail panel endpoint
  - Evidence streaming endpoint
  - CSV import endpoint
  - Login/logout views
- `violations/urls.py`
  - App-level URL routes

### Admin and automation

- `violations/admin.py`
  - Django admin configuration for models
- `violations/management/commands/set_user_role.py`
  - CLI utility to assign user role:
    - `super_admin`
    - `room_admin` (+ room name)
    - `viewer`

### UI layer

- `violations/templates/violations/base.html`
  - Shared layout and Bootstrap includes
- `violations/templates/violations/dashboard.html`
  - Main page with Chat Box and Statistics tabs
- Partial templates:
  - `_incident_list.html` for live feed rendering
  - `_stats_table.html` for aggregated statistics
  - `_candidate_detail.html` for offcanvas candidate detail panel
- `violations/static/violations/css/app.css`
  - Visual design and responsive styling
- `violations/static/violations/js/app.js`
  - Live refresh
  - Candidate detail loading
  - Evidence preview modal
  - Basic anti-copy/anti-download UI controls

## Data flow summary

1. Room/Super admin submits incident with SBD + violation text + evidence.
2. Backend extracts all SBD-like codes from text.
3. Backend creates participant links for primary SBD and mentioned SBDs.
4. Statistics aggregate by `IncidentParticipant`, not only primary SBD.
5. Dashboard websocket push updates refresh feed and statistics only when data changes.

## Where to change common requirements

- Add new candidate fields: `violations/models.py` + migrations + templates
- Change role rules: `Incident.can_edit` in `violations/models.py`
- Change statistics logic: `build_candidate_stats` in `violations/views.py`
- Change SBD parsing pattern: `SBD_PATTERN` in `violations/services.py`
- Change upload restrictions: `ALLOWED_EVIDENCE_EXTENSIONS` and `MAX_EVIDENCE_SIZE` in `violations/forms.py`
