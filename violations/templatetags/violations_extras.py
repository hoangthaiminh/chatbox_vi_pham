"""Template filter for rendering violation text with @{SBD} mentions.

Pipeline (defence-in-depth, server-side is the source of truth):

  0. Strip raw HTML from input (XSS defence).
  1. Replace every @{SBD} with a unique opaque placeholder (alphanumeric only,
     so markdown passes it through untouched).
  2. Render markdown (GFM-ish: nl2br, fenced_code, tables, pymdownx.tilde).
  3. Parse the rendered HTML with BeautifulSoup and visit every placeholder:
        * If a placeholder ended up inside a <code>, <pre> or <a> ancestor,
          render it as a literal @{SBD} (escaped text) — mention is NEUTRALISED.
        * If a placeholder ended up inside a tag attribute value (e.g. markdown
          turned ![](@{TS0032}) into <img src="PLACEHOLDER">), it is also
          rendered as literal text in a safe location — mention is NEUTRALISED.
        * Otherwise the placeholder is SAFE and we decide what to render:
            - invalid SBD syntax              → literal @{raw}
            - valid syntax but not in DB      → disabled strike-through link
            - valid syntax and in DB          → clickable mention link

The output is marked safe. Only trust input that has gone through this
function; every code path produces either an escaped literal or a
carefully-constructed tag.
"""

import re
import uuid

import markdown as md_lib
from bs4 import BeautifulSoup
from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

from violations.services import MENTION_TOKEN_PATTERN, SBD_PATTERN, normalize_sbd

register = template.Library()

# Strip ALL raw HTML tags to prevent XSS (keep only markdown-generated tags).
_ALL_HTML_TAGS_RE = re.compile(r"<[^>]+>", re.DOTALL)

# Placeholders look like MPHM<32hex>ZEND. Pure alphanumeric and unique per
# render so markdown preserves them unchanged and there is no risk of
# collision with legitimate content.
_PLACEHOLDER_PREFIX = "MPHM"
_PLACEHOLDER_SUFFIX = "ZEND"
_PLACEHOLDER_RE = re.compile(
    rf"{_PLACEHOLDER_PREFIX}[0-9a-f]{{32}}{_PLACEHOLDER_SUFFIX}"
)

# HTML ancestors that, if they surround a mention placeholder, force the
# mention to be rendered as a literal. <a> avoids nested anchors; <code>
# and <pre> preserve the user's explicit "show this as code" intent.
_NEUTRALISING_ANCESTORS = frozenset({"a", "code", "pre"})

# <del> / ~~…~~ is a special case: the strikethrough already visually
# means "withdrawn" / "crossed out". We only want to *downgrade* an
# otherwise-active mention to a literal when it sits under <del>
# (ADD-1). A missing-mention — which itself shows a <s>SBD</s> — is
# still allowed under <del>.
_DEL_ONLY_ANCESTORS = frozenset({"del"})

# Context classification returned by _classify_context().
_CTX_SAFE    = "safe"
_CTX_NEUTRAL = "neutral"   # hard neutralise: always render literal
_CTX_DEL     = "del_only"  # only neutralise if mention would be ACTIVE


def _get_known_sbds():
    """Return a set of all uppercase SBDs in the Candidate table."""
    from violations.models import Candidate
    return set(Candidate.objects.values_list("sbd", flat=True))


def _make_placeholder():
    return f"{_PLACEHOLDER_PREFIX}{uuid.uuid4().hex}{_PLACEHOLDER_SUFFIX}"


def _literal_token_html(raw_token):
    """Escaped literal form of @{...} that is always safe to insert as text."""
    return escape(raw_token)


def _active_mention_link(sbd):
    return (
        f'<a class="mention-link js-open-candidate-detail" '
        f'data-sbd="{escape(sbd)}" role="button" tabindex="0">'
        f'@{escape(sbd)}</a>'
    )


def _missing_mention_link(sbd):
    """Valid SBD syntax but candidate is not in DB.

    Rendered as a disabled, strike-through link-looking element. Crucially
    it has no `js-open-candidate-detail` class, no `role=button`, and
    `tabindex=-1`, so the offcanvas opener cannot fire.
    """
    return (
        f'<a class="mention-link mention-link--missing" '
        f'data-sbd="{escape(sbd)}" aria-disabled="true" tabindex="-1">'
        f'@<s>{escape(sbd)}</s></a>'
    )


def _classify_context(node):
    """Walk up the DOM tree and return one of _CTX_SAFE / _CTX_NEUTRAL /
    _CTX_DEL based on ancestry. Hard-neutral ancestors (a/code/pre) take
    priority over <del> if both are present."""
    saw_del = False
    parent = node.parent
    while parent is not None and getattr(parent, "name", None) is not None:
        name = parent.name.lower()
        if name in _NEUTRALISING_ANCESTORS:
            return _CTX_NEUTRAL
        if name in _DEL_ONLY_ANCESTORS:
            saw_del = True
        parent = parent.parent
    return _CTX_DEL if saw_del else _CTX_SAFE


