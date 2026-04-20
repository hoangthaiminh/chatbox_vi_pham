import pytest
from django.contrib.auth.models import Group, User

from violations.models import Incident

pytestmark = pytest.mark.django_db


@pytest.fixture
def room_admin_user(db):
    user = User.objects.create_user(username="del-room-admin", password="x")
    grp, _ = Group.objects.get_or_create(name="room_admin")
    user.groups.add(grp)
    return user


@pytest.fixture
def viewer_user(db):
    return User.objects.create_user(username="del-viewer", password="x")


@pytest.fixture
def incident(viewer_user):
    return Incident.objects.create(
        reported_sbd="TS0032",
        violation_text="test message",
        created_by=viewer_user,
    )


class TestDeleteIncidentAjax:
    def test_admin_delete_json(self, client, room_admin_user, incident):
        client.force_login(room_admin_user)
        res = client.post(
            f"/incidents/{incident.id}/delete/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert res.status_code == 200
        assert res.json() == {"ok": True, "incident_id": incident.id}
        assert not Incident.objects.filter(id=incident.id).exists()

    def test_viewer_forbidden_json(self, client, viewer_user, incident):
        client.force_login(viewer_user)
        res = client.post(
            f"/incidents/{incident.id}/delete/",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert res.status_code == 403
        assert "error" in res.json()
        assert Incident.objects.filter(id=incident.id).exists()


class TestDeleteButtonVisibility:
    def test_dashboard_has_delete_button_for_admin(self, client, room_admin_user, incident):
        client.force_login(room_admin_user)
        res = client.get("/")
        assert res.status_code == 200
        assert f"/incidents/{incident.id}/delete/" in res.content.decode("utf-8")
