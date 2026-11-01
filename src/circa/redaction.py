"""Best-effort redaction of credentials in CI text.

Secret values are replaced with ``[REDACTED]`` while the surrounding message is kept, so
"NPM_TOKEN=npm_..." becomes "NPM_TOKEN=[REDACTED]". One compiled pattern is applied in a
single pass. This catches common credential shapes; it is not complete secret detection and
does not replace human review or a Gitleaks scan.

Redaction fails closed: if it cannot complete, ``RedactionError`` is raised and no text is
returned, so callers never fall back to the original.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"
# Values already masked (GitHub Actions prints "***"), redacted (also by earlier CIRCA versions,
# as "<redacted>"), or written as a variable reference are left alone.
_NOT_MASKED = r"(?!\[REDACTED\]|<redacted>|\*{3}|\$)"
# A value never includes quotes or backslashes, so JSON and shell quoting stay intact.
_VALUE = r"[^\s\"'`,;\\]{4,}"
_KEY_LABEL = r"[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
# `gh run view --log` adds "job<TAB>step<TAB>timestamp " before each line of a key.
_LINE_PREFIX = r"[ \t\r]*\n(?:[^\t\n]*\t[^\t\n]*\t)?(?:\d{4}-\d{2}-\d{2}T\S+ ?)?[ \t]*"
_KEY_BODY_LINE = rf"{_LINE_PREFIX}(?:[A-Za-z0-9+/]+={{0,2}}|(?:Proc-Type|DEK-Info|Comment): [^\n]*?)[ \t\r]*(?=\n|\Z)"
_KEY_END_LINE = rf"{_LINE_PREFIX}-----END {_KEY_LABEL}"

_SECRET = re.compile(
    "|".join(
        [
            # A PEM private key: on one line (also JSON-escaped "\n"), or BEGIN, body lines, and END.
            # Without an END line the key ends at the first line that is not key material.
            (
                rf"(?P<key>-----BEGIN {_KEY_LABEL}"
                rf"(?:(?:\\[nr]|[A-Za-z0-9+/=])+(?:-----END {_KEY_LABEL})?|(?:{_KEY_BODY_LINE})*(?:{_KEY_END_LINE})?))"
            ),
            # "<prefix><value>" shapes: the named prefix is kept and only the value is replaced.
            # Credentials in a URL: scheme://user:password@host keeps the user and host.
            rf"(?P<url>\b[a-z][a-z0-9+.-]{{0,30}}://[^\s/:@'\"\\]{{0,200}}:){_NOT_MASKED}[^\s@/'\"\\]+(?=@)",
            # Authorization headers, with or without a scheme.
            (
                rf"(?P<header>(?i:\bauthorization)[\"']?[ \t]*[:=][ \t]*[\"']?(?:(?i:bearer|basic|token|bot)[ \t]+)?+)"
                rf"{_NOT_MASKED}{_VALUE}"
            ),
            # A bare bearer token; it must contain a digit so "Bearer authentication" is kept.
            rf"(?P<bearer>(?i:\bbearer)[ \t]+){_NOT_MASKED}(?=[A-Za-z._~+/=-]*\d)[A-Za-z0-9._~+/=-]{{8,}}",
            # Command-line options such as --password VALUE.
            rf"(?P<option>--(?i:password|passwd|token|api-key|secret|client-secret)(?:=|[ \t]+)){_NOT_MASKED}{_VALUE}",
            # Assignments whose name ends in a credential word, including environment variables:
            # password=..., NPM_TOKEN=..., AWS_SECRET_ACCESS_KEY: ..., "api_key": "...".
            # Matching starts at the credential word; any name prefix such as "NPM_" is kept as it is.
            # Python type hints such as "password: str" are kept.
            (
                r"(?P<assignment>(?i:password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key)"
                rf"[\"']?[ \t]*[:=][ \t]*[\"']?){_NOT_MASKED}(?!(?:str|bytes|Optional|SecretStr)\b){_VALUE}"
            ),
            # Tokens whose format identifies them as credentials wherever they appear.
            (
                r"(?P<token>\bgh[pousr]_[A-Za-z0-9]{20,}"  # GitHub
                r"|\bgithub_pat_[A-Za-z0-9_]{20,}"  # GitHub fine-grained
                r"|\bglpat-[A-Za-z0-9_-]{20,}"  # GitLab
                r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"  # AWS access key ID
                r"|\bxox[abposr]-[A-Za-z0-9-]{10,}"  # Slack
                r"|\bnpm_[A-Za-z0-9]{36}\b"  # npm
                r"|\bpypi-[A-Za-z0-9_-]{40,}"  # PyPI
                r"|\bAIza[0-9A-Za-z_-]{35}\b"  # Google API key
                r"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"  # OpenAI- and Anthropic-style API keys
                r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"  # JSON Web Token
            ),
        ]
    )
)


class RedactionError(Exception):
    """Credential redaction could not complete; the text must not be used."""


def _replace(match: re.Match[str]) -> str:
    kind = match.lastgroup
    if kind == "key":
        # One placeholder per line keeps the log's line structure.
        return "\n".join([REDACTED] * (match.group().count("\n") + 1))
    if kind == "token":
        return REDACTED
    return match.group(kind or 0) + REDACTED


def redact_secrets(text: str) -> str:
    """Return ``text`` with recognized credential values replaced by ``[REDACTED]``.

    Raises ``RedactionError``, which never carries the text, when redaction fails.
    """

    try:
        redacted: str | None = _SECRET.sub(_replace, text)
    except Exception:  # noqa: BLE001 - fail closed on any failure, whatever its cause
        redacted = None
    if redacted is None:
        # Raised outside the handler so the original exception, which may quote the text, is not chained.
        raise RedactionError("credential redaction failed, so CIRCA stopped without using the unredacted text")
    return redacted


def contains_secret(text: str) -> bool:
    """Whether ``text`` contains a recognized credential value."""

    return redact_secrets(text) != text
