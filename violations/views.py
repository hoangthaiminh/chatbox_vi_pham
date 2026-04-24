import csv
import io
import mimetypes
import re
import unicodedata

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.db import models, transaction
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET, require_POST

from .forms import (
    ALLOWED_VIDEO_EXTENSIONS,
    CandidateImportForm,
    IncidentCreateForm,
    IncidentEditForm,
)
from .models import Candidate, Incident, IncidentParticipant
from .realtime import (
    INCIDENT_PAGE_SIZE,
    INCIDENT_UPDATE_LIMIT,
    build_candidate_stats,
    build_live_payload,
    build_stats_payload,
    can_delete_incidents,
    fetch_incidents_page,
    get_editable_incident_ids,
    render_incident_rows_html,
)
from .services import (
    MAX_SBD_LENGTH,
    MAX_VIOLATION_TEXT_LEN,
    ROLE_LABELS,
    ROLE_ROOM_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_VIEWER,
    apply_default_prefix,
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
    return user.is_superuser or user.groups.filter(name=ROLE_SUPER_ADMIN).exists()


def is_room_admin(user):
    if not user.is_authenticated:
        return False
    return user.groups.filter(name=ROLE_ROOM_ADMIN).exists()


def can_post_message(user):
    return is_super_admin(user) or is_room_admin(user)


def is_ajax_request(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def get_user_room_name(user):
    profile = getattr(user, "room_admin_profile", None)
    return profile.room_name if profile else ""


def role_label(user):
    if is_super_admin(user):
        return ROLE_LABELS[ROLE_SUPER_ADMIN]
    if is_room_admin(user):
        room = get_user_room_name(user)
        return f"{ROLE_LABELS[ROLE_ROOM_ADMIN]} ({room})" if room else ROLE_LABELS[ROLE_ROOM_ADMIN]
    if user.is_authenticated:
        return ROLE_LABELS[ROLE_VIEWER]
    return "Khách xem"


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
        "can_delete_incidents": can_delete_incidents(request.user),
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
        if is_ajax_request(request):
            return JsonResponse({"ok": False, "error": "Bạn không có quyền gửi sự việc."}, status=403)
        return HttpResponseForbidden("Bạn không có quyền gửi sự việc.")

    raw_sbd = (request.POST.get("sbd") or "").strip()
    if not is_valid_sbd_syntax(raw_sbd):
        if is_ajax_request(request):
            return JsonResponse({"ok": False, "error": "SBD không hợp lệ."}, status=400)
        messages.error(request, "SBD không hợp lệ: chỉ dùng chữ cái tiếng Anh và chữ số.")
        return redirect("violations:dashboard")

    form = IncidentCreateForm(request.POST, request.FILES)
    if not form.is_valid():
        if is_ajax_request(request):
            error_text = "; ".join(
                f"{getattr(form.fields.get(field), 'label', field)}: {', '.join(errors)}"
                for field, errors in form.errors.items()
            )
            return JsonResponse(
                {
                    "ok": False,
                    "error": error_text or "Dữ liệu sự việc không hợp lệ.",
                    "errors": form.errors,
                },
                status=400,
            )
        for field, errors in form.errors.items():
            label = getattr(form.fields.get(field), "label", field)
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
        if _evidence_is_video(evidence):
            rate_error = _enforce_video_rate_limit(request.user.id)
            if rate_error:
                if is_ajax_request(request):
                    return JsonResponse({"ok": False, "error": rate_error}, status=429)
                messages.error(request, rate_error)
                return redirect("violations:dashboard")
        incident.evidence = evidence

    sync_info = sync_incident_references(
        incident=incident,
        primary_sbd=form.cleaned_data["sbd"],
        violation_text=form.cleaned_data["violation_text"],
        incident_kind=form.cleaned_data["incident_kind"],
    )
    notify_live_update()

    if is_ajax_request(request):
        payload = build_stats_payload()
        payload.update(
            {
                "ok": True,
                "incident_html": render_incident_rows_html([incident], request.user),
                "incident_id": incident.id,
                "newest_id": incident.id,
            }
        )
        return JsonResponse(payload)

    _surface_truncation_warnings(request, sync_info)

    messages.success(request, "Đã gửi sự việc thành công.")
    return redirect("violations:dashboard")


def _evidence_is_video(uploaded_file):
    """Return True if the uploaded file's extension is a video format."""
    from pathlib import Path
    if not uploaded_file or not getattr(uploaded_file, "name", ""):
        return False
    return Path(uploaded_file.name).suffix.lower() in ALLOWED_VIDEO_EXTENSIONS


def _enforce_video_rate_limit(user_id):
    """Wrap the sliding-window limiter so callers get a user-facing string
    on throttle and None when the upload is allowed."""
    from .image_uploads import VideoUploadRateLimitError, enforce_video_rate_limit
    try:
        enforce_video_rate_limit(user_id)
    except VideoUploadRateLimitError as exc:
        return str(exc)
    return None


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
        return HttpResponseForbidden("Bạn không có quyền sửa sự việc này.")

    initial_data = {
        "sbd": incident.reported_sbd,
        "incident_kind": incident.incident_kind,
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
                if _evidence_is_video(evidence):
                    rate_error = _enforce_video_rate_limit(request.user.id)
                    if rate_error:
                        messages.error(request, rate_error)
                        return redirect("violations:edit_incident", pk=pk)
                if incident.evidence:
                    incident.evidence.delete(save=False)
                incident.evidence = evidence

            incident.is_markdown = True

            sync_info = sync_incident_references(
                incident=incident,
                primary_sbd=form.cleaned_data["sbd"],
                violation_text=form.cleaned_data["violation_text"],
                incident_kind=form.cleaned_data["incident_kind"],
            )
            _surface_truncation_warnings(request, sync_info)
            notify_live_update()
            messages.success(request, "Đã cập nhật sự việc thành công.")
            return redirect("violations:dashboard")
        else:
            for field, errors in form.errors.items():
                label = getattr(form.fields.get(field), "label", field)
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


@require_POST
@login_required
def delete_incident(request, pk):
    if not can_delete_incidents(request.user):
        if is_ajax_request(request):
            return JsonResponse({"error": "Bạn không có quyền xoá sự việc."}, status=403)
        return HttpResponseForbidden("Bạn không có quyền xoá sự việc.")

    incident = get_object_or_404(Incident, pk=pk)
    if incident.evidence:
        incident.evidence.delete(save=False)
    incident.delete()
    notify_live_update()

    if is_ajax_request(request):
        return JsonResponse({"ok": True, "incident_id": pk})

    messages.success(request, "Đã xoá tin nhắn thành công.")
    return redirect("violations:dashboard")


# ── Candidate detail ──────────────────────────────────────────────────────────

@require_GET
def candidate_detail(request, sbd):
    if len(sbd) > _MAX_SBD_URL_LEN or not is_valid_sbd_syntax(sbd):
        raise Http404("SBD không hợp lệ.")

    normalized_sbd, _ = apply_default_prefix(sbd)
    candidate = Candidate.objects.filter(sbd__iexact=normalized_sbd).first()
    incidents = (
        Incident.objects.filter(participants__sbd_snapshot__iexact=normalized_sbd)
        .select_related("created_by", "reported_candidate")
        .distinct()
        .order_by("-created_at")
    )

    violation_count = 0
    reminder_count = 0
    for incident in incidents:
        if incident.incident_kind == Incident.KIND_REMINDER:
            reminder_count += 1
        else:
            violation_count += 1
    effective_violations = violation_count + (reminder_count // 2)

    return render(
        request,
        "violations/_candidate_detail.html",
        {
            "candidate": candidate,
            "sbd": normalized_sbd,
            "incidents": incidents,
            "rule_summary": {
                "violation_count": violation_count,
                "reminder_count": reminder_count,
                "effective_violations": effective_violations,
                "is_out": effective_violations >= 2,
            },
        },
    )


# ── Evidence file ─────────────────────────────────────────────────────────────

_RANGE_HEADER_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _iter_file_chunks(file_handle, remaining, chunk_size=8192):
    try:
        while remaining > 0:
            data = file_handle.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
    finally:
        file_handle.close()


@require_GET
def incident_evidence(request, pk):
    incident = get_object_or_404(Incident, pk=pk)
    if not incident.evidence:
        raise Http404("Không có bằng chứng đính kèm.")

    guessed_type, _ = mimetypes.guess_type(incident.evidence.name)
    content_type = guessed_type or "application/octet-stream"
    filename = incident.evidence.name.split("/")[-1]

    try:
        file_size = incident.evidence.size
    except (OSError, NotImplementedError):
        file_size = None

    range_header = request.META.get("HTTP_RANGE", "").strip()
    range_match = _RANGE_HEADER_RE.match(range_header) if range_header else None

    if range_match and file_size is not None:
        start_str, end_str = range_match.groups()
        if start_str == "" and end_str == "":
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{file_size}"
            return response
        if start_str == "":
            # Suffix range: last N bytes.
            suffix_length = int(end_str)
            if suffix_length <= 0:
                response = HttpResponse(status=416)
                response["Content-Range"] = f"bytes */{file_size}"
                return response
            start = max(file_size - suffix_length, 0)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
        if end >= file_size:
            end = file_size - 1
        if start > end or start >= file_size:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{file_size}"
            return response
        length = end - start + 1
        file_handle = incident.evidence.open("rb")
        file_handle.seek(start)
        response = StreamingHttpResponse(
            _iter_file_chunks(file_handle, length),
            status=206,
            content_type=content_type,
        )
        response["Content-Length"] = str(length)
        response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    else:
        file_handle = incident.evidence.open("rb")
        response = FileResponse(file_handle, content_type=content_type)
        if file_size is not None:
            response["Content-Length"] = str(file_size)

    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = f'inline; filename="{filename}"'
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
        return HttpResponseBadRequest("Thiếu tham số before.")
    try:
        before_id = int(before_raw)
    except (ValueError, TypeError):
        return HttpResponseBadRequest("before phải là số nguyên.")
    if not (1 <= before_id <= _MAX_ID_VALUE):
        return HttpResponseBadRequest("before nằm ngoài phạm vi hợp lệ.")

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
        return HttpResponseBadRequest("after phải là số nguyên.")
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
        return HttpResponseForbidden("Chỉ Quản trị tổng mới được nhập dữ liệu thí sinh.")

    form = CandidateImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Vui lòng tải lên tệp CSV hợp lệ.")
        return redirect("violations:dashboard")

    csv_file = form.cleaned_data["csv_file"]

    # Enforce upload size limit (5 MB)
    if csv_file.size > _MAX_CSV_SIZE:
        messages.error(request, "Tệp CSV phải nhỏ hơn 5 MB.")
        return redirect("violations:dashboard")

    raw_content = csv_file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw_content))

    if not reader.fieldnames:
        messages.error(request, "CSV phải có dòng tiêu đề cột.")
        return redirect("violations:dashboard")

    header_map = {normalize_header(h): h for h in reader.fieldnames}

    def row_value(row, keys, default=""):
        for key in keys:
            raw_header = header_map.get(key)
            if raw_header and row.get(raw_header):
                return row[raw_header].strip()
        return default

    created_count = updated_count = truncated_count = 0
    imported_sbds = []

    with transaction.atomic():
        for row in reader:
            raw_sbd = row_value(row, ["sbd", "sobaodanh"])
            if not raw_sbd:
                continue
            sbd, was_truncated = apply_default_prefix(raw_sbd)
            if not sbd or not is_valid_sbd_syntax(sbd):
                continue

            defaults = {
                "full_name": row_value(row, ["hovaten", "fullname", "name"], default="Chưa rõ")[:150],
                "school": row_value(row, ["truong", "school"], default="Chưa rõ")[:150],
                "supervisor_teacher": row_value(
                    row, ["gvpt", "giaovienphutrach", "supervisorteacher"], default="Chưa rõ"
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
            imported_sbds.append(sbd)

        # Re-link incidents/participants whose SBDs are now covered by the
        # updated candidate list. Without this, previously-unknown SBDs remain
        # flagged as "Không tìm thấy hồ sơ thí sinh" and stay in the
        # unknown-stats bucket. Scope the UPDATE to just the SBDs touched in
        # this import so we don't sweep the entire unresolved history.
        relinked_incidents, relinked_participants = _relink_candidates_to_references(
            imported_sbds
        )

    summary = f"Đã nhập danh sách thí sinh. Thêm mới: {created_count}, Cập nhật: {updated_count}."
    if truncated_count:
        summary += f" {truncated_count} SBD đã được cắt ngắn cho vừa {MAX_SBD_LENGTH} ký tự."
    if relinked_incidents or relinked_participants:
        summary += (
            f" Đã gắn lại {relinked_incidents} tin nhắn chính và "
            f"{relinked_participants} lượt nhắc với hồ sơ thí sinh."
        )
    messages.success(request, summary)
    notify_live_update()
    return redirect("violations:dashboard")


def _relink_candidates_to_references(sbds):
    """Bind Incident.reported_candidate and IncidentParticipant.candidate to
    Candidate rows whose SBD matches one of ``sbds``. Returns
    ``(incident_count, participant_count)``.

    Called right after a candidate import: the SBDs in ``sbds`` are the ones
    just inserted/updated, so any incident/participant that previously had a
    NULL FK because the candidate didn't exist can now be linked. SBDs are
    stored upper-cased on both sides (see model ``save()`` methods), so the
    equality join is exact and hits the db_index on both columns.
    """
    if not sbds:
        return 0, 0

    from django.db.models import OuterRef, Subquery

    incident_lookup = Candidate.objects.filter(
        sbd=OuterRef("reported_sbd")
    ).values("pk")[:1]
    incident_count = Incident.objects.filter(
        reported_candidate__isnull=True,
        reported_sbd__in=sbds,
    ).update(reported_candidate=Subquery(incident_lookup))

    participant_lookup = Candidate.objects.filter(
        sbd=OuterRef("sbd_snapshot")
    ).values("pk")[:1]
    participant_count = IncidentParticipant.objects.filter(
        candidate__isnull=True,
        sbd_snapshot__in=sbds,
    ).update(candidate=Subquery(participant_lookup))

    return incident_count, participant_count


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
        return HttpResponseForbidden("Chỉ người có quyền gửi sự việc mới được xem trước.")

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

    if raw_sbd and is_valid_sbd_syntax(raw_sbd):
        sbd, _ = apply_default_prefix(raw_sbd)
    else:
        sbd = raw_sbd.upper()[:9]

    candidate = (
        Candidate.objects.filter(sbd__iexact=sbd).first() if sbd else None
    )

    preview_incident = Incident(
        reported_sbd=sbd,
        incident_kind=Incident.normalize_incident_kind(request.POST.get("incident_kind")),
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
