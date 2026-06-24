"""Deterministic PII minimization for durable memory — Slice 5, scope 2a.

Design lock: ``docs/plans/2026-06-18-deterministic-pii-2a-design.md``.

This is the **PII stage** of the durable-memory sanitization pipeline::

    structured object
      -> credential/secret redaction   (each service's existing guard — UNCHANGED)
      -> deterministic PII redaction    (THIS module)
      -> serialization -> final secret scan -> persistence

It runs AFTER each service's secret handling and never replaces it. The credential
sanitizer (``pulse_store._redact_obj`` / the ``_detect_secret`` reject guards) stays
authoritative; this module deliberately does not look at secrets.

What it removes (self-delimiting, validatable classes only):
- email addresses                       -> ``[REDACTED EMAIL]``
- telephone numbers (formatting-gated)  -> ``[REDACTED PHONE]``
- payment cards (Luhn-validated)        -> ``[REDACTED PAYMENT CARD]``
- national identifiers (validator gate) -> ``[REDACTED PERSONAL IDENTIFIER]``
- public IPv4 addresses                 -> ``[REDACTED IP]``

Invariants (locked):
- **Default redact per class.** IP is class-aware: loopback / RFC1918 / link-local /
  CGNAT / reserved are RETAINED (operational topology, not personal data); public IPv4
  is redacted.
- **A PII false positive never rejects the write.** This module does not raise on content;
  it redacts or leaves. Only the existing secret/PEM guards (elsewhere) fail closed.
- **Never retains the original value.** ``RedactionReport`` carries class/count/field-path/
  version only — never the value, and never a (brute-forceable) hash of one.
- **Idempotent.** Re-running over already-redacted text is a no-op (placeholders carry no
  PII shape).

Out of scope (contextual-PII track, charter §2b): names, addresses, employers, health,
financial-context, pseudonymization. **IPv6 is deferred** — IPv6 literals pass through
unredacted (documented residual). PII patterns are bounded and self-delimiting, so unlike
the greedy secret signatures they are safe to apply either per string leaf
(``redact_pii_obj``) or to an already-serialized string (``redact_pii``).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

PII_SANITIZER_VERSION = 1

_EMAIL_LABEL = "[REDACTED EMAIL]"
_PHONE_LABEL = "[REDACTED PHONE]"
_CARD_LABEL = "[REDACTED PAYMENT CARD]"
_ID_LABEL = "[REDACTED PERSONAL IDENTIFIER]"
_IP_LABEL = "[REDACTED IP]"

# --- email ----------------------------------------------------------------- #
# Default email allowlist is EMPTY: every syntactically-valid email redacts. This
# resolves the design's §6.2-vs-§7 tension toward §7 + threat-model #1 (a real email,
# and the documentation example `john@example.com`, both redact). Operators may add
# domains here later; redacting an example address loses nothing.
_EMAIL_ALLOWLIST: frozenset[str] = frozenset()
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)

# --- payment card ---------------------------------------------------------- #
# 13-19 digit runs grouped only by single spaces/dashes (dots excluded so dotted-quad
# IPs and version strings never look like cards). Redact only if Luhn-valid and not an
# allowlisted published test PAN.
_CARD_RE = re.compile(r"(?<![\d.\-])(?:\d[ \-]?){12,18}\d(?![\d.\-])")
# Curated published synthetic PANs we choose to RETAIN (threat-model #4: allowlisted
# fixtures survive). These are well-known test numbers, not real cards.
_CARD_ALLOWLIST: frozenset[str] = frozenset({
    "4111111111111111", "4012888888881881",
    "5555555555554444", "5105105105105100",
    "378282246310005", "371449635398431",
    "6011111111111117",
})

# --- IPv4 ------------------------------------------------------------------- #
_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")

# --- phone ------------------------------------------------------------------ #
# A candidate must carry a phone-shaped signal that dates/ports/versions/IDs lack:
# a leading "+" or "00" country prefix, a parenthesised area code, or a 3-group
# separated NANP-style form. Bare undelimited digit runs are never phones.
_PHONE_RE = re.compile(
    r"(?<![\w])("
    r"\+\d[\d ().\-]{5,}\d"                 # +CC ...        (international)
    r"|00\d[\d ().\-]{5,}\d"                # 00CC ...       (international)
    r"|\(\d{2,4}\)[\d ().\-]{4,}\d"         # (415) 555-2671 (area-code form)
    r"|\d{3}[ .\-]\d{3,4}[ .\-]\d{3,4}"     # 415-555-2671   (3-group form)
    r")(?![\w])"
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# --- national identifiers --------------------------------------------------- #
# EMPTY by default: we claim coverage ONLY for classes with a documented, country-
# specific validator (design §6.4 / acceptance #6). Add entries as
# ``(compiled_regex, validator_fn)``; a match whose validator returns True redacts to
# ``_ID_LABEL``. Shipping none means arbitrary numeric identifiers are NOT claimed as
# covered, by construction.
_NATIONAL_ID_VALIDATORS: tuple[tuple[re.Pattern[str], Callable[[str], bool]], ...] = ()


def _luhn_ok(digits: str) -> bool:
    """True iff ``digits`` (already stripped of separators) passes the Luhn checksum."""
    if not digits.isdigit() or len(digits) < 2:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _ipv4_retained(octets: list[int]) -> bool:
    """True iff this IPv4 is operational topology we keep (not public/personal)."""
    a, b = octets[0], octets[1]
    if a == 127:                       # loopback
        return True
    if a == 10:                        # RFC1918
        return True
    if a == 172 and 16 <= b <= 31:     # RFC1918
        return True
    if a == 192 and b == 168:          # RFC1918
        return True
    if a == 169 and b == 254:          # link-local
        return True
    if a == 100 and 64 <= b <= 127:    # CGNAT (RFC6598)
        return True
    if a == 0:                         # unspecified / reserved
        return True
    return False


def redact_pii(text: str) -> tuple[str, dict[str, int]]:
    """Redact deterministic PII in ONE string. Returns ``(clean, counts_by_class)``.

    Never raises on content; never returns or stores the original value. Order matters:
    email first (distinct ``@``), then card (Luhn-gated, so non-cards fall through), then
    IPv4 (dotted quads classified), then phone (formatting-gated), then national IDs.
    """
    if not text:
        return text, {}
    counts: dict[str, int] = {}

    def bump(cls: str) -> None:
        counts[cls] = counts.get(cls, 0) + 1

    def email_repl(m: re.Match[str]) -> str:
        domain = m.group(0).rsplit("@", 1)[1].lower()
        if domain in _EMAIL_ALLOWLIST:
            return m.group(0)
        bump("email")
        return _EMAIL_LABEL

    def card_repl(m: re.Match[str]) -> str:
        digits = re.sub(r"[ \-]", "", m.group(0))
        if not (13 <= len(digits) <= 19) or digits in _CARD_ALLOWLIST or not _luhn_ok(digits):
            return m.group(0)
        bump("payment_card")
        return _CARD_LABEL

    def ip_repl(m: re.Match[str]) -> str:
        try:
            octets = [int(o) for o in m.group(0).split(".")]
        except ValueError:
            return m.group(0)
        if any(o > 255 for o in octets):       # not a valid dotted quad
            return m.group(0)
        if _ipv4_retained(octets):             # operational, retained per locked IP rule
            return m.group(0)
        bump("ip")
        return _IP_LABEL

    def phone_repl(m: re.Match[str]) -> str:
        cand = m.group(0)
        digits = re.sub(r"\D", "", cand)
        if not (7 <= len(digits) <= 15):
            return cand
        if _ISO_DATE_RE.match(cand.strip()):   # belt-and-suspenders: never a date
            return cand
        bump("phone")
        return _PHONE_LABEL

    out = _EMAIL_RE.sub(email_repl, text)
    out = _CARD_RE.sub(card_repl, out)
    out = _IPV4_RE.sub(ip_repl, out)
    out = _PHONE_RE.sub(phone_repl, out)
    for rx, valid in _NATIONAL_ID_VALIDATORS:
        def id_repl(m: re.Match[str], _valid: Callable[[str], bool] = valid) -> str:
            if not _valid(m.group(0)):
                return m.group(0)
            bump("personal_identifier")
            return _ID_LABEL
        out = rx.sub(id_repl, out)
    return out, counts


@dataclass(frozen=True)
class RedactionReport:
    """Safe audit metadata for one ``redact_pii_obj`` run — counts only, never values."""

    version: int
    counts: dict[str, int]
    fields: tuple[str, ...]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def redact_pii_obj(obj: Any) -> tuple[Any, RedactionReport]:
    """Recursively redact PII in every string leaf; preserve non-string values.

    Returns ``(clean_obj, RedactionReport)``. The report records which class counts and
    which field paths were touched — never the original value.
    """
    counts: dict[str, int] = {}
    fields: list[str] = []

    def walk(node: Any, path: str) -> Any:
        if isinstance(node, str):
            clean, c = redact_pii(node)
            if c:
                for k, v in c.items():
                    counts[k] = counts.get(k, 0) + v
                fields.append(path or "<root>")
            return clean
        if isinstance(node, list):
            return [walk(x, f"{path}[{i}]") for i, x in enumerate(node)]
        if isinstance(node, dict):
            return {k: walk(v, f"{path}.{k}" if path else str(k)) for k, v in node.items()}
        return node     # int / float / bool / None preserved unchanged

    clean = walk(obj, "")
    return clean, RedactionReport(PII_SANITIZER_VERSION, dict(counts), tuple(fields))


# ---------------------------------------------------------------------------
# Credential / secret detection guard
# ---------------------------------------------------------------------------
# These patterns are intentionally public: transparency about what is guarded is
# better security posture than obscurity. Durable memory writes (decisions,
# findings, research briefs) call detect_secret before persisting; the write is
# rejected if a match is found, preventing accidental credential storage.

_SECRET_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-(ant-)?[A-Za-z0-9_\-]{16,}"),            # OpenAI / Anthropic keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                     # AWS access key id
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),                 # GitHub PAT (classic)
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),         # GitHub PAT (fine-grained)
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),        # Slack token
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}\b"),    # bearer token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),       # PEM private key
    re.compile(
        r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=]\s*\S{6,}"
    ),
)


def detect_secret(text: str) -> str | None:
    """Return the name of the first matched secret pattern, or None if clean.

    Used as a durable-memory write guard: call before persisting any user-supplied
    text to ``ProjectMemory``. If this returns a non-None value, reject the write
    and surface a descriptive error — never store a matched value.
    """
    for rx in _SECRET_RES:
        m = rx.search(text)
        if m:
            return rx.pattern
    return None
