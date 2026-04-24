import re
from pathlib import Path

from django import forms
from django.contrib.auth.forms import PasswordChangeForm as DjangoPasswordChangeForm

from .models import Incident
from .services import MAX_SBD_LENGTH, MAX_VIOLATION_TEXT_LEN, is_valid_sbd_syntax

ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
}
ALLOWED_VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".webm", ".mkv", ".avi",
}
MAX_IMAGE_SIZE = 20 * 1024 * 1024   # 20 MB
MAX_VIDEO_SIZE = 40 * 1024 * 1024   # 40 MB


class IncidentBaseForm(forms.Form):
    sbd = forms.CharField(max_length=MAX_SBD_LENGTH, label="SBD", strip=True)
    incident_kind = forms.ChoiceField(
        label="Phân loại",
        choices=Incident.INCIDENT_KIND_CHOICES,
        required=False,
        initial=Incident.KIND_VIOLATION,
    )
    violation_text = forms.CharField(
        label="Nội dung vi phạm",
        widget=forms.Textarea(attrs={"rows": 5}),
        max_length=MAX_VIOLATION_TEXT_LEN,
        strip=True,
    )
    evidence = forms.FileField(label="Bằng chứng (Ảnh hoặc Video)", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"

    def clean_sbd(self):
        value = self.cleaned_data["sbd"].strip()
        if not value:
            raise forms.ValidationError("SBD không được để trống.")
        if not is_valid_sbd_syntax(value):
            raise forms.ValidationError(
                "SBD phải từ 2 đến 9 ký tự, chỉ gồm chữ cái (a-z, A-Z) và/hoặc chữ số (0-9). "
                "Không được có dấu cách, ký tự đặc biệt hoặc chữ tiếng Việt."
            )
        return value.upper()

    def clean_violation_text(self):
        value = self.cleaned_data["violation_text"].strip()
        if len(value) > MAX_VIOLATION_TEXT_LEN:
            raise forms.ValidationError(
                f"Nội dung vi phạm tối đa {MAX_VIOLATION_TEXT_LEN} ký tự."
            )
        return value

    def clean_incident_kind(self):
        value = self.cleaned_data.get("incident_kind")
        return Incident.normalize_incident_kind(value)

    def clean_evidence(self):
        evidence = self.cleaned_data.get("evidence")
        if not evidence:
            return evidence
        extension = Path(evidence.name).suffix.lower()
        if extension in ALLOWED_IMAGE_EXTENSIONS:
            if evidence.size > MAX_IMAGE_SIZE:
                raise forms.ValidationError("File ảnh phải ≤ 20 MB.")
            return evidence

        if extension in ALLOWED_VIDEO_EXTENSIONS:
            if evidence.size > MAX_VIDEO_SIZE:
                raise forms.ValidationError(
                    f"Video quá lớn: tối đa {MAX_VIDEO_SIZE // (1024 * 1024)} MB."
                )
            return evidence

        raise forms.ValidationError(
            "Chỉ chấp nhận file ảnh/video (jpg, jpeg, png, gif, webp, mp4, mov, webm, mkv, avi)."
        )


class IncidentCreateForm(IncidentBaseForm):
    pass


class IncidentEditForm(IncidentBaseForm):
    remove_evidence = forms.BooleanField(required=False, label="Xoá bằng chứng hiện có")


class CandidateImportForm(forms.Form):
    csv_file = forms.FileField(label="Tệp CSV")


# ── Password change ──────────────────────────────────────────────────────────

# Tightly-curated short list of passwords we refuse outright. The goal is not
# to replicate a full dictionary (that would bloat the repo and be mostly
# redundant once the character-class checks fire); it's to block the handful
# of passwords admins tend to pick when rushed on exam day.
COMMON_PASSWORDS = frozenset({
    "password", "password1", "123456", "1234567", "12345678", "123456789",
    "qwerty", "qwerty123", "abc123", "admin", "admin123", "administrator",
    "letmein", "welcome", "iloveyou", "111111", "000000", "1q2w3e4r",
    "monkey", "dragon", "passw0rd", "p@ssw0rd", "changeme",
})

# Whitelist of allowed password characters: ASCII letters, digits, and the
# common shifted-keyboard specials. No whitespace, no control characters, no
# Unicode — those are either footguns (invisible chars) or hard to re-type on
# a kiosk keyboard.
_PASSWORD_ALLOWED_RE = re.compile(
    r"^[A-Za-z0-9!@#$%^&*()\-_=+\[\]{};:'\",.<>/?\\|`~]+$"
)


def validate_password_strength(value):
    """Enforce the project's password policy.

    Rules (all must pass):
      * length ≥ 6
      * only characters from the keyboard whitelist above (no spaces, no
        control characters, no Unicode letters)
      * not on the common-passwords blocklist
      * contains at least one uppercase letter, one lowercase letter, and
        one digit (special characters are optional but allowed)
    """
    if value is None:
        return
    if len(value) < 6:
        raise forms.ValidationError("Mật khẩu phải có ít nhất 6 ký tự.")
    if not _PASSWORD_ALLOWED_RE.match(value):
        raise forms.ValidationError(
            "Mật khẩu chứa ký tự không hợp lệ. Chỉ cho phép chữ cái (a-z, A-Z), "
            "chữ số (0-9) và các ký tự đặc biệt thông dụng; không được có khoảng "
            "trắng hay ký tự điều khiển."
        )
    if value.lower() in COMMON_PASSWORDS:
        raise forms.ValidationError(
            "Mật khẩu quá phổ biến. Vui lòng chọn mật khẩu khác."
        )
    if not re.search(r"[A-Z]", value):
        raise forms.ValidationError("Mật khẩu phải có ít nhất 1 chữ cái in hoa.")
    if not re.search(r"[a-z]", value):
        raise forms.ValidationError("Mật khẩu phải có ít nhất 1 chữ cái thường.")
    if not re.search(r"\d", value):
        raise forms.ValidationError("Mật khẩu phải có ít nhất 1 chữ số.")


class AdminPasswordChangeForm(DjangoPasswordChangeForm):
    """Django's built-in PasswordChangeForm with our policy attached and
    Vietnamese error messages for the two checks it performs itself."""

    error_messages = {
        **DjangoPasswordChangeForm.error_messages,
        "password_incorrect": "Mật khẩu cũ không đúng. Vui lòng nhập lại.",
        "password_mismatch": "Hai mật khẩu mới không trùng khớp.",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["old_password"].label = "Mật khẩu cũ"
        self.fields["new_password1"].label = "Mật khẩu mới"
        self.fields["new_password2"].label = "Nhập lại mật khẩu mới"
        # Strip Django's default auto-generated help text (we show our own
        # rules next to the submit button if needed).
        self.fields["new_password1"].help_text = ""

    def clean_new_password1(self):
        password = self.cleaned_data.get("new_password1")
        validate_password_strength(password)
        return password
