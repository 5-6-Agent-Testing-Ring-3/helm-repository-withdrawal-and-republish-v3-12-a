from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from chartpub.errors import ChartpubError

SENSITIVE_NAMES = {"GITHUB_TOKEN", "GH_TOKEN"}


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ChartpubError(f"cannot read credential file: {path}") from exc
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator:
            raise ChartpubError("invalid environment entry")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def require_token(values: Mapping[str, str]) -> str:
    token = values.get("GITHUB_TOKEN") or values.get("GH_TOKEN") or ""
    if not token:
        raise ChartpubError("GitHub token is missing")
    return token


def redact(message: str, secrets: Mapping[str, str]) -> str:
    result = message
    for name in SENSITIVE_NAMES:
        value = secrets.get(name)
        if value:
            result = result.replace(value, "[REDACTED]")
    return result
