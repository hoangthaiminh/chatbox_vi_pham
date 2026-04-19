import csv
import io
import mimetypes
import unicodedata

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.db import models
from django.http import FileResponse, Http404, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET, require_POST

from .forms import CandidateImportForm, IncidentCreateForm, IncidentEditForm
from .models import Candidate, Incident
from .realtime import (
    INCIDENT_PAGE_SIZE,
    INCIDENT_UPDATE_LIMIT,
    build_candidate_stats,
    build_live_payload,
    build_stats_payload,
    fetch_incidents_page,
    get_editable_incident_ids,
    render_incident_rows_html,
)
from .services import (
    MAX_SBD_LENGTH,
    MAX_VIOLATION_TEXT_LEN,
    is_valid_sbd_syntax,
    normalize_sbd,
    sync_incident_references,
)
from .ws_events import notify_live_update

# ── Limits ────────────────────────────────────────────────────────────────────
_MAX_CSV_SIZE    = 5 * 1024 * 1024   # 5 MB
_MAX_SBD_URL_LEN = MAX_SBD_LENGTH    # SBD path param must not exceed the hard cap
_MAX_ID_VALUE    = 2_147_483_647     # signed 32-bit max


# ── Role helpers ──────────────────────────────────────────────────────────────

def is_super_admin(user):
    if not user.is_authenticated:
        return False
    return user.is_superuser or user.groups.filter(name="super_admin").exists()


def is_room_admin(user):
    if not user.is_authenticated:
        return False
    return user.groups.filter(name="room_admin").exists()


def can_post_message(user):
    return is_super_admin(user) or is_room_admin(user)


def get_user_room_name(user):
    profile = getattr(user, "room_admin_profile", None)
    return profile.room_name if profile else ""


def role_label(user):
    if is_super_admin(user):
        return "Super Admin"
    if is_room_admin(user):
        room = get_user_room_name(user)
        return f"Room Admin ({room})" if room else "Room Admin"
    if user.is_authenticated:
        return "Viewer"
    return "Guest Viewer"


