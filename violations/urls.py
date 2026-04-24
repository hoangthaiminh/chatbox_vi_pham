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
    path("stats/candidate/<str:sbd>/", views.candidate_detail, name="candidate_detail"),
    path("import-candidates/", views.import_candidates, name="import_candidates"),
    path("api/live/", views.live_snapshot, name="live_snapshot"),
    path("api/candidates/search/", views.candidate_search, name="candidate_search"),
]
