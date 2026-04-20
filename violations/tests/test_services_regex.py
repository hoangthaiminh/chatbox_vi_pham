"""Tests for SBD regex constants and helpers in violations.services.

These are pure-Python tests — no DB access required. They lock in the
behaviour defined for ADD-4:
  * SBD shape = 0..2 letters + >=2 digits, total length 1..9, OR pure digits.
  * Mention token @{...} uses the same character class and 1..9 length.
  * is_valid_sbd_syntax accepts any Latin-alphanum 1..9 chars, which is
    intentionally broader than SBD_PATTERN (form field validation first,
    stricter pattern-matching when extracting participants).
"""

import pytest

from violations.services import (
    MAX_SBD_LENGTH,
    MENTION_TOKEN_PATTERN,
    SBD_PATTERN,
    SBD_TEXT_PATTERN,
    extract_sbd_codes,
    is_valid_sbd_syntax,
    normalize_sbd,
)


class TestSbdPattern:
    """SBD_PATTERN is the authoritative 'is this a real SBD' check."""

    @pytest.mark.parametrize("sbd", [
        "TS0032", "TS00321", "AB123", "ABCD12345", "X123",
        "CT983", "7728", "12", "99999999",  # pure digits
        "A99", "ab1234",                     # minimum variants
    ])
    def test_accepts(self, sbd):
        assert SBD_PATTERN.match(sbd), f"{sbd!r} should be a valid SBD"

    @pytest.mark.parametrize("sbd", [
        "TS003",         # only 1 digit after 2 letters — actually wait, 3 digits
        "1",             # only 1 digit — below min_digits=2
        "ABCD",          # letters only
        "TS0000032",     # 9 digits but with 2 letters → total 9 is OK? Actually TS0000032 = 2 letters + 7 digits = 9 chars, valid.
        "ABCDEF123",     # 6 letters — too many
        "ABCD1",         # 4 letters + 1 digit — only 1 digit (needs ≥ 2)
    ])
    def test_rejects(self, sbd):
        assert not SBD_PATTERN.match(sbd), f"{sbd!r} should NOT match SBD_PATTERN"

    def test_length_boundary_9(self):
        # exactly 9 chars = max
        assert SBD_PATTERN.match("AB1234567")
        assert SBD_PATTERN.match("123456789")  # 9 digits
        # 10 chars = too long
        assert not SBD_PATTERN.match("AB12345678")
        assert not SBD_PATTERN.match("1234567890")

    def test_max_sbd_length_constant(self):
        assert MAX_SBD_LENGTH == 9


class TestIsValidSbdSyntax:
    """Permissive form-input syntax check: any 1..9 alnum chars."""

    @pytest.mark.parametrize("val", ["TS0032", "abc", "1", "XYZ999999"])
    def test_accepts_any_alnum(self, val):
        assert is_valid_sbd_syntax(val)

    @pytest.mark.parametrize("val", [
        "",                # empty
        "TS 0032",         # space
        "TS-032",          # dash
        "ABCDEFGHIJ",      # 10 chars — over limit
        "TS0032!",         # special char
        "Số32",            # non-latin
    ])
    def test_rejects(self, val):
        assert not is_valid_sbd_syntax(val)


class TestMentionTokenPattern:
    """@{SBD} extraction pattern."""

    def test_captures_groups(self):
        assert MENTION_TOKEN_PATTERN.findall("hi @{TS0032} and @{CT983}") == ["TS0032", "CT983"]

    def test_length_cap_9(self):
        # token regex matches 1..9 alphanumeric chars between braces
        assert MENTION_TOKEN_PATTERN.findall("@{AB1234567}") == ["AB1234567"]  # 9 chars
        assert MENTION_TOKEN_PATTERN.findall("@{ABC1234567}") == []            # 10 chars → reject

    def test_special_chars_rejected(self):
        assert MENTION_TOKEN_PATTERN.findall("@{TS 0032}") == []
        assert MENTION_TOKEN_PATTERN.findall("@{TS-0032}") == []


class TestNormalizeSbd:
    def test_uppercases_and_strips(self):
        assert normalize_sbd("  ts0032  ") == "TS0032"
        assert normalize_sbd(None) == ""
        assert normalize_sbd("") == ""


class TestExtractSbdCodes:
    """Only @{...} tokens whose content matches SBD_PATTERN are kept."""

    def test_ignores_bare_sbds(self):
        # bare TS0032 is not a token
        assert extract_sbd_codes("saw TS0032 cheating") == []

    def test_extracts_valid_tokens(self):
        out = extract_sbd_codes("caught @{TS0032} with @{CT983}")
        assert out == ["TS0032", "CT983"]

    def test_dedupes_preserving_order(self):
        out = extract_sbd_codes("@{TS0032} then @{TS0032} again")
        assert out == ["TS0032"]

    def test_drops_invalid_shape(self):
        # @{ABCDEF} matches the *token* regex (6 alnum) but fails SBD_PATTERN
        # (needs ≥ 2 digits), so extract_sbd_codes must drop it.
        assert extract_sbd_codes("hello @{ABCDEF}") == []

    def test_drops_overlong(self):
        # 10 chars inside braces does not match the token regex at all.
        assert extract_sbd_codes("hello @{AB12345678}") == []


class TestSbdTextPattern:
    """SBD_TEXT_PATTERN scans free text for bare SBD-like tokens."""

    def test_finds_bare_sbds(self):
        assert SBD_TEXT_PATTERN.findall("caught TS0032 and AB123") == ["TS0032", "AB123"]

    def test_does_not_overmatch(self):
        assert SBD_TEXT_PATTERN.findall("xyz") == []
