"""Write-time redaction: mask secrets and PII before they enter the stores. Regex catches keys, tokens
and emails, never names, codenames or secrets in prose: redacted output is not "safe to share".

Mirrors the Go client (docs/redaction.md). Modes are ordered, additive levels:
secrets(0) < privacy(1) < strict(2)."""
import re

# Ordered, additive redaction levels. "minimum required mode" == the higher of two levels (max_mode).
LEVELS = {"secrets": 0, "privacy": 1, "strict": 2}

# re.A on every pattern mirrors Go's ASCII-only RE2: without it a token glued to a non-ASCII letter
# has no Python \b and leaks past the server while the client redacts it. Two residual gaps, both only
# in the three `(?i)` privacy patterns: Python's \s also matches \v (server over-matches, safe), and
# Go folds case by Unicode (ſ -> s) where re.A folds ASCII only. Never make a secrets pattern `(?i)`.
_A = re.A


def _nocase(*words):
    """An alternation of `words` in any ASCII case, `_` meaning an optional `_` or `-`. Spelled out
    because `(?i)` folds Unicode in Go, so a `(?i)` secrets pattern would fail open here."""
    return "|".join("".join("[_-]?" if c == "_" else f"[{c}{c.upper()}]" for c in w) for w in words)


# A credential value that names where the secret lives, or one a pattern above already redacted.
_REFERENCE = re.compile(r"\$\{|\$[A-Z_][A-Z0-9_]*\b|\[REDACTED:|os\.environ|os\.getenv|process\.env", _A)

# Where a token may start: a word boundary, or just after what glues a word character to it in raw JSONL
# or tool output, where `\nghp_...` or an ANSI `...[0mghp_...` has no boundary and the token would ship: a
# JSON escape, or a terminal colour sequence, raw or as `\u001b`. A leading \b keeps a prefix from matching
# inside a word (`task-...` is not an `sk-` key); RE2 has no lookbehind, so the prefix is consumed and kept by
# the `v` group rule in _sub.
_START = r"(?:\\[bfnrt]|\\u[0-9a-fA-F]{4}(?:\[[0-9;]*[A-Za-z])?|\x1b\[[0-9;]*[A-Za-z]|\b)"

