"""Tests for Task 4: POST /incidents/preview/ endpoint."""

import pytest
from django.contrib.auth.models import Group, User

pytestmark = pytest.mark.django_db


@pytest.fixture
def room_admin_user(db):
    user = User.objects.create_user(username="pr-admin", password="x")
    grp, _ = Group.objects.get_or_create(name="room_admin")
    user.groups.add(grp)
    return user


@pytest.fixture
def viewer_user(db):
    return User.objects.create_user(username="pr-viewer", password="x")


@pytest.fixture
def candidate(db):
    from violations.models import Candidate
    return Candidate.objects.create(
        sbd="TS0032", full_name="Test Candidate",
        school="S1", supervisor_teacher="T1", exam_room="R1",
    )


class TestPreviewEndpoint:
    url = "/incidents/preview/"

    def test_requires_login(self, client):
        res = client.post(self.url, {"sbd": "TS0032", "violation_text": "hi"})
        assert res.status_code in (302, 403)

    def test_viewer_forbidden(self, client, viewer_user):
        client.force_login(viewer_user)
        res = client.post(self.url, {"sbd": "TS0032", "violation_text": "hi"})
        assert res.status_code == 403

    def test_room_admin_success(self, client, room_admin_user, candidate):
        client.force_login(room_admin_user)
        res = client.post(self.url, {
            "sbd": "TS0032",
            "violation_text": "Caught TS0032 cheating",
        })
        assert res.status_code == 200
        html = res.json()["html"]
        # Chat bubble class marker
        assert "chat-bubble" in html
        # Active mention link was rendered
        assert 'class="mention-link js-open-candidate-detail"' in html
        assert 'data-sbd="TS0032"' in html
        # Candidate name is shown in the preview header
        assert "Test Candidate" in html

    def test_empty_text_ok(self, client, room_admin_user):
        client.force_login(room_admin_user)
        res = client.post(self.url, {"sbd": "TS0032", "violation_text": ""})
        assert res.status_code == 200
        assert "html" in res.json()

    def test_missing_candidate_shows_literal_or_profile_not_found(
        self, client, room_admin_user
    ):
        client.force_login(room_admin_user)
        res = client.post(self.url, {
            "sbd": "XX999",
            "violation_text": "bare text",
        })
        assert res.status_code == 200
        html = res.json()["html"]
        # Preview of unknown SBD must mark it as missing profile.
        assert "Candidate profile not found" in html

    def test_long_text_is_clamped(self, client, room_admin_user):
        client.force_login(room_admin_user)
        big = "a" * 20_000
        res = client.post(self.url, {"sbd": "TS0032", "violation_text": big})
        # Should not error out; the service clamps to MAX_VIOLATION_TEXT_LEN.
        assert res.status_code == 200

    def test_preview_mode_has_no_edit_button(self, client, room_admin_user, candidate):
        client.force_login(room_admin_user)
        res = client.post(self.url, {
            "sbd": "TS0032", "violation_text": "x",
        })
        html = res.json()["html"]
        # No /incidents/<id>/edit/ URL can appear because this is unsaved.
        assert "/edit/" not in html

    def test_rate_limit_returns_429(self, client, room_admin_user, monkeypatch):
        """Tight loop on preview must trip the per-user rate limit."""
        from violations import image_uploads
        monkeypatch.setattr(image_uploads, "PREVIEW_RATE_LIMIT_MAX_PER_WINDOW", 3)
        image_uploads._preview_state.clear()

        client.force_login(room_admin_user)
        for _ in range(3):
            res = client.post(self.url, {"sbd": "TS0032", "violation_text": "x"})
            assert res.status_code == 200
        # 4th call within the window → 429
        res = client.post(self.url, {"sbd": "TS0032", "violation_text": "x"})
        assert res.status_code == 429
        assert "error" in res.json()
