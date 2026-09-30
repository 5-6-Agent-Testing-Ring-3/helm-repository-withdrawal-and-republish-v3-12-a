from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from chartpub.errors import PublicationError
from chartpub.models import Artifact

EPOCH = "1970-01-01T00:00:00Z"


def load_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"apiVersion": "v1", "entries": {}}
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("entries"), dict):
        raise PublicationError("index must contain an entries mapping")
    return value


def add_artifact(index: dict[str, Any], artifact: Artifact, base_url: str) -> dict[str, Any]:
    versions = index.setdefault("entries", {}).setdefault(artifact.name, [])
    matches = [item for item in versions if item["version"] == artifact.version]
    if any(item["digest"] != artifact.sha256 for item in matches):
        raise PublicationError("immutable version digest conflict")
    if not matches:
        versions.append(
            {
                "apiVersion": "v2",
                "name": artifact.name,
                "version": artifact.version,
                "digest": artifact.sha256,
                "urls": [f"{base_url.rstrip('/')}/{artifact.path.name}"],
                "created": EPOCH,
            }
        )
    return index


def remove_version(index: dict[str, Any], chart: str, version: str) -> dict[str, Any]:
    entries = index.get("entries", {})
    entries[chart] = [item for item in entries.get(chart, []) if item.get("version") != version]
    return index


def write_index(path: Path, index: dict[str, Any]) -> None:
    for chart, versions in index["entries"].items():
        unique: dict[str, Any] = {}
        for item in versions:
            version = item["version"]
            if version in unique and item != unique[version]:
                raise PublicationError("conflicting duplicate index entries")
            unique[version] = item
        index["entries"][chart] = sorted(unique.values(), key=lambda x: x["version"], reverse=True)
    index["generated"] = EPOCH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(index, sort_keys=True), encoding="utf-8")
