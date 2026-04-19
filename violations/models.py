from datetime import timedelta

from django.conf import settings
from django.core.files.images import get_image_dimensions
from django.db import models
from django.utils import timezone
from django.utils.functional import cached_property


class Candidate(models.Model):
    sbd = models.CharField(max_length=20, unique=True)
    full_name = models.CharField(max_length=150)
    school = models.CharField(max_length=150)
    supervisor_teacher = models.CharField(max_length=150)
    exam_room = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["sbd"]

    def save(self, *args, **kwargs):
        self.sbd = self.sbd.upper().strip()
        self.exam_room = self.exam_room.strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.sbd} - {self.full_name}"


class RoomAdminProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="room_admin_profile",
    )
    room_name = models.CharField(max_length=50)

    class Meta:
        verbose_name = "Room Admin Profile"
        verbose_name_plural = "Room Admin Profiles"

    def save(self, *args, **kwargs):
        self.room_name = self.room_name.strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} ({self.room_name})"


class Incident(models.Model):
    reported_sbd = models.CharField(max_length=20)
    reported_candidate = models.ForeignKey(
        Candidate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reported_incidents",
    )
    violation_text = models.TextField()
    evidence = models.FileField(upload_to="evidence/%Y/%m/%d/", blank=True, null=True)
    room_name = models.CharField(max_length=50, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incidents",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        self.reported_sbd = self.reported_sbd.upper().strip()
        self.room_name = self.room_name.strip()
        super().save(*args, **kwargs)

    def can_edit(self, user):
        if not user.is_authenticated:
            return False
        if user.is_superuser or user.groups.filter(name="super_admin").exists():
            return True
        if self.created_by_id != user.id:
            return False
        if not user.groups.filter(name="room_admin").exists():
            return False
        return timezone.now() <= self.created_at + timedelta(hours=24)

    @property
    def evidence_is_image(self):
        if not self.evidence:
            return False
        return self.evidence.name.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))

    @property
    def evidence_is_video(self):
        if not self.evidence:
            return False
        return self.evidence.name.lower().endswith((".mp4", ".mov", ".webm", ".mkv", ".avi"))

    @cached_property
    def evidence_dimensions(self):
        if not self.evidence_is_image or not self.evidence:
            return (None, None)

        try:
            width, height = get_image_dimensions(self.evidence)
        except Exception:
            return (None, None)

        if not width or not height:
            return (None, None)
        return (int(width), int(height))

    @property
    def evidence_width(self):
        return self.evidence_dimensions[0]

    @property
    def evidence_height(self):
        return self.evidence_dimensions[1]

    @property
    def evidence_aspect_ratio(self):
        width, height = self.evidence_dimensions
        if width and height:
            return f"{width} / {height}"
        if self.evidence_is_video:
            return "16 / 9"
        return "4 / 3"

    @property
    def evidence_natural_width(self):
        width, _ = self.evidence_dimensions
        if width:
            return width
        if self.evidence_is_video:
            return 520
        return 420

    def __str__(self):
        return f"{self.reported_sbd} - {self.violation_text[:40]}"


class IncidentParticipant(models.Model):
    RELATION_REPORTED = "reported"
    RELATION_MENTIONED = "mentioned"
    RELATION_CHOICES = [
        (RELATION_REPORTED, "Reported"),
        (RELATION_MENTIONED, "Mentioned"),
    ]

    incident = models.ForeignKey(
        Incident,
        on_delete=models.CASCADE,
        related_name="participants",
    )
    candidate = models.ForeignKey(
        Candidate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incident_links",
    )
    sbd_snapshot = models.CharField(max_length=20)
    relation_type = models.CharField(max_length=20, choices=RELATION_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("incident", "sbd_snapshot")
        ordering = ["incident", "sbd_snapshot"]

    def save(self, *args, **kwargs):
        self.sbd_snapshot = self.sbd_snapshot.upper().strip()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.sbd_snapshot} ({self.relation_type})"
