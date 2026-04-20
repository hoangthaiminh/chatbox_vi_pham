import pytest
from django.contrib.auth.models import Group, User

from violations.models import Incident

pytestmark = pytest.mark.django_db


@pytest.fixture
def room_admin_user(db):
    user = User.objects.create_user(username="send-room-admin", password="x")
    grp, _ = Group.objects.get_or_create(name="room_admin")
    user.groups.add(grp)
    return user


@pytest.fixture
def viewer_user(db):
    return User.objects.create_user(username="send-viewer", password="x")


class TestCreateIncidentAjax:
    url = "/incidents/create/"

    def test_viewer_forbidden_json(self, client, viewer_user):
        client.force_login(viewer_user)
        res = client.post(
            self.url,
            {"sbd": "TS0032", "violation_text": "TS0032 cheating"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert res.status_code == 403
        assert res.json()["ok"] is False

    def test_room_admin_success_json(self, client, room_admin_user):
        client.force_login(room_admin_user)
        res = client.post(
            self.url,
            {"sbd": "TS0032", "violation_text": "TS0032 cheating"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert "incident_html" in body
        assert "newest_id" in body
        assert Incident.objects.filter(id=body["newest_id"]).exists()

    def test_room_admin_can_create_reminder_kind(self, client, room_admin_user):
        client.force_login(room_admin_user)
        res = client.post(
            self.url,
            {
                "sbd": "TS0032",
                "incident_kind": "reminder",
                "violation_text": "Nhac nho lan 1",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True

        incident = Incident.objects.get(id=body["newest_id"])
        assert incident.incident_kind == Incident.KIND_REMINDER

    def test_invalid_sbd_returns_400_json(self, client, room_admin_user):
        client.force_login(room_admin_user)
        res = client.post(
            self.url,
            {"sbd": "###", "violation_text": "bad"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert res.status_code == 400
        assert res.json()["ok"] is False