def normalize_header(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return value.lower().strip().replace(" ", "").replace("_", "")


# ── Dashboard / Statistics ────────────────────────────────────────────────────

def get_dashboard_context(request):
    incidents = fetch_incidents_page(limit=INCIDENT_PAGE_SIZE)
    editable_ids = get_editable_incident_ids(incidents, request.user)
    candidate_stats, unknown_stats = build_candidate_stats()
    oldest_id = incidents[0].id if incidents else None
    newest_id = incidents[-1].id if incidents else None
    has_older = Incident.objects.filter(id__lt=oldest_id).exists() if oldest_id else False

    return {
        "incidents": incidents,
        "editable_incident_ids": editable_ids,
        "current_user_id": request.user.id if request.user.is_authenticated else None,
        "oldest_incident_id": oldest_id,
        "newest_incident_id": newest_id,
        "has_older": has_older,
        "candidate_stats": candidate_stats,
        "unknown_stats": unknown_stats,
        "incident_form": IncidentCreateForm(),
        "import_form": CandidateImportForm(),
        "can_post": can_post_message(request.user),
        "can_import_candidates": is_super_admin(request.user),
        "role_label": role_label(request.user),
    }


def get_statistics_context(request):
    candidate_stats, unknown_stats = build_candidate_stats()
    return {
        "candidate_stats": candidate_stats,
        "unknown_stats": unknown_stats,
        "import_form": CandidateImportForm(),
        "can_import_candidates": is_super_admin(request.user),
        "role_label": role_label(request.user),
    }


@require_GET
def dashboard(request):
    return render(request, "violations/dashboard.html", get_dashboard_context(request))


@require_GET
def statistics(request):
    return render(request, "violations/statistics.html", get_statistics_context(request))


# ── Incident create ───────────────────────────────────────────────────────────

@require_POST
@login_required
def create_incident(request):
    if not can_post_message(request.user):
        return HttpResponseForbidden("You do not have permission to post incidents.")

    raw_sbd = (request.POST.get("sbd") or "").strip()
    if not is_valid_sbd_syntax(raw_sbd):
        messages.error(request, "SBD không hợp lệ: chỉ dùng chữ cái tiếng Anh và chữ số.")
        return redirect("violations:dashboard")

    form = IncidentCreateForm(request.POST, request.FILES)
    if not form.is_valid():
        for field, errors in form.errors.items():
            label = field.replace("_", " ").title()
            for error in errors:
                messages.error(request, f"{label}: {error}")
        return redirect("violations:dashboard")

    is_markdown = request.POST.get("is_markdown") == "1"

    incident = Incident(
        created_by=request.user,
        room_name=get_user_room_name(request.user),
        is_markdown=is_markdown,
    )
    evidence = form.cleaned_data.get("evidence")
    if evidence:
        incident.evidence = evidence

    sync_info = sync_incident_references(
        incident=incident,
        primary_sbd=form.cleaned_data["sbd"],
        violation_text=form.cleaned_data["violation_text"],
    )
    _surface_truncation_warnings(request, sync_info)
    notify_live_update()
    messages.success(request, "Incident posted successfully.")
    return redirect("violations:dashboard")


def _surface_truncation_warnings(request, sync_info):
    """Attach a user-visible warning if the service had to drop trailing
    digits from any SBD during normalisation."""
    from .services import MAX_SBD_LENGTH
    truncated = bool(sync_info.get("primary_sbd_truncated")) or bool(
        sync_info.get("mention_truncations")
    )
    if truncated:
        messages.warning(
            request,
            f"Những SBD quá {MAX_SBD_LENGTH} ký tự đã được cắt ngắn bằng cách xoá một số ký tự cuối.",
        )


# ── Incident edit ─────────────────────────────────────────────────────────────

@login_required
def edit_incident(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    if not incident.can_edit(request.user):
        return HttpResponseForbidden("You do not have permission to edit this incident.")

    initial_data = {
        "sbd": incident.reported_sbd,
        "violation_text": incident.violation_text,
    }

    if request.method == "POST":
        # Hard SBD check before form validation
        raw_sbd = (request.POST.get("sbd") or "").strip()
        if not is_valid_sbd_syntax(raw_sbd):
            messages.error(request, "SBD không hợp lệ: chỉ dùng chữ cái tiếng Anh và chữ số.")
            return redirect("violations:edit_incident", pk=pk)

        form = IncidentEditForm(request.POST, request.FILES)
        if form.is_valid():
            if form.cleaned_data.get("remove_evidence") and incident.evidence:
                incident.evidence.delete(save=False)
                incident.evidence = None

            evidence = form.cleaned_data.get("evidence")
            if evidence:
                if incident.evidence:
                    incident.evidence.delete(save=False)
                incident.evidence = evidence

            incident.is_markdown = True

            sync_info = sync_incident_references(
                incident=incident,
                primary_sbd=form.cleaned_data["sbd"],
                violation_text=form.cleaned_data["violation_text"],
            )
            _surface_truncation_warnings(request, sync_info)
            notify_live_update()
            messages.success(request, "Incident updated successfully.")
            return redirect("violations:dashboard")
        else:
            for field, errors in form.errors.items():
                label = field.replace("_", " ").title()
                for error in errors:
                    messages.error(request, f"{label}: {error}")
    else:
        form = IncidentEditForm(initial=initial_data)

    return render(
        request,
        "violations/edit_incident.html",
        {
            "incident": incident,
            "form": form,
            "role_label": role_label(request.user),
        },
    )


# ── Candidate detail ──────────────────────────────────────────────────────────

@require_GET
def candidate_detail(request, sbd):
    if len(sbd) > _MAX_SBD_URL_LEN or not is_valid_sbd_syntax(sbd):
        raise Http404("Invalid SBD.")

    from .services import apply_default_prefix
    normalized_sbd, _ = apply_default_prefix(sbd)
    candidate = Candidate.objects.filter(sbd__iexact=normalized_sbd).first()
    incidents = (
        Incident.objects.filter(participants__sbd_snapshot__iexact=normalized_sbd)
        .select_related("created_by", "reported_candidate")
        .distinct()
        .order_by("-created_at")
    )
    return render(
        request,
        "violations/_candidate_detail.html",
        {"candidate": candidate, "sbd": normalized_sbd, "incidents": incidents},
    )


# ── Evidence file ─────────────────────────────────────────────────────────────

@require_GET
def incident_evidence(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    if not incident.evidence:
        raise Http404("No evidence available.")

    guessed_type, _ = mimetypes.guess_type(incident.evidence.name)
    file_handle = incident.evidence.open("rb")
    response = FileResponse(file_handle, content_type=guessed_type or "application/octet-stream")
    response["Content-Disposition"] = f'inline; filename="{incident.evidence.name.split("/")[-1]}"'
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ── Candidate search (mention autocomplete) ───────────────────────────────────

@require_GET
@login_required
def candidate_search(request):
    q = (request.GET.get("q") or "").strip().upper()[:50]
    if not q:
        qs = Candidate.objects.all().order_by("sbd")[:20]
    else:
        if q.isdigit():
            from .services import apply_default_prefix
            canonical, _ = apply_default_prefix(q)
            qs = Candidate.objects.filter(
                models.Q(sbd__icontains=q) | models.Q(sbd__icontains=canonical)
            ).order_by("sbd")[:20]
        else:
            qs = Candidate.objects.filter(sbd__icontains=q).order_by("sbd")[:20]
    results = [{"sbd": c.sbd, "full_name": c.full_name} for c in qs]
    return JsonResponse({"results": results})


# ── Live snapshot ─────────────────────────────────────────────────────────────

@require_GET
def live_snapshot(request):
    return JsonResponse(build_live_payload(request.user))


# ── Incident history (load older) ─────────────────────────────────────────────

@require_GET
def incident_history(request):
    before_raw = request.GET.get("before")
    if not before_raw:
        return HttpResponseBadRequest("Missing before parameter.")
    try:
        before_id = int(before_raw)
    except (ValueError, TypeError):
        return HttpResponseBadRequest("before must be an integer.")
    if not (1 <= before_id <= _MAX_ID_VALUE):
        return HttpResponseBadRequest("before out of valid range.")

    incidents = fetch_incidents_page(before_id=before_id, limit=INCIDENT_PAGE_SIZE)
    oldest_id = incidents[0].id if incidents else None
    newest_id = incidents[-1].id if incidents else None
    has_older = Incident.objects.filter(id__lt=oldest_id).exists() if oldest_id else False

    return JsonResponse({
        "incidents_html": render_incident_rows_html(incidents, request.user),
        "oldest_id": oldest_id,
        "newest_id": newest_id,
        "has_older": has_older,
    })


# ── Incident updates (polling / WS trigger) ───────────────────────────────────

@require_GET
def incident_updates(request):
    after_raw = request.GET.get("after", "0")
    try:
        after_id = int(after_raw)
    except (ValueError, TypeError):
        return HttpResponseBadRequest("after must be an integer.")
    # Clamp: negative values would return everything; values > max are no-ops anyway
    after_id = max(0, min(after_id, _MAX_ID_VALUE))

    incidents = fetch_incidents_page(after_id=after_id, limit=INCIDENT_UPDATE_LIMIT)
    payload = build_stats_payload()
    payload.update({
        "incidents_html": render_incident_rows_html(incidents, request.user),
        "oldest_id": incidents[0].id if incidents else None,
        "newest_id": incidents[-1].id if incidents else None,
        "added_count": len(incidents),
    })
    return JsonResponse(payload)


# ── Import candidates ─────────────────────────────────────────────────────────

@require_POST
@login_required
def import_candidates(request):
    if not is_super_admin(request.user):
        return HttpResponseForbidden("Only Super Admin can import candidate data.")

    form = CandidateImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Please upload a valid CSV file.")
        return redirect("violations:dashboard")

    csv_file = form.cleaned_data["csv_file"]

    # Enforce upload size limit (5 MB)
    if csv_file.size > _MAX_CSV_SIZE:
        messages.error(request, "CSV file must be smaller than 5 MB.")
        return redirect("violations:dashboard")

    raw_content = csv_file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw_content))

    if not reader.fieldnames:
        messages.error(request, "CSV must include header columns.")
        return redirect("violations:dashboard")

    header_map = {normalize_header(h): h for h in reader.fieldnames}

    def row_value(row, keys, default=""):
        for key in keys:
            raw_header = header_map.get(key)
            if raw_header and row.get(raw_header):
                return row[raw_header].strip()
        return default

    created_count = updated_count = truncated_count = 0

    for row in reader:
        raw_sbd = row_value(row, ["sbd", "sobaodanh"])
        if not raw_sbd:
            continue
        from .services import apply_default_prefix
        sbd, was_truncated = apply_default_prefix(raw_sbd)
        if not sbd or not is_valid_sbd_syntax(sbd):
            continue

        defaults = {
            "full_name": row_value(row, ["hovaten", "fullname", "name"], default="Unknown")[:150],
            "school": row_value(row, ["truong", "school"], default="Unknown")[:150],
            "supervisor_teacher": row_value(
                row, ["gvpt", "giaovienphutrach", "supervisorteacher"], default="Unknown"
            )[:150],
            "exam_room": row_value(row, ["phongthi", "examroom", "room"])[:50],
        }

        _, created = Candidate.objects.update_or_create(sbd=sbd, defaults=defaults)
        if created:
            created_count += 1
        else:
            updated_count += 1
        if was_truncated:
            truncated_count += 1

    summary = f"Candidate import finished. Created: {created_count}, Updated: {updated_count}."
    if truncated_count:
        summary += f" {truncated_count} SBD đã được cắt ngắn cho vừa {MAX_SBD_LENGTH} ký tự."
    messages.success(request, summary)
    notify_live_update()
    return redirect("violations:dashboard")


# ── Auth ──────────────────────────────────────────────────────────────────────

def login_view(request):
    if request.user.is_authenticated:
        return redirect("violations:dashboard")

    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect("violations:dashboard")

    return render(request, "violations/login.html", {
        "form": form,
        "role_label": role_label(request.user),
    })


@require_POST   # CRITICAL FIX: logout must be POST-only (CSRF-protected)
@login_required
def logout_view(request):
    logout(request)
    return redirect("violations:dashboard")


# ── Incident markdown preview (server-side rendering) ────────────────────────

@require_POST
@login_required
def incident_preview(request):
    """Render a draft incident exactly as a viewer would see it.

    Security / policy:
      • Only users who can post incidents (room_admin / super_admin) get the
        preview endpoint. Viewers don't compose messages.
      • Text is clamped to MAX_VIOLATION_TEXT_LEN to match service-layer limit.
      • The Incident object is built in-memory and NEVER saved; .participants
        is not populated. The shared card template hides the participants
        block in preview mode for that reason.
      • Output HTML is generated by the same render_violation filter that
        powers the live feed, so mention rules (DB-missing strike, neutralised
        contexts, XSS strip) are guaranteed consistent with production render.
    """
    if not can_post_message(request.user):
        return HttpResponseForbidden("Preview is only available for posters.")

    # Per-user preview rate limit so tight loops cannot burn CPU on
    # markdown + BeautifulSoup rendering. Generous enough for a human
    # composing a message (120 calls / minute).
    from .image_uploads import PreviewRateLimitError, enforce_preview_rate_limit
    try:
        enforce_preview_rate_limit(request.user.id)
    except PreviewRateLimitError as exc:
        return JsonResponse({"error": str(exc)}, status=429)

    raw_sbd = (request.POST.get("sbd") or "").strip()
    raw_text = (request.POST.get("violation_text") or "")
    is_markdown = request.POST.get("is_markdown") == "1"

    if len(raw_text) > MAX_VIOLATION_TEXT_LEN:
        raw_text = raw_text[:MAX_VIOLATION_TEXT_LEN]

    from .services import MENTION_TOKEN_PATTERN, SBD_PATTERN, apply_default_prefix

    if raw_sbd and is_valid_sbd_syntax(raw_sbd):
        sbd, _ = apply_default_prefix(raw_sbd)
    else:
        sbd = raw_sbd.upper()[:9]

    def _canon(m):
        canon, _ = apply_default_prefix(m.group(1))
        if SBD_PATTERN.match(canon):
            return "@{" + canon + "}"
        return m.group(0)
    raw_text = MENTION_TOKEN_PATTERN.sub(_canon, raw_text)

    candidate = (
        Candidate.objects.filter(sbd__iexact=sbd).first() if sbd else None
    )

    preview_incident = Incident(
        reported_sbd=sbd,
        violation_text=raw_text,
        room_name=get_user_room_name(request.user),
        is_markdown=is_markdown,
    )
    preview_incident.reported_candidate = candidate
    preview_incident.created_by = request.user

    html = render_to_string(
        "violations/_incident_card.html",
        {
            "incident": preview_incident,
            "mode": "preview",
            "current_user_id": request.user.id,
            "editable_incident_ids": [],
        },
        request=request,
    )
    return JsonResponse({"html": html})


# ── Image upload for markdown embedding (GitHub-style) ───────────────────────

@require_POST
@login_required
def upload_image(request):
    """Accept a single image for markdown embedding.

    Security:
      * login required + must be allowed to post incidents (viewers cannot).
      * per-user in-memory rate limit (see image_uploads module).
      * Pillow validates the file is a real image; filename is replaced with
        a UUID to eliminate path/name injection.

    Response:
      200 {"url": "/media/..."} on success
      4xx {"error": "..."} on rejection
    """
    from .image_uploads import ImageUploadError, save_uploaded_image

    if not can_post_message(request.user):
        return JsonResponse(
            {"error": "Bạn không có quyền upload ảnh."}, status=403,
        )

    upload = request.FILES.get("image")
    if upload is None:
        return JsonResponse({"error": "Thiếu file ảnh."}, status=400)

    try:
        url = save_uploaded_image(upload, user_id=request.user.id)
    except ImageUploadError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        # Don't leak server internals; log on server side for the operator.
        import logging
        logging.getLogger(__name__).exception("Unexpected image upload failure")
        return JsonResponse(
            {"error": "Lỗi máy chủ khi xử lý ảnh. Vui lòng thử lại."},
            status=500,
        )

    return JsonResponse({"url": url})