# (label, pattern): SECRETS (level >= 0), high-precision, most specific first. A minimum length `{n,}`
# with no trailing \b fails safe if a vendor lengthens its tokens.
PATTERNS = [
    ("openai_key",   re.compile(_START + r"(?P<v>sk-[A-Za-z0-9_-]{20,})", _A)),
    ("aws_key",      re.compile(r"AKIA[0-9A-Z]{16}", _A)),
    # {4,}: a truncated fragment still identifies a real token
    ("github_token", re.compile(_START + r"(?P<v>(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{4,})", _A)),
    ("slack_token",  re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}", _A)),
    ("private_key",  re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----", re.S | _A)),
    ("jwt",          re.compile(_START + r"(?P<v>eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})", _A)),
    # leading \b only: it stops matches inside base64 blobs, and a trailing one would miss longer real keys
    ("gcp_key",      re.compile(_START + r"(?P<v>AIza[0-9A-Za-z_-]{35})", _A)),
    # Vendor-prefixed, case-explicit shapes; classes that exclude `_` let snake_case identifiers
    # (npm_config_registry) break the run. Stripe's publishable pk_ is not a credential.
    ("stripe_key",   re.compile(_START + r"(?P<v>(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,})", _A)),
    ("docker_pat",   re.compile(_START + r"(?P<v>dckr_pat_[A-Za-z0-9_-]{20,})", _A)),
    ("npm_token",    re.compile(_START + r"(?P<v>npm_[A-Za-z0-9]{36,})", _A)),
    ("sendgrid_key", re.compile(_START + r"(?P<v>SG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})", _A)),
    ("hf_token",     re.compile(_START + r"(?P<v>hf_[A-Za-z0-9]{30,})", _A)),
    ("gitlab_pat",   re.compile(_START + r"(?P<v>glpat-[A-Za-z0-9_-]{20,})", _A)),
    ("pypi_token",   re.compile(_START + r"(?P<v>pypi-AgEI[A-Za-z0-9_-]{16,})", _A)),   # AgEI = fixed macaroon b64 header
    ("telegram_bot", re.compile(_START + r"(?P<v>[0-9]{8,10}:AA[A-Za-z0-9_-]{32,})", _A)),
    ("slack_webhook", re.compile(r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]{20,}", _A)),
    ("azure_key",    re.compile(r"AccountKey=[A-Za-z0-9+/]{86}==", _A)),   # 86+`==` is the discriminator
    # Last, so a token above keeps its own label. Only the `v` group is replaced, and a reference
    # (_REFERENCE) is kept. `\\?` takes the escaped quotes of raw JSONL. A quoted key needs a quoted
    # value, so `"secret":true` is left alone instead of becoming invalid JSON; the value takes an escape
    # pair but never `\"`, so a redaction never eats the escape of a closing quote.
    ("credential",   re.compile(r"(?:" + _nocase("secret_key", "private_key", "access_key", "access_token",
                                                 "auth_token", "refresh_token", "client_secret", "api_key",
                                                 "password", "passwd", "secret")
                                + r""")\b(?:\\?["'][ \t]*:[ \t]*\\?["']|[ \t]*[:=][ \t]*(?:\\?["'])?)(?P<v>(?:\\[^"']|[^\t\n\v\f\r "'\\]){4,})""", _A)),
    # Not here: AWS secret access keys have no prefix, and `[A-Za-z0-9+/]{40}` matches every git SHA.
    # Email is identity, not a secret: redacted from privacy up.
]

# PRIVACY (level >= 1), additive, applied in this order (the spec table's).
PRIVACY = [
    ("conn_string", re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:[^\s:/@]+@\S+", _A)),
    ("bearer",      re.compile(r"(?i)" + _START + r"(?P<v>bearer\s+[A-Za-z0-9._-]{8,})", _A)),
    ("email",       re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", _A)),
    # Greedy on purpose. No leading \b: `aws_secret_access_key=` glues the keyword to a word char;
    # the trailing \b blocks `Secretary:`. Compound *_key forms come first, since alternation is
    # leftmost-first and a bare `secret` would match inside `SECRET_KEY` and then fail its \b.
    ("credential",  re.compile(r'''(?i)(?:secret[_-]?key|private[_-]?key|encryption[_-]?key|signing[_-]?key|password|passwd|pwd|secret|api[_-]?key|access[_-]?key|client[_-]?secret|token)\b\s*[:=]\s*["']?[^\s"']{4,}''', _A)),
    # internal TLDs only; a specific public hostname goes in the deny list
    ("hostname",    re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:internal|local|lan|corp|intranet|localdomain|home|test)\b", _A)),
]


def max_mode(a, b):
    """Return the higher-level mode NAME of a and b. Unknown names read as level 0 (secrets)."""
    return a if LEVELS.get(a, 0) >= LEVELS.get(b, 0) else b


def _sub(text, label, pat, allow, found):
    """Substitute pat with [REDACTED:label], keeping a match whose lowercased text is an allow term.
    A pattern with a `v` group replaces only that group. Records `label` in `found` only when
    something was redacted."""
    hit = [False]
    def repl(m):
        s = m.group(0)
        head, value = "", s
        if "v" in pat.groupindex:
            head, value = s[:m.start("v") - m.start()], m.group("v")
            if _REFERENCE.match(value):
                return s
        if s.lower() in allow or value.lower() in allow:
            return s
        hit[0] = True
        return f"{head}[REDACTED:{label}]"
    text = pat.sub(repl, text)
    if hit[0] and label not in found:
        found.append(label)
    return text


def redact(text, mode="secrets", deny=(), allow=()):
    """Return (redacted_text, [labels_found]). mode in secrets|privacy|strict; each match becomes
    [REDACTED:label]. `deny` terms apply in every mode; `allow` terms are never redacted."""
    if not text:
        return text, []
    level = LEVELS.get(mode, 0)
    allow_set = {a.lower() for a in allow if a}
    found = []
    for label, pat in PATTERNS:
        text = _sub(text, label, pat, allow_set, found)
    if level >= 1:
        for label, pat in PRIVACY:
            text = _sub(text, label, pat, allow_set, found)
    if deny:                                          # the operator's backstop: every mode
        for term in deny:
            term = (term or "").strip()
            if not term:
                continue
            hit = [False]
            def drepl(m, _hit=hit):
                s = m.group(0)
                if s.lower() in allow_set:
                    return s
                _hit[0] = True
                return "[REDACTED:denylisted]"
            text = re.compile(re.escape(term), re.I).sub(drepl, text)   # case-insensitive SUBSTRING replace
            if hit[0] and "denylisted" not in found:
                found.append("denylisted")
    return text, found
