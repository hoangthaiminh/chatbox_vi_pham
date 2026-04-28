import csv
import io
import mimetypes
import re
import unicodedata

from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
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
    AdminPasswordChangeForm,
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
    get_deletable_incident_ids,
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
from .locks import (
    BUSY_USER_MESSAGE,
    INCIDENT_BUSY_USER_MESSAGE,
    acquire_candidate_lock,
    acquire_incident_bulk_lock,
    get_incident_bulk_lock_state,
    get_lock_state,
    release_candidate_lock,
    release_incident_bulk_lock,
)
from .ws_events import (
    notify_candidates_changed,
    notify_candidates_lock,
    notify_incidents_changed,
    notify_incidents_lock,
    notify_live_update,
)

# ── Limits ────────────────────────────────────────────────────────────────────
_MAX_CSV_SIZE    = 5 * 1024 * 1024   # 5 MB
_MAX_SBD_URL_LEN = MAX_SBD_LENGTH    # SBD path param must not exceed the hard cap
_MAX_ID_VALUE    = 2_147_483_647     # signed 32-bit max

# Hard ceiling for incident bulk-select / bulk-delete. Kept here as a single
# source of truth so the JS, the GET deletable-ids endpoint, and the POST
# bulk-delete endpoint all enforce the same number. The user-facing error
# message ("Không thể thao tác nhiều hơn N tin nhắn") interpolates this
# constant directly.
INCIDENT_BULK_DELETE_MAX = 50


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
    deletable_ids = get_deletable_incident_ids(incidents, request.user)
    candidate_stats, unknown_stats = build_candidate_stats()
    oldest_id = incidents[0].id if incidents else None
    newest_id = incidents[-1].id if incidents else None
    has_older = Incident.objects.filter(id__lt=oldest_id).exists() if oldest_id else False

    return {
        "incidents": incidents,
        "editable_incident_ids": editable_ids,
        "deletable_incident_ids": deletable_ids,
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
    can_import = is_super_admin(request.user)
    # Only the super admin sees the full roster table — keep this off the
    # context for everyone else so we don't leak personal info.
    all_candidates = Candidate.objects.all().order_by("sbd") if can_import else []
    return {
        "candidate_stats": candidate_stats,
        "unknown_stats": unknown_stats,
        "import_form": CandidateImportForm(),
        "can_import_candidates": can_import,
        "all_candidates": all_candidates,
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

    busy = _refuse_if_incident_bulk_busy(request)
    if busy is not None:
        return busy

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
        busy = _refuse_if_incident_bulk_busy(request)
        if busy is not None:
            return busy
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
    """Delete an incident.

    Permission model:
      * Super admins (and superusers) may delete any incident.
      * Room admins may delete only incidents they themselves created. They
        must NOT be able to remove a super admin's notice.
      * Anyone else is rejected.

    Robustness:
      * If the incident has already been deleted (race with another tab/
        WebSocket-pushed delete), AJAX callers receive ``{ok: True}`` so
        their UI converges to the correct state instead of throwing.
      * Any unexpected exception is logged and surfaced as a clean JSON
        error rather than letting a 400/500 leak from middleware with no
        diagnostic body.
    """
    # First the broad role gate (cheap, doesn't hit the row).
    if not can_delete_incidents(request.user):
        if is_ajax_request(request):
            return JsonResponse({"ok": False, "error": "Bạn không có quyền xoá sự việc."}, status=403)
        return HttpResponseForbidden("Bạn không có quyền xoá sự việc.")

    try:
        incident = Incident.objects.filter(pk=pk).first()
    except (ValueError, TypeError):
        if is_ajax_request(request):
            return JsonResponse({"ok": False, "error": "Mã tin nhắn không hợp lệ."}, status=400)
        return HttpResponseBadRequest("Mã tin nhắn không hợp lệ.")

    if incident is None:
        # Already gone — for AJAX, treat as success so the row drops out of
        # the UI cleanly. For full-page, fall through to a plain 404.
        if is_ajax_request(request):
            return JsonResponse({"ok": True, "incident_id": pk, "already_deleted": True})
        raise Http404("Tin nhắn không còn tồn tại.")

    if not incident.can_delete(request.user):
        if is_ajax_request(request):
            return JsonResponse(
                {"ok": False, "error": "Bạn không có quyền xoá tin nhắn này."},
                status=403,
            )
        return HttpResponseForbidden("Bạn không có quyền xoá tin nhắn này.")

    deleted_pk = incident.id
    try:
        if incident.evidence:
            incident.evidence.delete(save=False)
        incident.delete()
        notify_live_update()
        notify_incidents_changed(kind="delete", deleted_ids=[deleted_pk])
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Unexpected failure deleting incident %s", pk)
        if is_ajax_request(request):
            return JsonResponse(
                {"ok": False, "error": "Lỗi máy chủ khi xoá tin nhắn. Vui lòng thử lại."},
                status=500,
            )
        messages.error(request, "Lỗi máy chủ khi xoá tin nhắn. Vui lòng thử lại.")
        return redirect("violations:dashboard")

    if is_ajax_request(request):
        return JsonResponse({"ok": True, "incident_id": deleted_pk})

    messages.success(request, "Đã xoá tin nhắn thành công.")
    return redirect("violations:dashboard")


# ── Incident bulk select / delete ─────────────────────────────────────────────

def _user_deletable_incident_qs(user):
    """Queryset of incidents this user can delete.

    Mirrors ``Incident.can_delete`` but at the queryset level so we can
    paginate / cap without loading every row into memory:

    * Super admin / Django superuser → every incident.
    * Room admin → only ``created_by=user``.
    * Anything else → empty (caller should short-circuit before hitting
      the DB, but the empty queryset keeps callsites uniform).
    """
    if not getattr(user, "is_authenticated", False):
        return Incident.objects.none()
    if user.is_superuser or user.groups.filter(name="super_admin").exists():
        return Incident.objects.all()
    if user.groups.filter(name="room_admin").exists():
        return Incident.objects.filter(created_by_id=user.id)
    return Incident.objects.none()


@require_GET
@login_required
def incidents_deletable_ids(request):
    """Return up to ``INCIDENT_BULK_DELETE_MAX`` newest deletable incident IDs.

    Used by the "Chọn tất cả" master checkbox in selection mode: clicking
    master should select every row the user is allowed to delete, but no
    more than the bulk cap. Anything beyond the cap is excluded so the
    server-side validator can keep its strict ≤ cap rule without any
    client-side trimming gymnastics.

    The list is sorted DESC by id (newest first), matching the on-screen
    ordering of the chat. The IDs are integers, ready to feed straight
    back into the bulk-delete POST body.
    """
    if not can_delete_incidents(request.user):
        return JsonResponse({"ok": False, "error": "Bạn không có quyền xoá tin nhắn."}, status=403)

    qs = _user_deletable_incident_qs(request.user).order_by("-id")
    ids = list(qs.values_list("id", flat=True)[:INCIDENT_BULK_DELETE_MAX])
    total = qs.count()
    return JsonResponse({
        "ok": True,
        "ids": ids,
        "total": total,
        "max": INCIDENT_BULK_DELETE_MAX,
        "capped": total > INCIDENT_BULK_DELETE_MAX,
    })


@require_POST
@login_required
def incidents_bulk_delete(request):
    """Delete up to ``INCIDENT_BULK_DELETE_MAX`` incidents in one call.

    Per-row permission is enforced against ``Incident.can_delete``: a room
    admin who somehow includes a super admin's incident ID in their POST
    will see that ID rejected (returned in ``forbidden_ids``) instead of
    silently failing. The endpoint deletes whatever it CAN delete and
    reports both successes and rejections, so the client can refresh
    accurately.

    Validation order (cheap → expensive):
      1. Role gate (any deletable rights at all?).
      2. ID parsing + cap.
      3. Per-row permission (one query, evaluated row by row in Python so
         the `can_delete` rules stay in one place — the model method).
      4. Atomic delete + WebSocket broadcast.
    """
    if not can_delete_incidents(request.user):
        return JsonResponse({"ok": False, "error": "Bạn không có quyền xoá tin nhắn."}, status=403)

    # Accept ids in any of the three idiomatic shapes the JS client may send:
    #   1. ids=1&ids=2&ids=3            -> getlist returns ["1","2","3"]
    #   2. ids=1,2,3                    -> getlist returns ["1,2,3"]
    #   3. ids[]=1&ids[]=2              -> normalized via getlist("ids[]")
    raw_ids = request.POST.getlist("ids") or request.POST.getlist("ids[]")
    handle = None

    cleaned_ids = []
    for token in raw_ids:
        # Each item may itself be a comma-joined string ("1,2,3") so we
        # split before parsing — handles both shapes uniformly.
        for piece in str(token).split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                value = int(piece)
            except (TypeError, ValueError):
                continue
            if 0 < value <= _MAX_ID_VALUE:
                cleaned_ids.append(value)

    # De-dup while preserving order (so the response's `deleted_ids` list
    # reflects the user's intent without duplicates).
    cleaned_ids = list(dict.fromkeys(cleaned_ids))

    if not cleaned_ids:
        return JsonResponse({"ok": False, "error": "Chưa có tin nhắn nào được chọn."}, status=400)

    if len(cleaned_ids) > INCIDENT_BULK_DELETE_MAX:
        return JsonResponse(
            {
                "ok": False,
                "error": f"Không thể thao tác nhiều hơn {INCIDENT_BULK_DELETE_MAX} tin nhắn.",
                "max": INCIDENT_BULK_DELETE_MAX,
            },
            status=400,
        )

    # Load only the requested rows. We deliberately do NOT pre-filter by
    # the role-scoped queryset here — we want to surface "missing" vs
    # "forbidden" as separate categories in the response so the UI can
    # explain why a given checkbox didn't disappear.
    incidents = list(Incident.objects.filter(pk__in=cleaned_ids))
    found_ids = {inc.id for inc in incidents}
    missing_ids = [pk for pk in cleaned_ids if pk not in found_ids]

    deletable = []
    forbidden_ids = []
    for incident in incidents:
        if incident.can_delete(request.user):
            deletable.append(incident)
        else:
            forbidden_ids.append(incident.id)

    if not deletable:
        # Nothing to do — but tell the client what happened so it can
        # explain the no-op (vs a silent 200 that confuses the user).
        return JsonResponse(
            {
                "ok": False,
                "error": "Không có tin nhắn nào trong số đã chọn có thể xoá.",
                "forbidden_ids": forbidden_ids,
                "missing_ids": missing_ids,
            },
            status=403,
        )

    # Acquire the bulk-delete lock just before we start mutating. Doing it
    # AFTER validation means a parallel selection-mode user who happens to
    # send an empty / invalid POST never trips the lock and never causes a
    # spurious "đang xoá" toast on every other admin's screen.
    handle, busy = _acquire_incident_bulk_lock_or_busy(
        request, operation="incident_bulk_delete"
    )
    if busy is not None:
        return busy

    deleted_ids = []
    try:
        with transaction.atomic():
            for incident in deletable:
                # Capture the PK BEFORE the delete — Django sets ``pk`` to
                # ``None`` on the in-memory instance after the row is gone.
                pk_to_record = incident.id
                # Evidence files live on disk — drop them BEFORE the row so
                # we don't orphan blobs if the row delete somehow fails.
                if incident.evidence:
                    try:
                        incident.evidence.delete(save=False)
                    except Exception:
                        # Disk delete failures should not block the row
                        # delete; we'll log but continue.
                        import logging
                        logging.getLogger(__name__).exception(
                            "Failed to delete evidence file for incident %s during bulk delete",
                            pk_to_record,
                        )
                incident.delete()
                deleted_ids.append(pk_to_record)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "incidents_bulk_delete failed; ids_count=%s", len(deletable)
        )
        _release_incident_bulk_lock_quietly(handle)
        return JsonResponse(
            {"ok": False, "error": "Lỗi máy chủ khi xoá hàng loạt tin nhắn."},
            status=500,
        )

    # Single broadcast for the whole batch — every connected dashboard
    # will drop the matching ``.chat-row`` elements at once, instead of N
    # times. ``notify_live_update`` is kept for any legacy listeners that
    # only know how to react to the generic ping.
    notify_live_update()
    notify_incidents_changed(kind="bulk_delete", deleted_ids=deleted_ids)
    _release_incident_bulk_lock_quietly(handle)

    return JsonResponse({
        "ok": True,
        "deleted_ids": deleted_ids,
        "deleted_count": len(deleted_ids),
        "forbidden_ids": forbidden_ids,
        "missing_ids": missing_ids,
    })


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
    note_count = 0
    for incident in incidents:
        kind = incident.incident_kind
        if kind == Incident.KIND_REMINDER:
            reminder_count += 1
        elif kind == Incident.KIND_NOTE:
            # "Ghi chú" is informational only — it shows in the incident
            # list under the candidate but never contributes to the
            # violation tally or the cấm-thi rule.
            note_count += 1
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
                "note_count": note_count,
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
        return redirect("violations:statistics")

    csv_file = form.cleaned_data["csv_file"]

    # Enforce upload size limit (5 MB)
    if csv_file.size > _MAX_CSV_SIZE:
        messages.error(request, "Tệp CSV phải nhỏ hơn 5 MB.")
        return redirect("violations:statistics")

    try:
        raw_content = csv_file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        messages.error(
            request,
            "Tệp CSV phải được mã hoá UTF-8. Vui lòng lưu lại tệp với encoding UTF-8 và thử lại.",
        )
        return redirect("violations:statistics")
    reader = csv.DictReader(io.StringIO(raw_content))

    if not reader.fieldnames:
        messages.error(request, "CSV phải có dòng tiêu đề cột.")
        return redirect("violations:statistics")

    header_map = {normalize_header(h): h for h in reader.fieldnames}

    def row_value(row, keys, default=""):
        for key in keys:
            raw_header = header_map.get(key)
            if raw_header and row.get(raw_header):
                return row[raw_header].strip()
        return default

    # Overwrite policy: this endpoint is the canonical "load the official
    # candidate roster" action. Any thí sinh that doesn't appear in the new
    # CSV must disappear from the database, regardless of whether they were
    # added by a previous import or by hand. We do NOT update_or_create per
    # row — we wipe and reload inside one transaction so a parse failure
    # rolls everything back.

    # First pass: parse the CSV in memory so we don't truncate the table
    # before we've validated the input. If the CSV is malformed, we want
    # the existing roster to remain intact.
    parsed_rows = []
    truncated_count = 0
    for row in reader:
        raw_sbd = row_value(row, ["sbd", "sobaodanh"])
        if not raw_sbd:
            continue
        sbd, was_truncated = apply_default_prefix(raw_sbd)
        if not sbd or not is_valid_sbd_syntax(sbd):
            continue
        parsed_rows.append({
            "sbd": sbd,
            "full_name": row_value(row, ["hovaten", "fullname", "name"], default="Chưa rõ")[:150],
            "school": row_value(row, ["truong", "school"], default="Chưa rõ")[:150],
            "supervisor_teacher": row_value(
                row, ["gvpt", "giaovienphutrach", "supervisorteacher"], default="Chưa rõ"
            )[:150],
            "exam_room": row_value(row, ["phongthi", "examroom", "room"])[:50],
        })
        if was_truncated:
            truncated_count += 1

    if not parsed_rows:
        messages.error(
            request,
            "Tệp CSV không có dòng thí sinh hợp lệ. Vui lòng kiểm tra lại "
            "(cột SBD bắt buộc, mỗi SBD chỉ chứa chữ cái và chữ số).",
        )
        return redirect("violations:statistics")

    # De-duplicate by SBD: if the CSV repeats a SBD, last entry wins (this
    # matches the prior behaviour of update_or_create iterating top-to-bottom).
    deduped = {row["sbd"]: row for row in parsed_rows}

    # Second pass: atomic wipe + reload + re-link. SET_NULL on the FKs from
    # Incident/IncidentParticipant means deleting candidates does not lose
    # any incident history; we just temporarily orphan the references and
    # immediately rebind them via _relink.
    #
    # CSV reload is the heaviest candidate mutation we have — it wipes and
    # rewrites the entire roster — so we acquire the global mutation lock
    # for the whole operation. The page is a regular form POST (not AJAX),
    # so when busy we surface the message via Django's `messages` framework
    # rather than a JSON 409.
    handle, busy = _acquire_candidate_lock_or_busy(request, operation="csv_reload")
    if busy is not None:
        messages.error(request, BUSY_USER_MESSAGE)
        return redirect("violations:statistics")

    try:
        with transaction.atomic():
            Candidate.objects.all().delete()
            Candidate.objects.bulk_create([
                Candidate(
                    sbd=row["sbd"],
                    full_name=row["full_name"],
                    school=row["school"],
                    supervisor_teacher=row["supervisor_teacher"],
                    exam_room=row["exam_room"],
                )
                for row in deduped.values()
            ])
            relinked_incidents, relinked_participants = _relink_candidates_to_references(
                list(deduped.keys())
            )

        imported_count = len(deduped)
        summary = (
            f"Đã ghi đè danh sách thí sinh: {imported_count} thí sinh trong dữ liệu mới."
        )
        if truncated_count:
            summary += f" {truncated_count} SBD đã được cắt ngắn cho vừa {MAX_SBD_LENGTH} ký tự."
        if relinked_incidents or relinked_participants:
            summary += (
                f" Đã gắn lại {relinked_incidents} tin nhắn chính và "
                f"{relinked_participants} lượt nhắc với hồ sơ thí sinh."
            )
        messages.success(request, summary)
        notify_live_update()
        # CSV reload affects every SBD — pass the full list so clients can
        # do a thorough cache invalidation. Empty-list semantics (or a
        # very large list) signal "refresh broadly" on the JS side.
        notify_candidates_changed(
            kind="csv_reload",
            affected_sbds=list(deduped.keys()),
        )
        return redirect("violations:statistics")
    finally:
        _release_candidate_lock_quietly(handle)


# ── Candidate CRUD (super admin) ──────────────────────────────────────────────

# Field length caps from the model — kept in one place so trimming is consistent
# across create/update endpoints. Mirrors `Candidate` model field max_lengths.
_CANDIDATE_NAME_MAX = 150
_CANDIDATE_SCHOOL_MAX = 150
_CANDIDATE_TEACHER_MAX = 150
_CANDIDATE_ROOM_MAX = 50

# Hard ceiling for bulk-delete payloads — protects the DB from a runaway POST
# trying to delete millions of rows in one transaction.
_CANDIDATE_BULK_DELETE_LIMIT = 5000


def _busy_response():
    """Standard 409 payload sent when the candidate-mutation lock is held.

    Front-end JS keys off ``busy=True`` and the Vietnamese message to keep
    the user's modal open and surface the "thử lại sau" toast. Owner info
    is included so views/tooltips can show who is currently mutating.
    """
    state = get_lock_state()
    return JsonResponse(
        {
            "ok": False,
            "busy": True,
            "error": BUSY_USER_MESSAGE,
            "owner_username": state.owner_username,
            "owner_user_id": state.owner_user_id,
            "operation": state.operation,
        },
        status=409,
    )


def _acquire_candidate_lock_or_busy(request, *, operation):
    """Try to grab the global candidate-mutation lock for this request.

    Returns ``(handle, None)`` on success — the caller MUST release the
    handle in a ``finally`` block — or ``(None, busy_response)`` if another
    super-admin is already mutating, in which case the caller returns the
    response directly. The acquisition is non-blocking so the UX stays
    snappy: the second writer hears 409 immediately rather than waiting.

    On success this also broadcasts ``busy=True`` to every connected
    socket so the owner's browser can pop the "Đang cập nhật" modal and
    other admins can show a passive indicator.
    """
    handle = acquire_candidate_lock(
        user_id=getattr(request.user, "id", None),
        username=getattr(request.user, "username", "") or "",
        operation=operation,
    )
    if handle is None:
        return None, _busy_response()

    notify_candidates_lock(
        busy=True,
        owner_user_id=handle.owner_user_id,
        owner_username=handle.owner_username,
        operation=handle.operation,
    )
    return handle, None


def _release_candidate_lock_quietly(handle):
    """Release the lock and broadcast ``busy=False``.

    ``release_candidate_lock`` is token-checked so calling this from a
    ``finally`` block is always safe — even if the lock has already been
    auto-released by the cache TTL, the broadcast still tells clients the
    owner is no longer holding it (idempotent UI dismissal).
    """
    if handle is None:
        return
    release_candidate_lock(handle)
    notify_candidates_lock(
        busy=False,
        owner_user_id=handle.owner_user_id,
        owner_username=handle.owner_username,
        operation=handle.operation,
    )


# ── Incident bulk-delete lock helpers ────────────────────────────────────────
#
# Mirror the candidate-lock helpers but for the incident bulk-delete flow.
# The two locks live independently in the cache (different keys) so a
# super-admin running a CSV reload does not block a room-admin running a
# bulk-delete pass and vice versa — they only block their own kind.

def _incident_busy_response():
    """Standard 409 payload sent when the incident bulk-delete lock is held."""
    state = get_incident_bulk_lock_state()
    return JsonResponse(
        {
            "ok": False,
            "busy": True,
            "error": INCIDENT_BUSY_USER_MESSAGE,
            "owner_username": state.owner_username,
            "owner_user_id": state.owner_user_id,
            "operation": state.operation,
        },
        status=409,
    )


def _acquire_incident_bulk_lock_or_busy(request, *, operation):
    """Try to grab the incident bulk-delete lock for this request."""
    handle = acquire_incident_bulk_lock(
        user_id=getattr(request.user, "id", None),
        username=getattr(request.user, "username", "") or "",
        operation=operation,
    )
    if handle is None:
        return None, _incident_busy_response()

    notify_incidents_lock(
        busy=True,
        owner_user_id=handle.owner_user_id,
        owner_username=handle.owner_username,
        operation=handle.operation,
    )
    return handle, None


def _release_incident_bulk_lock_quietly(handle):
    """Release the incident bulk-delete lock and broadcast ``busy=False``."""
    if handle is None:
        return
    release_incident_bulk_lock(handle)
    notify_incidents_lock(
        busy=False,
        owner_user_id=handle.owner_user_id,
        owner_username=handle.owner_username,
        operation=handle.operation,
    )


def _refuse_if_incident_bulk_busy(request):
    """Cheap pre-flight: if a bulk-delete is in flight, refuse with 409.

    Used by ``create_incident`` and ``edit_incident`` so a freshly-posted
    or freshly-edited message cannot collide with the deletion pass that
    is about to wipe a swath of rows. The check is read-only (no lock
    acquisition) — we do NOT want to acquire the lock here, just observe
    that someone else is holding it.

    Returns a JsonResponse for AJAX callers and a redirect-with-error for
    full-page submits, or ``None`` when there is no contention and the
    caller should proceed.
    """
    state = get_incident_bulk_lock_state()
    if not state.busy:
        return None
    if is_ajax_request(request):
        return _incident_busy_response()
    messages.warning(request, INCIDENT_BUSY_USER_MESSAGE)
    return redirect("violations:dashboard")


def _candidate_to_dict(candidate):
    """Serialise a Candidate row for JSON responses. Stable shape for the JS."""
    return {
        "id": candidate.id,
        "sbd": candidate.sbd,
        "full_name": candidate.full_name,
        "school": candidate.school,
        "supervisor_teacher": candidate.supervisor_teacher,
        "exam_room": candidate.exam_room,
    }


def _clean_candidate_payload(post, *, require_sbd=True):
    """Validate and normalise candidate fields from a POST payload.

    Returns ``(cleaned_dict, error_message_or_None)``. Trims and length-caps
    every field so we never violate ``max_length``. SBD is upper-cased and run
    through the same default-prefix logic as the import pipeline so a user
    typing pure digits gets the same treatment as everywhere else.
    """
    raw_sbd = (post.get("sbd") or "").strip()
    if require_sbd and not raw_sbd:
        return None, "Vui lòng nhập SBD."
    if raw_sbd and not is_valid_sbd_syntax(raw_sbd):
        return None, "SBD không hợp lệ: chỉ dùng chữ cái tiếng Anh và chữ số (tối đa 9 ký tự)."

    canonical_sbd, _ = apply_default_prefix(raw_sbd) if raw_sbd else ("", False)

    full_name = (post.get("full_name") or "").strip()[:_CANDIDATE_NAME_MAX]
    school = (post.get("school") or "").strip()[:_CANDIDATE_SCHOOL_MAX]
    teacher = (post.get("supervisor_teacher") or "").strip()[:_CANDIDATE_TEACHER_MAX]
    exam_room = (post.get("exam_room") or "").strip()[:_CANDIDATE_ROOM_MAX]

    return {
        "sbd": canonical_sbd,
        "full_name": full_name or "Chưa rõ",
        "school": school or "Chưa rõ",
        "supervisor_teacher": teacher or "Chưa rõ",
        "exam_room": exam_room,
    }, None


def _next_unused_sbd():
    """Compute a placeholder SBD that does not clash with any existing row.

    Strategy: scan rows whose SBD matches ``TS<digits>``, take the max numeric
    suffix, return ``TS<max+1>`` zero-padded to 5 digits. Falls back to a
    linear probe if formatting overflows ``MAX_SBD_LENGTH``.
    """
    digits_re = re.compile(r"^TS(\d+)$")
    max_n = 0
    for sbd in Candidate.objects.filter(sbd__regex=r"^TS\d+$").values_list("sbd", flat=True):
        m = digits_re.match(sbd)
        if m:
            try:
                n = int(m.group(1))
            except ValueError:
                continue
            if n > max_n:
                max_n = n

    candidate_n = max_n + 1
    while True:
        suffix = str(candidate_n)
        # Prefer 5-digit zero-pad if it fits; otherwise raw digits, clamped.
        formatted = f"TS{suffix.zfill(5)}" if len(suffix) <= 5 else f"TS{suffix}"
        formatted = formatted[:MAX_SBD_LENGTH]
        if not Candidate.objects.filter(sbd=formatted).exists():
            return formatted
        candidate_n += 1
        # Pathological safety net — should never hit this in practice.
        if candidate_n - max_n > 10_000:
            raise RuntimeError("Could not allocate a free SBD after 10000 attempts.")


@require_POST
@login_required
def candidate_create(request):
    """Create a single candidate. Super-admin only.

    JS calls this with no body to mean "give me a sample row to edit"; in that
    case we synthesise a unique placeholder SBD and standard sample values so
    the new row appears immediately. JS may also pass full data to create a
    specific row.
    """
    if not is_super_admin(request.user):
        return JsonResponse({"ok": False, "error": "Chỉ Quản trị tổng được thao tác."}, status=403)

    # Validate the payload BEFORE acquiring the global mutation lock — a
    # garbage payload should not block other admins for the duration of the
    # round-trip. The lock is only worth holding for the actual DB write.
    raw_sbd = (request.POST.get("sbd") or "").strip()
    if not raw_sbd:
        # No SBD provided → sample row.
        sbd = _next_unused_sbd()
        full_name = (request.POST.get("full_name") or "Nguyễn Văn A").strip()[:_CANDIDATE_NAME_MAX]
        school = (request.POST.get("school") or "THCS ABC").strip()[:_CANDIDATE_SCHOOL_MAX]
        teacher = (request.POST.get("supervisor_teacher") or "Chưa rõ").strip()[:_CANDIDATE_TEACHER_MAX]
        exam_room = (request.POST.get("exam_room") or "").strip()[:_CANDIDATE_ROOM_MAX]
    else:
        cleaned, err = _clean_candidate_payload(request.POST)
        if err:
            return JsonResponse({"ok": False, "error": err}, status=400)
        sbd = cleaned["sbd"]
        full_name = cleaned["full_name"]
        school = cleaned["school"]
        teacher = cleaned["supervisor_teacher"]
        exam_room = cleaned["exam_room"]

    handle, busy = _acquire_candidate_lock_or_busy(request, operation="candidate_create")
    if busy is not None:
        return busy

    try:
        try:
            with transaction.atomic():
                candidate, created = Candidate.objects.get_or_create(
                    sbd=sbd,
                    defaults={
                        "full_name": full_name,
                        "school": school,
                        "supervisor_teacher": teacher,
                        "exam_room": exam_room,
                    },
                )
                if not created:
                    return JsonResponse(
                        {"ok": False, "error": f"SBD {sbd} đã tồn tại."},
                        status=409,
                    )
                # New SBD might match orphaned incidents — relink so historical
                # references attach to the new candidate row.
                _relink_candidates_to_references([sbd])
        except Exception:
            import logging
            logging.getLogger(__name__).exception("candidate_create failed")
            return JsonResponse(
                {"ok": False, "error": "Lỗi máy chủ khi thêm thí sinh."},
                status=500,
            )

        # Broadcast the row change AFTER the commit so subscribers don't
        # race-fetch a row that hasn't landed yet.
        notify_candidates_changed(
            kind="create",
            candidate_id=candidate.id,
            sbd=candidate.sbd,
            affected_sbds=[candidate.sbd],
        )
        return JsonResponse({"ok": True, "candidate": _candidate_to_dict(candidate)})
    finally:
        _release_candidate_lock_quietly(handle)


@require_POST
@login_required
def candidate_update(request, pk):
    """Edit one candidate. Super-admin only."""
    if not is_super_admin(request.user):
        return JsonResponse({"ok": False, "error": "Chỉ Quản trị tổng được thao tác."}, status=403)

    candidate = Candidate.objects.filter(pk=pk).first()
    if candidate is None:
        return JsonResponse(
            {"ok": False, "error": "Không tìm thấy thí sinh.", "missing": True},
            status=404,
        )

    cleaned, err = _clean_candidate_payload(request.POST)
    if err:
        return JsonResponse({"ok": False, "error": err}, status=400)

    new_sbd = cleaned["sbd"]
    sbd_changed = (new_sbd != candidate.sbd)
    old_sbd = candidate.sbd

    if sbd_changed and Candidate.objects.exclude(pk=pk).filter(sbd=new_sbd).exists():
        return JsonResponse(
            {"ok": False, "error": f"SBD {new_sbd} đã thuộc về thí sinh khác."},
            status=409,
        )

    handle, busy = _acquire_candidate_lock_or_busy(request, operation="candidate_update")
    if busy is not None:
        return busy

    try:
        try:
            with transaction.atomic():
                candidate.sbd = new_sbd
                candidate.full_name = cleaned["full_name"]
                candidate.school = cleaned["school"]
                candidate.supervisor_teacher = cleaned["supervisor_teacher"]
                candidate.exam_room = cleaned["exam_room"]
                candidate.save()
                if sbd_changed:
                    # The SBD changed — orphans for the new SBD become linkable,
                    # and the old SBD's incidents stay attached because FKs are
                    # by candidate id, not by SBD string.
                    _relink_candidates_to_references([new_sbd])
        except Exception:
            import logging
            logging.getLogger(__name__).exception("candidate_update failed for pk=%s", pk)
            return JsonResponse(
                {"ok": False, "error": "Lỗi máy chủ khi cập nhật thí sinh."},
                status=500,
            )

        # On rename, both old and new SBDs need their tooltip/stat caches
        # invalidated client-side: the old key disappears from the row, the
        # new key needs to be hydrated.
        affected = [candidate.sbd]
        if sbd_changed and old_sbd:
            affected.append(old_sbd)
        notify_candidates_changed(
            kind="update",
            candidate_id=candidate.id,
            sbd=candidate.sbd,
            old_sbd=old_sbd if sbd_changed else "",
            affected_sbds=affected,
        )
        return JsonResponse({"ok": True, "candidate": _candidate_to_dict(candidate)})
    finally:
        _release_candidate_lock_quietly(handle)


@require_POST
@login_required
def candidate_delete(request, pk):
    """Delete one candidate. Super-admin only.

    Incident/IncidentParticipant FKs use SET_NULL, so deleting a candidate
    keeps incident history intact (those rows simply lose their candidate FK
    and will display "không tìm thấy hồ sơ thí sinh").
    """
    if not is_super_admin(request.user):
        return JsonResponse({"ok": False, "error": "Chỉ Quản trị tổng được thao tác."}, status=403)

    candidate = Candidate.objects.filter(pk=pk).first()
    if candidate is None:
        # Race: already deleted — converge the UI cleanly.
        return JsonResponse({"ok": True, "id": pk, "already_deleted": True})

    handle, busy = _acquire_candidate_lock_or_busy(request, operation="candidate_delete")
    if busy is not None:
        return busy

    try:
        # Cache identifiers before delete() so the broadcast can name the
        # row that was removed even though the model instance is gone.
        deleted_id = candidate.id
        deleted_sbd = candidate.sbd
        try:
            candidate.delete()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("candidate_delete failed for pk=%s", pk)
            return JsonResponse(
                {"ok": False, "error": "Lỗi máy chủ khi xoá thí sinh."},
                status=500,
            )

        notify_candidates_changed(
            kind="delete",
            candidate_id=deleted_id,
            sbd=deleted_sbd,
            affected_sbds=[deleted_sbd] if deleted_sbd else [],
        )
        return JsonResponse({"ok": True, "id": pk})
    finally:
        _release_candidate_lock_quietly(handle)


@require_POST
@login_required
def candidate_bulk_delete(request):
    """Delete many candidates in one atomic operation. Super-admin only.

    Accepts ``ids`` either as a repeated form field (``ids=1&ids=2``) or as a
    comma-separated string. Caps the batch at ``_CANDIDATE_BULK_DELETE_LIMIT``
    so a malicious or runaway client cannot ask the DB to delete the world.
    """
    if not is_super_admin(request.user):
        return JsonResponse({"ok": False, "error": "Chỉ Quản trị tổng được thao tác."}, status=403)

    # Accept ids in any of the three idiomatic shapes the JS client may send:
    #   1. ids=1&ids=2&ids=3            -> getlist returns ["1","2","3"]
    #   2. ids=1,2,3                    -> getlist returns ["1,2,3"]
    #   3. ids[]=1&ids[]=2              -> normalized via getlist("ids[]")
    raw_ids = request.POST.getlist("ids") or request.POST.getlist("ids[]")

    cleaned_ids = []
    for token in raw_ids:
        # Each item may itself be a comma-joined string ("1,2,3") so we
        # split before parsing — handles both shapes uniformly.
        for piece in str(token).split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                value = int(piece)
            except (TypeError, ValueError):
                continue
            if 0 < value <= _MAX_ID_VALUE:
                cleaned_ids.append(value)

    if not cleaned_ids:
        return JsonResponse({"ok": False, "error": "Không có thí sinh nào được chọn."}, status=400)

    if len(cleaned_ids) > _CANDIDATE_BULK_DELETE_LIMIT:
        return JsonResponse(
            {"ok": False, "error": f"Mỗi lần chỉ được xoá tối đa {_CANDIDATE_BULK_DELETE_LIMIT} thí sinh."},
            status=400,
        )

    # De-dup while preserving order
    cleaned_ids = list(dict.fromkeys(cleaned_ids))

    handle, busy = _acquire_candidate_lock_or_busy(request, operation="candidate_bulk_delete")
    if busy is not None:
        return busy

    try:
        # Capture SBDs BEFORE delete so the broadcast can name them; once the
        # rows are gone we lose the link.
        affected_sbds = list(
            Candidate.objects.filter(pk__in=cleaned_ids).values_list("sbd", flat=True)
        )
        try:
            with transaction.atomic():
                deleted_count, _ = Candidate.objects.filter(pk__in=cleaned_ids).delete()
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "candidate_bulk_delete failed; ids_count=%s", len(cleaned_ids)
            )
            return JsonResponse(
                {"ok": False, "error": "Lỗi máy chủ khi xoá hàng loạt."},
                status=500,
            )

        notify_candidates_changed(
            kind="bulk_delete",
            affected_sbds=affected_sbds,
        )
        return JsonResponse({"ok": True, "deleted": deleted_count, "ids": cleaned_ids})
    finally:
        _release_candidate_lock_quietly(handle)


