import re
from urllib.parse import urlparse

import markdown as md_lib
from bs4 import BeautifulSoup
from django import template
from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

from violations.services import SBD_PATTERN, normalize_sbd

register = template.Library()


# ── HTML sanitizer (whitelist) ────────────────────────────────────────────────
#
# We render markdown first, then walk the resulting HTML with BeautifulSoup
# and:
#   • drop any tag not on ``ALLOWED_TAGS`` (replace with its plain text);
#   • drop any attribute not on ``ALLOWED_ATTRS`` for that tag;
#   • drop any href/src whose URL scheme is not on ``SAFE_URL_SCHEMES``.
#
# This replaces the old approach of stripping ``<...>`` from the raw input
# before markdown ran. That approach had two problems:
#   1. It corrupted code blocks (e.g. ``cout << k;`` got eaten because a ``>``
#      from a later blockquote made the regex span across the code fence).
#   2. It did NOT cover ``[click](javascript:alert(1))`` — markdown happily
#      produced ``<a href="javascript:alert(1)">`` from that.

ALLOWED_TAGS = frozenset({
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "del", "code", "pre", "blockquote",
    "ul", "ol", "li",
    "a", "img",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "sup", "sub", "span", "div",
})

# Tags whose CONTENT is itself dangerous (executable script, parsed CSS,
# nested browsing context, form actions). For these we drop the whole subtree
# rather than just unwrapping the tag — otherwise ``<script>alert(1)</script>``
# would leak its literal text into the output, which is escaped (so not
# executable) but still surfaces attacker-controlled strings on the page.
DANGEROUS_TAGS = frozenset({
    "script", "style", "iframe", "frame", "frameset", "object", "embed",
    "applet", "noscript", "noframes", "form", "input", "button", "textarea",
    "select", "option", "meta", "link", "base", "svg", "math",
})

# Per-tag attribute whitelist. Anything not listed is removed. ``class`` is
# allowed but later filtered to only safe prefixes the app itself emits.
ALLOWED_ATTRS = {
    "a": frozenset({"href", "title", "class"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "table": frozenset({"class"}),
    "th": frozenset({"colspan", "rowspan", "scope", "align"}),
    "td": frozenset({"colspan", "rowspan", "align"}),
    "code": frozenset({"class"}),  # markdown emits class="language-xxx"
    "pre": frozenset({"class"}),
    "span": frozenset({"class", "data-sbd"}),
    "div": frozenset({"class"}),
    "button": frozenset({"class", "type", "data-sbd", "aria-label"}),
}

# URL schemes we accept on <a href> and <img src>. Relative URLs (no scheme)
# are also permitted. ``data:`` is permitted only for <img> with image/* and
# is policed in ``_is_safe_url``.
SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto", "tel"})

# Class names we allow through. Markdown emits a small set; anything else is
# almost certainly user-supplied via a raw <tag class="..."> attempt.
_SAFE_CLASS_RE = re.compile(
    r"^(language-[A-Za-z0-9_+-]+|highlight|codehilite|recognized-id|"
    r"mention-chip|js-open-candidate-detail|footnote|footnote-backref|"
    r"footnote-ref|markdown-body)$"
)


def _is_safe_url(value, allow_data_image=False):
    """Return True iff ``value`` is a safe URL for an href/src attribute.

    Relative URLs (no scheme) pass. Absolute URLs must use one of the
    allow-listed schemes. ``data:`` is only allowed on ``<img>`` and only
    for ``data:image/...`` payloads.
    """
    if not value:
        return False
    raw = value.strip()
    if not raw:
        return False
    # Reject anything containing control characters that browsers are known
    # to ignore when matching schemes (newline/tab in URLs is a known XSS
    # vector for sloppy filters).
    if any(ch in raw for ch in "\x00\x09\x0a\x0d"):
        return False

    # Relative URL? No colon before the first slash, hash, or question mark.
    # Use ``urlparse`` to be robust.
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False

    scheme = (parsed.scheme or "").lower()
    if not scheme:
        return True  # relative URL
    if scheme in SAFE_URL_SCHEMES:
        return True
    if allow_data_image and scheme == "data":
        # parsed.path is e.g. "image/png;base64,...."
        path = (parsed.path or "").lower()
        return path.startswith("image/")
    return False


def _filter_class_attr(class_value):
    """Keep only class tokens that match our safe whitelist."""
    if not class_value:
        return ""
    if isinstance(class_value, (list, tuple)):
        tokens = list(class_value)
    else:
        tokens = str(class_value).split()
    safe = [tok for tok in tokens if _SAFE_CLASS_RE.match(tok)]
    return " ".join(safe)


def _sanitize_soup(soup):
    """In-place sanitize a parsed BeautifulSoup tree."""
    # Iterate over a static list because we mutate the tree.
    for tag in list(soup.find_all(True)):
        # ``decompose()`` on an outer tag detaches descendants we already
        # captured; their ``.name`` becomes ``None``. Skip those.
        if tag.name is None:
            continue
        name = tag.name.lower()

        if name in DANGEROUS_TAGS:
            # Drop the entire subtree, including any text/children inside.
            tag.decompose()
            continue

        if name not in ALLOWED_TAGS:
            # Drop the tag but keep its visible text.
            tag.unwrap()
            continue

        allowed = ALLOWED_ATTRS.get(name, frozenset())
        # Snapshot keys; we mutate ``tag.attrs`` below.
        for attr in list(tag.attrs.keys()):
            attr_lower = attr.lower()
            # Always strip event handlers and "style" (style can carry
            # expression()/url(javascript:) tricks on legacy browsers).
            if attr_lower.startswith("on") or attr_lower == "style":
                del tag.attrs[attr]
                continue
            if attr_lower not in allowed:
                del tag.attrs[attr]
                continue

            value = tag.attrs[attr]

            if attr_lower == "href":
                if not _is_safe_url(value):
                    del tag.attrs[attr]
                    continue
            elif attr_lower == "src":
                if not _is_safe_url(value, allow_data_image=(name == "img")):
                    del tag.attrs[attr]
                    continue
            elif attr_lower == "class":
                cleaned = _filter_class_attr(value)
                if cleaned:
                    tag.attrs[attr] = cleaned
                else:
                    del tag.attrs[attr]

        # External links should not be able to navigate the opener back.
        if name == "a" and tag.attrs.get("href"):
            tag.attrs["rel"] = "noopener noreferrer nofollow"
            tag.attrs["target"] = "_blank"
    return soup


# ── SBD highlighting ──────────────────────────────────────────────────────────


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
    """Render ``value`` either as plain text + SBD chips, or as sanitized
    markdown. Sanitization happens AFTER markdown rendering so code blocks
    keep their literal ``<`` and ``>`` characters, and any raw HTML the user
    typed (or that markdown produced from ``[a](javascript:...)``) gets
    filtered through a strict whitelist.
    """
    text = str(value or "")

    if isinstance(is_markdown, str):
        markdown_mode = is_markdown.lower() not in {"", "0", "false", "no"}
    else:
        markdown_mode = bool(is_markdown)

    if not markdown_mode:
        return highlight_ids(text, autoescape=autoescape)

    rendered = md_lib.markdown(
        text,
        extensions=["nl2br", "fenced_code", "tables", "pymdownx.tilde"],
        extension_configs={"pymdownx.tilde": {"subscript": False}},
        output_format="html",
    )

    soup = BeautifulSoup(rendered, "html.parser")
    _sanitize_soup(soup)

    # SBD chips: walk text nodes outside link/code/pre containers and inject
    # clickable mention buttons. Done after sanitizing so injected buttons
    # are not themselves stripped.
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
