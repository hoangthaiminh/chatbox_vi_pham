from django.urls import path

from . import views

app_name = "violations"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("statistics/", views.statistics, name="statistics"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("change-password/", views.change_password_view, name="change_password"),
    path("incidents/create/", views.create_incident, name="create_incident"),
    path("incidents/preview/", views.incident_preview, name="incident_preview"),
    path("incidents/upload-image/", views.upload_image, name="upload_image"),
    path("incidents/<int:pk>/edit/", views.edit_incident, name="edit_incident"),
    path("incidents/<int:pk>/delete/", views.delete_incident, name="delete_incident"),
    path("incidents/<int:pk>/evidence/", views.incident_evidence, name="incident_evidence"),
    path("api/incidents/history/", views.incident_history, name="incident_history"),
    path("api/incidents/updates/", views.incident_updates, name="incident_updates"),
    path("api/incidents/deletable-ids/", views.incidents_deletable_ids, name="incidents_deletable_ids"),
    path("api/incidents/bulk-delete/", views.incidents_bulk_delete, name="incidents_bulk_delete"),
    path("stats/candidate/<str:sbd>/", views.candidate_detail, name="candidate_detail"),
    path("import-candidates/", views.import_candidates, name="import_candidates"),
    path("candidates/export.csv", views.candidate_export_csv, name="candidate_export_csv"),
    path("api/live/", views.live_snapshot, name="live_snapshot"),
    path("api/candidates/search/", views.candidate_search, name="candidate_search"),
    path("api/candidates/", views.candidate_create, name="candidate_create"),
    path("api/candidates/bulk-delete/", views.candidate_bulk_delete, name="candidate_bulk_delete"),
    path("api/candidates/<int:pk>/update/", views.candidate_update, name="candidate_update"),
    path("api/candidates/<int:pk>/delete/", views.candidate_delete, name="candidate_delete"),
]
