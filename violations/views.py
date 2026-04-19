import csv
import io
import mimetypes
import unicodedata

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.http import FileResponse, Http404, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
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
from .services import normalize_sbd, sync_incident_references
from .ws_events import notify_live_update


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


def is_ajax_request(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def get_user_room_name(user):
    profile = getattr(user, "room_admin_profile", None)
    if profile:
        return profile.room_name
    return ""


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
    value = "".join(char for char in value if not unicodedata.combining(char))
    return value.lower().strip().replace(" ", "").replace("_", "")


def get_dashboard_context(request):
    incidents = fetch_incidents_page(limit=INCIDENT_PAGE_SIZE)

    editable_incident_ids = get_editable_incident_ids(incidents, request.user)

    candidate_stats, unknown_stats = build_candidate_stats()
    oldest_incident_id = incidents[0].id if incidents else None
    newest_incident_id = incidents[-1].id if incidents else None
    has_older = Incident.objects.filter(id__lt=oldest_incident_id).exists() if oldest_incident_id else False

    return {
        "incidents": incidents,
        "editable_incident_ids": editable_incident_ids,
        "current_user_id": request.user.id if request.user.is_authenticated else None,
        "oldest_incident_id": oldest_incident_id,
        "newest_incident_id": newest_incident_id,
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
    context = get_dashboard_context(request)
    return render(request, "violations/dashboard.html", context)


@require_GET
def statistics(request):
    context = get_statistics_context(request)
    return render(request, "violations/statistics.html", context)


@require_POST
@login_required
def create_incident(request):
    if not can_post_message(request.user):
        if is_ajax_request(request):
            return JsonResponse({"ok": False, "error": "You do not have permission to post incidents."}, status=403)
        return HttpResponseForbidden("You do not have permission to post incidents.")

    form = IncidentCreateForm(request.POST, request.FILES)
    if not form.is_valid():
        if is_ajax_request(request):
            error_text = "; ".join(
                f"{field.replace('_', ' ').title()}: {', '.join(errors)}"
                for field, errors in form.errors.items()
            )
            return JsonResponse(
                {
                    "ok": False,
                    "error": error_text or "Invalid incident data.",
                    "errors": form.errors,
                },
                status=400,
            )
        for field, errors in form.errors.items():
            label = field.replace("_", " ").title()
            for error in errors:
                messages.error(request, f"{label}: {error}")
        return redirect("violations:dashboard")

    incident = Incident(
        created_by=request.user,
        room_name=get_user_room_name(request.user),
    )
    evidence = form.cleaned_data.get("evidence")
    if evidence:
        incident.evidence = evidence

    sync_incident_references(
        incident=incident,
        primary_sbd=form.cleaned_data["sbd"],
        violation_text=form.cleaned_data["violation_text"],
    )
    notify_live_update()

    if is_ajax_request(request):
        return JsonResponse({"ok": True})

    messages.success(request, "Incident posted successfully.")
    return redirect("violations:dashboard")


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

            sync_incident_references(
                incident=incident,
                primary_sbd=form.cleaned_data["sbd"],
                violation_text=form.cleaned_data["violation_text"],
            )
            notify_live_update()
            messages.success(request, "Incident updated successfully.")
            return redirect("violations:dashboard")
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


@require_GET
def candidate_detail(request, sbd):
    normalized_sbd = normalize_sbd(sbd)
    candidate = Candidate.objects.filter(sbd__iexact=normalized_sbd).first()
    incidents = Incident.objects.filter(
        participants__sbd_snapshot__iexact=normalized_sbd
    ).select_related("created_by").distinct().order_by("-created_at")

    context = {
        "candidate": candidate,
        "sbd": normalized_sbd,
        "incidents": incidents,
    }
    return render(request, "violations/_candidate_detail.html", context)


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


@require_GET
def live_snapshot(request):
    return JsonResponse(build_live_payload(request.user))


@require_GET
def incident_history(request):
    before_raw = request.GET.get("before")
    if not before_raw:
        return HttpResponseBadRequest("Missing before parameter.")

    try:
        before_id = int(before_raw)
    except ValueError:
        return HttpResponseBadRequest("before must be an integer.")

    incidents = fetch_incidents_page(before_id=before_id, limit=INCIDENT_PAGE_SIZE)
    oldest_id = incidents[0].id if incidents else None
    newest_id = incidents[-1].id if incidents else None
    has_older = Incident.objects.filter(id__lt=oldest_id).exists() if oldest_id else False

    return JsonResponse(
        {
            "incidents_html": render_incident_rows_html(incidents, request.user),
            "oldest_id": oldest_id,
            "newest_id": newest_id,
            "has_older": has_older,
        }
    )


@require_GET
def incident_updates(request):
    after_raw = request.GET.get("after", "0")
    try:
        after_id = int(after_raw)
    except ValueError:
        return HttpResponseBadRequest("after must be an integer.")

    incidents = fetch_incidents_page(after_id=after_id, limit=INCIDENT_UPDATE_LIMIT)
    payload = build_stats_payload()

    payload.update(
        {
            "incidents_html": render_incident_rows_html(incidents, request.user),
            "oldest_id": incidents[0].id if incidents else None,
            "newest_id": incidents[-1].id if incidents else None,
            "added_count": len(incidents),
        }
    )
    return JsonResponse(payload)


@login_required
def import_candidates(request):
    if not is_super_admin(request.user):
        return HttpResponseForbidden("Only Super Admin can import candidate data.")

    if request.method != "POST":
        return redirect("violations:dashboard")

    form = CandidateImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Please upload a valid CSV file.")
        return redirect("violations:dashboard")

    raw_content = form.cleaned_data["csv_file"].read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw_content))

    if not reader.fieldnames:
        messages.error(request, "CSV must include header columns.")
        return redirect("violations:dashboard")

    header_map = {normalize_header(header): header for header in reader.fieldnames}

    def row_value(row, keys, default=""):
        for key in keys:
            raw_header = header_map.get(key)
            if raw_header and row.get(raw_header):
                return row[raw_header].strip()
        return default

    created_count = 0
    updated_count = 0

    for row in reader:
        sbd = normalize_sbd(row_value(row, ["sbd", "sobaodanh"]))
        if not sbd:
            continue

        defaults = {
            "full_name": row_value(row, ["hovaten", "fullname", "name"], default="Unknown"),
            "school": row_value(row, ["truong", "school"], default="Unknown"),
            "supervisor_teacher": row_value(
                row,
                ["gvpt", "giaovienphutrach", "supervisorteacher"],
                default="Unknown",
            ),
            "exam_room": row_value(row, ["phongthi", "examroom", "room"]),
        }

        _, created = Candidate.objects.update_or_create(sbd=sbd, defaults=defaults)
        if created:
            created_count += 1
        else:
            updated_count += 1

    messages.success(
        request,
        f"Candidate import finished. Created: {created_count}, Updated: {updated_count}.",
    )
    notify_live_update()
    return redirect("violations:dashboard")


def login_view(request):
    if request.user.is_authenticated:
        return redirect("violations:dashboard")

    form = AuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect("violations:dashboard")

    return render(
        request,
        "violations/login.html",
        {
            "form": form,
            "role_label": role_label(request.user),
        },
    )


@login_required
def logout_view(request):
    logout(request)
    return redirect("violations:dashboard")
