"""Reading `.env`, without a dependency.

`python-dotenv` is twenty lines of behaviour in a package, and this project's
whole premise is that it installs nothing. So the parser lives here.

The rule that matters: **the real environment always wins.** A value already
exported in the shell is never overwritten by the file, so a CI secret or a
one-off `GROQ_API_KEY=... python -m finctl ...` behaves the way anyone would
expect, and a stale `.env` on a developer's disk cannot quietly override it.
"""
from __future__ import annotations

import os
from pathlib import Path

#: The project root, three levels up from this file.
_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Load `KEY=value` lines into the environment. Returns what it set.

    Tolerates comments, blank lines, `export ` prefixes and quoted values,
    because those are what people actually write. A malformed line is skipped
    rather than raising: a typo in a config file should not stop a
    reconciliation run that may not even need the key.
    """
    target = path or _ROOT / ".env"
    applied: dict[str, str] = {}

    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return applied

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        # Strip one matching pair of surrounding quotes, if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            continue

        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = value

    return applied


def describe_secrets() -> dict[str, bool]:
    """Which provider keys are visible, without revealing any of them.

    Used by the CLI and the dashboard to say what is configured. It reports
    presence only -- a value is never returned, logged or rendered.
    """
    return {
        name: bool(os.environ.get(name))
        for name in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
                     "ANTHROPIC_API_KEY")
    }
