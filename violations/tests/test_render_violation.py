"""Tests for the render_violation template filter.

These are the same 22 cases that the standalone harness covers, ported to
pytest-django so they run against a real Django template engine and a real
Candidate queryset (not a mock). This is the authoritative test for Task 3+6
and ADD-1.
"""

import pytest

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _candidates(db):
    """Two candidates, so tests have a stable 'valid & present' fixture
    (TS0032, AB1234) and can rely on everything else being 'missing'."""
    from violations.models import Candidate
    Candidate.objects.create(sbd="TS0032", full_name="Example", school="S",
                             supervisor_teacher="T", exam_room="R1")
    Candidate.objects.create(sbd="AB1234", full_name="Ex2", school="S",
                             supervisor_teacher="T", exam_room="R2")


def render(value):
    from violations.templatetags.violations_extras import render_violation
    return str(render_violation(value))


# ─── Happy-path mentions ───────────────────────────────────────────────────
class TestActiveMention:
    def test_plain_existing_sbd(self):
        out = render("Xin chào @{TS0032}")
        assert 'class="mention-link js-open-candidate-detail"' in out
        assert 'data-sbd="TS0032"' in out
        assert 'mention-link--missing' not in out

    def test_all_digits_sbd(self):
        from violations.models import Candidate
        Candidate.objects.create(sbd="7728", full_name="Num", school="S",
                                 supervisor_teacher="T", exam_room="")
        out = render("Test @{7728}")
        assert 'class="mention-link js-open-candidate-detail"' in out
        assert 'data-sbd="7728"' in out

    def test_lowercase_normalises_to_uppercase(self):
        out = render("hi @{ts0032}")
        # Should be rendered as TS0032 active link (DB stores uppercase).
        assert 'data-sbd="TS0032"' in out
        assert 'class="mention-link js-open-candidate-detail"' in out


class TestMissingMention:
    def test_valid_shape_absent_from_db(self):
        out = render("Bắt gặp @{TS0030} quay cóp")
        assert 'mention-link mention-link--missing' in out
        assert 'aria-disabled="true"' in out
        assert 'tabindex="-1"' in out
        assert '<s>TS0030</s>' in out
        assert 'js-open-candidate-detail' not in out


class TestInvalidMention:
    def test_shape_invalid_renders_literal(self):
        # 6 letters, no digit → fails SBD_PATTERN
        out = render("Hello @{ABCDEF} there")
        assert '@{ABCDEF}' in out
        assert 'mention-link' not in out

    def test_overlong_token_unchanged(self):
        # Token regex caps at 9 chars, so the raw text stays.
        out = render("lots @{ABCDE12345} here")
        assert '@{ABCDE12345}' in out
        assert 'mention-link' not in out


# ─── Context neutralisation (Task 3+6) ─────────────────────────────────────
class TestContextNeutralisation:
    def test_inside_inline_code(self):
        out = render("Syntax `@{TS0032}`")
        assert '<code>' in out
        assert '@{TS0032}' in out
        assert 'mention-link' not in out

    def test_inside_fenced_code(self):
        out = render("```\n@{TS0032}\n```")
        assert '<pre>' in out
        assert '@{TS0032}' in out
        assert 'mention-link' not in out

    def test_inside_link_label(self):
        out = render("See [@{TS0032}](https://example.com)")
        assert '<a href="https://example.com">' in out
        assert '@{TS0032}' in out
        assert 'mention-link' not in out

    def test_as_image_url_attribute(self):
        out = render("![evidence](@{TS0032})")
        assert 'mention-link' not in out
        # No raw placeholder leaked
        assert 'MPHM' not in out

    def test_as_link_href_attribute(self):
        out = render("[click](@{TS0032})")
        assert 'mention-link' not in out
        assert 'MPHM' not in out


# ─── Contexts where mentions DO render ─────────────────────────────────────
class TestSafeContexts:
    def test_inside_bold_italic(self):
        out = render("***@{TS0032}*** breaking news")
        assert 'mention-link js-open-candidate-detail' in out

    def test_inside_blockquote(self):
        out = render("> @{TS0032} at the front")
        assert '<blockquote>' in out
        assert 'mention-link js-open-candidate-detail' in out

    def test_inside_table_cell(self):
        out = render("| a | b |\n|---|---|\n| @{TS0032} | ok |")
        assert '<table>' in out
        assert 'mention-link js-open-candidate-detail' in out

    def test_inside_list(self):
        out = render("- @{TS0032}\n- @{XX999}")
        assert 'mention-link js-open-candidate-detail' in out
        assert 'mention-link--missing' in out
        assert '<ul>' in out


# ─── ADD-1: conditional <del> neutralisation ───────────────────────────────
class TestDelConditional:
    def test_active_mention_inside_strike_neutralised(self):
        """ADD-1: ~~@{active}~~ — active link becomes literal so we don't
        show a live clickable link through a strikethrough (visual clash)."""
        out = render("~~@{TS0032}~~ has left")
        assert '<del>' in out
        assert '@{TS0032}' in out
        # Must NOT be the active mention-link variant:
        assert 'js-open-candidate-detail' not in out

    def test_missing_mention_inside_strike_rendered(self):
        """ADD-1: ~~@{missing}~~ — missing (already struck) mention stays."""
        out = render("~~@{TS0030}~~")
        assert 'mention-link mention-link--missing' in out
        assert '<del>' in out

    def test_invalid_shape_inside_strike_literal(self):
        out = render("~~@{ABCDEF}~~")
        assert '@{ABCDEF}' in out
        assert 'mention-link' not in out


# ─── GFM extras ────────────────────────────────────────────────────────────
class TestGFMStrikethrough:
    def test_strikethrough_standalone(self):
        out = render("xoá ~~cái này~~ đi")
        assert '<del>cái này</del>' in out


# ─── Security ──────────────────────────────────────────────────────────────
class TestSecurity:
    def test_raw_script_stripped(self):
        out = render("<script>alert('x')</script> @{TS0032}")
        assert '<script' not in out
        assert 'mention-link js-open-candidate-detail' in out

    def test_empty_input(self):
        out = render("")
        assert 'mention-link' not in out
        # should be falsy / near-empty
        assert len(out.strip()) < 20


# ─── Mixed scenarios ───────────────────────────────────────────────────────
class TestMixed:
    def test_multiple_mentions_code_and_free(self):
        out = render("Real @{TS0032} vs code `@{TS0032}`")
        # First one is active
        assert 'mention-link js-open-candidate-detail' in out
        # Second one survives as literal inside <code>
        assert '<code>@{TS0032}</code>' in out

    def test_mixed_missing_and_active_in_list(self):
        out = render("- @{TS0032}\n- @{ZZ999}")
        assert 'js-open-candidate-detail' in out
        assert 'mention-link--missing' in out
