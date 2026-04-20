from pathlib import Path

from django import forms

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
