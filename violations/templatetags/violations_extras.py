import re

import markdown as md_lib
from bs4 import BeautifulSoup
from django import template
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

from violations.services import SBD_PATTERN, normalize_sbd

register = template.Library()

_ALL_HTML_TAGS_RE = re.compile(r"<[^>]+>", re.DOTALL)


def _id_button_html(sbd):
    return (
        f'<button type="button" class="recognized-id mention-chip js-open-candidate-detail" '
        f'data-sbd="{sbd}" aria-label="Open detail for {sbd}">{sbd}</button>'
    )


def _highlight_text_fragment(text):
    fragments = []
    last = 0
    for match in SBD_PATTERN.finditer(text):
        fragments.append(conditional_escape(text[last:match.start()]))
        normalized = normalize_sbd(match.group(0))
        fragments.append(_id_button_html(normalized))
        last = match.end()
    fragments.append(conditional_escape(text[last:]))
    return "".join(str(part) for part in fragments)


@register.filter(needs_autoescape=True)
def highlight_ids(value, autoescape=True):
    """Highlight recognized IDs and normalize them to uppercase for display."""
    text = str(value or "")
    escape = conditional_escape if autoescape else (lambda x: x)
    escaped_text = str(escape(text))

    def repl(match):
        normalized = normalize_sbd(match.group(0))
        return _id_button_html(normalized)

    return mark_safe(SBD_PATTERN.sub(repl, escaped_text))


@register.filter(needs_autoescape=True)
def render_violation(value, is_markdown=True, autoescape=True):
    text = str(value or "")

    if isinstance(is_markdown, str):
        markdown_mode = is_markdown.lower() not in {"", "0", "false", "no"}
    else:
        markdown_mode = bool(is_markdown)

    if not markdown_mode:
        return highlight_ids(text, autoescape=autoescape)

    # Strip raw HTML first, then render markdown to keep server-side output safe.
    safe_source = _ALL_HTML_TAGS_RE.sub("", text)
    rendered = md_lib.markdown(
        safe_source,
        extensions=["nl2br", "fenced_code", "tables", "pymdownx.tilde"],
        extension_configs={"pymdownx.tilde": {"subscript": False}},
        output_format="html",
    )

    soup = BeautifulSoup(rendered, "html.parser")
    for text_node in list(soup.find_all(string=True)):
        parent = getattr(text_node, "parent", None)
        if parent and getattr(parent, "name", "") in {"a", "code", "pre", "script", "style"}:
            continue

        content = str(text_node)
        if not SBD_PATTERN.search(content):
            continue

        replacement = BeautifulSoup(_highlight_text_fragment(content), "html.parser")
        text_node.replace_with(replacement)

    return mark_safe(str(soup))