def _render_mention_for_sbd(sbd, known_sbds):
    """Return mention HTML once we know the placeholder sits in a SAFE DOM
    context and its SBD is syntactically valid.
    """
    if sbd in known_sbds:
        return _active_mention_link(sbd)
    return _missing_mention_link(sbd)


def _neutralise_in_attributes(html, placeholders_map):
    """Replace any placeholder that ended up inside an HTML attribute value
    with its escaped literal form, quoted appropriately. Mark such
    placeholders as consumed so the DOM walker skips them.
    """
    attr_re = re.compile(r'(\s[\w:-]+\s*=\s*)(["\'])(.*?)\2', re.DOTALL)

    def repl(match):
        normalized = normalize_sbd(match.group(0))
        return (
            f'<button type="button" class="recognized-id mention-chip js-open-candidate-detail" '
            f'data-sbd="{normalized}" aria-label="Open detail for {normalized}">{normalized}</button>'
        )

    return attr_re.sub(repl, html)


@register.filter(is_safe=True)
def render_violation(value, is_markdown=True):
    """Render violation_text, preserving mention rules.

    is_markdown=True: run the GFM-ish markdown pipeline (default).
    is_markdown=False: plain text — HTML-escape content, newlines → <br>,
    no bold/italic/table/etc processing. Mentions are still resolved in
    both modes exactly the same way.
    """
    text = str(value or "")
    known_sbds = _get_known_sbds()

    placeholders = {}

    def on_mention(match):
        raw = match.group(1)
        sbd = normalize_sbd(raw)
        key = _make_placeholder()
        if not SBD_PATTERN.match(sbd):
            placeholders[key] = {
                "kind": "literal",
                "raw_token": match.group(0),
            }
        else:
            placeholders[key] = {
                "kind": "pending",
                "sbd": sbd,
                "raw_token": match.group(0),
            }
        return key

    text_stripped = _ALL_HTML_TAGS_RE.sub("", text)
    processed = MENTION_TOKEN_PATTERN.sub(on_mention, text_stripped)

    if is_markdown:
        rendered = md_lib.markdown(
            processed,
            extensions=["nl2br", "fenced_code", "tables", "pymdownx.tilde"],
            extension_configs={
                "pymdownx.tilde": {"subscript": False},
            },
            output_format="html",
        )
    else:
        escaped = escape(processed).replace("\n", "<br>\n")
        rendered = f'<p class="plain-text">{escaped}</p>'

    if not placeholders:
        return mark_safe(rendered)

    # Step 3a — neutralise placeholders inside tag attributes (e.g. the
    # href/src/alt/title produced by `[x](@{...})` or `![x](@{...})`).
    rendered = _neutralise_in_attributes(rendered, placeholders)

    # Step 3b — parse the resulting HTML and walk remaining placeholders in
    # text nodes, deciding active / missing / literal based on ancestry.
    soup = BeautifulSoup(rendered, "html.parser")

    for text_node in list(soup.find_all(string=_PLACEHOLDER_RE)):
        content = str(text_node)
        if not _PLACEHOLDER_RE.search(content):
            continue

        ctx = _classify_context(text_node)

        fragments = []
        last = 0
        for m in _PLACEHOLDER_RE.finditer(content):
            fragments.append(escape(content[last:m.start()]))
            key = m.group(0)
            info = placeholders.pop(key, None)
            if info is None:
                # Already consumed in the attribute pass — drop silently.
                pass
            elif info["kind"] == "literal" or ctx == _CTX_NEUTRAL:
                fragments.append(_literal_token_html(info["raw_token"]))
            elif info["kind"] == "pending":
                sbd = info["sbd"]
                is_active = sbd in known_sbds
                # ADD-1: under <del>, only active mentions are downgraded to
                # literal. Missing mentions (.mention-link--missing) keep
                # rendering so the reader still sees the strikethrough SBD.
                if ctx == _CTX_DEL and is_active:
                    fragments.append(_literal_token_html(info["raw_token"]))
                else:
                    fragments.append(_render_mention_for_sbd(sbd, known_sbds))
            else:
                fragments.append(_literal_token_html(info.get("raw_token", "")))
            last = m.end()
        fragments.append(escape(content[last:]))

        replacement = BeautifulSoup("".join(fragments), "html.parser")
        text_node.replace_with(replacement)

    # Step 3c — safety net: any placeholder still unaccounted for is rendered
    # as its literal at string level. This shouldn't happen after the two
    # passes above but we keep it so a bug cannot leak raw placeholders to
    # the client.
    final_html = str(soup)
    if placeholders:
        for key, info in list(placeholders.items()):
            final_html = final_html.replace(key, _literal_token_html(info.get("raw_token", "")))

    return mark_safe(final_html)