@require_GET
@login_required
def candidate_export_csv(request):
    """Stream the entire candidate roster as CSV. Super-admin only.

    Uses ``StreamingHttpResponse`` with a generator so the response starts
    sending bytes before the queryset is fully materialised — keeps memory
    flat even with very large rosters.
    """
    if not is_super_admin(request.user):
        return HttpResponseForbidden("Chỉ Quản trị tổng được tải xuống danh sách thí sinh.")

    class _Echo:
        """A file-like that just returns the bytes written to it. Stdlib
        recipe for streaming CSVs in Django."""
        def write(self, value):
            return value

    writer = csv.writer(_Echo())
    headers = ["SBD", "Ho va ten", "Truong", "GVPT", "Phong thi"]

    def _rows():
        # UTF-8 BOM so Excel on Windows opens it as UTF-8.
        yield "\ufeff"
        yield writer.writerow(headers)
        for candidate in Candidate.objects.all().order_by("sbd").iterator(chunk_size=500):
            yield writer.writerow([
                candidate.sbd,
                candidate.full_name,
                candidate.school,
                candidate.supervisor_teacher,
                candidate.exam_room,
            ])

    response = StreamingHttpResponse(_rows(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="candidates.csv"'
    response["Cache-Control"] = "no-store"
    return response


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


@login_required
def change_password_view(request):
    """Let the signed-in admin rotate their own password.

    Uses Django's built-in PasswordChangeForm (via ``AdminPasswordChangeForm``)
    so the old-password verification, hashing and session-hash refresh all
    follow the framework's hardened path. Custom policy is layered on via
    ``validate_password_strength`` in ``forms.py``.
    """
    if request.method == "POST":
        form = AdminPasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            # Keep the current session alive after the password hash rotates;
            # otherwise Django would log the user out on the next request.
            update_session_auth_hash(request, user)
            messages.success(request, "Đổi mật khẩu thành công!")
            return redirect("violations:dashboard")
    else:
        form = AdminPasswordChangeForm(user=request.user)

    return render(request, "violations/change_password.html", {
        "form": form,
        "role_label": role_label(request.user),
    })


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
