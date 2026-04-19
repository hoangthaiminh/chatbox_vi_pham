from pathlib import Path

from django import forms

from .services import MAX_SBD_LENGTH, MAX_VIOLATION_TEXT_LEN, is_valid_sbd_syntax

# Images are now embedded via Markdown; evidence field is video-only
ALLOWED_VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".webm", ".mkv", ".avi",
}
MAX_VIDEO_SIZE = 200 * 1024 * 1024  # 200 MB


class IncidentBaseForm(forms.Form):
    sbd = forms.CharField(max_length=MAX_SBD_LENGTH, label="SBD", strip=True)
    violation_text = forms.CharField(
        label="Violation Content",
        widget=forms.Textarea(attrs={"rows": 5}),
        max_length=MAX_VIOLATION_TEXT_LEN,
        strip=True,
    )
    evidence = forms.FileField(label="Video Evidence", required=False)

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
            raise forms.ValidationError("SBD cannot be empty.")
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
                f"Violation text must be at most {MAX_VIOLATION_TEXT_LEN} characters."
            )
        return value

    def clean_evidence(self):
        evidence = self.cleaned_data.get("evidence")
        if not evidence:
            return evidence
        extension = Path(evidence.name).suffix.lower()
        if extension not in ALLOWED_VIDEO_EXTENSIONS:
            raise forms.ValidationError(
                "Chỉ chấp nhận file video (mp4, mov, webm, mkv, avi)."
            )
        if evidence.size > MAX_VIDEO_SIZE:
            raise forms.ValidationError("Video file phải ≤ 200 MB.")
        return evidence


class IncidentCreateForm(IncidentBaseForm):
    pass


class IncidentEditForm(IncidentBaseForm):
    remove_evidence = forms.BooleanField(required=False, label="Remove existing video")


class CandidateImportForm(forms.Form):
    csv_file = forms.FileField(label="CSV file")
