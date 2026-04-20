from django import forms
from django.contrib import admin
from django.contrib.admin.sites import NotRegistered
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm

from .models import Candidate, Incident, IncidentParticipant, RoomAdminProfile
from .services import (
    ROLE_CHOICES,
    ROLE_LABELS,
    ROLE_ROOM_ADMIN,
    ROLE_VIEWER,
    apply_user_role,
    detect_user_role,
    ensure_valid_role_room,
)

User = get_user_model()


class RoleAwareUserCreationForm(AdminUserCreationForm):
    role = forms.ChoiceField(choices=ROLE_CHOICES, initial=ROLE_VIEWER)
    room_name = forms.CharField(
        max_length=50,
        required=False,
        help_text="Bắt buộc khi vai trò là Quản trị phòng.",
    )

    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("username", "email", "first_name", "last_name")

    def clean(self):
        cleaned_data = super().clean()
        role = cleaned_data.get("role")
        try:
            cleaned_data["room_name"] = ensure_valid_role_room(role, cleaned_data.get("room_name"))
        except ValueError as exc:
            self.add_error("room_name", str(exc))
        return cleaned_data


class RoleAwareUserChangeForm(UserChangeForm):
    role = forms.ChoiceField(choices=ROLE_CHOICES, initial=ROLE_VIEWER)
    room_name = forms.CharField(
        max_length=50,
        required=False,
        help_text="Bắt buộc khi vai trò là Quản trị phòng.",
    )

    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = self.instance
        if user and user.pk:
            self.fields["role"].initial = detect_user_role(user)
            profile = getattr(user, "room_admin_profile", None)
            if profile:
                self.fields["room_name"].initial = profile.room_name

    def clean(self):
        cleaned_data = super().clean()
        role = cleaned_data.get("role")
        try:
            cleaned_data["room_name"] = ensure_valid_role_room(role, cleaned_data.get("room_name"))
        except ValueError as exc:
            self.add_error("room_name", str(exc))
        return cleaned_data


try:
    admin.site.unregister(User)
except NotRegistered:
    pass


@admin.register(User)
class RoleAwareUserAdmin(UserAdmin):
    form = RoleAwareUserChangeForm
    add_form = RoleAwareUserCreationForm

    fieldsets = UserAdmin.fieldsets + (
        ("Vai trò ứng dụng", {"fields": ("role", "room_name")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "email", "first_name", "last_name", "password1", "password2"),
            },
        ),
        ("Vai trò ứng dụng", {"fields": ("role", "room_name")}),
    )
    list_display = UserAdmin.list_display + ("app_role", "app_room")

    @admin.display(description="Vai trò")
    def app_role(self, obj):
        return ROLE_LABELS.get(detect_user_role(obj), ROLE_VIEWER)

    @admin.display(description="Phòng")
    def app_room(self, obj):
        profile = getattr(obj, "room_admin_profile", None)
        return profile.room_name if profile else "-"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        apply_user_role(
            obj,
            form.cleaned_data.get("role", ROLE_VIEWER),
            form.cleaned_data.get("room_name", ""),
        )


class IncidentParticipantInline(admin.TabularInline):
    model = IncidentParticipant
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ("sbd", "full_name", "school", "supervisor_teacher", "exam_room")
    search_fields = ("sbd", "full_name", "school", "supervisor_teacher", "exam_room")


@admin.register(RoomAdminProfile)
class RoomAdminProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "room_name")
    search_fields = ("user__username", "room_name")


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = (
        "reported_sbd",
        "incident_kind",
        "room_name",
        "created_by",
        "created_at",
        "updated_at",
    )
    list_filter = ("incident_kind", "room_name", "created_at")
    search_fields = ("reported_sbd", "violation_text", "created_by__username")
    inlines = [IncidentParticipantInline]


@admin.register(IncidentParticipant)
class IncidentParticipantAdmin(admin.ModelAdmin):
    list_display = ("incident", "sbd_snapshot", "relation_type", "candidate", "created_at")
    search_fields = ("sbd_snapshot", "candidate__sbd")
    list_filter = ("relation_type", "created_at")
