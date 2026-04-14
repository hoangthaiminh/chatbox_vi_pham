from django import template
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

from violations.services import SBD_PATTERN, normalize_sbd

register = template.Library()


@register.filter(needs_autoescape=True)
def highlight_ids(value, autoescape=True):
    """Highlight recognized IDs and normalize them to uppercase for display."""
    text = str(value or "")
    escape = conditional_escape if autoescape else (lambda x: x)
    escaped_text = str(escape(text))

    def repl(match):
        normalized = normalize_sbd(match.group(0))
        return f'<span class="recognized-id">{normalized}</span>'

    return mark_safe(SBD_PATTERN.sub(repl, escaped_text))
