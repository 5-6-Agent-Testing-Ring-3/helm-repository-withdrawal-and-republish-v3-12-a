from __future__ import annotations

import re
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from chartpub.errors import ContractError


@dataclass(frozen=True)
class PublicationContract:
    schema_version: int
    repository: str
    source_branch: str
    pages_branch: str
    pages_url: str
    chart: str
    bad_version: str
    replacement_version: str
    bad_tag: str
    replacement_tag: str
    expected_bad_tag_target: str
    expected_pages_tip: str
    release_asset_name: str

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> PublicationContract:
        unknown = set(raw) - {field.name for field in fields(cls)}
        if unknown:
            raise ContractError("unknown contract keys")
        if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
            raise ContractError("schema_version must be 1")
        for key, value in raw.items():
            if key != "schema_version" and (not isinstance(value, str) or not value):
                raise ContractError(f"invalid {key}")
        result = cls(**raw)
        for key in (
            "chart",
            "bad_version",
            "replacement_version",
            "bad_tag",
            "replacement_tag",
            "source_branch",
            "pages_branch",
        ):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", raw[key]):
                raise ContractError(f"unsafe {key}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", result.repository):
            raise ContractError("invalid repository")
        for key in ("expected_bad_tag_target", "expected_pages_tip"):
            if not re.fullmatch(r"[0-9a-f]{40}", raw[key]):
                raise ContractError(f"invalid {key}")
        if (
            result.bad_version == result.replacement_version
            or result.bad_tag == result.replacement_tag
            or result.source_branch == result.pages_branch
        ):
            raise ContractError("publication targets must be distinct")
        if result.release_asset_name != result.chart + "-{version}.tgz":
            raise ContractError("invalid release_asset_name")
        if not result.pages_url.startswith("https://") or "@" in result.pages_url:
            raise ContractError("invalid pages_url")
        return result

    def asset_name(self, version: str) -> str:
        return self.release_asset_name.format(version=version)

    @property
    def chart_dir(self) -> Path:
        return Path("charts") / self.chart


@dataclass(frozen=True)
class Artifact:
    path: Path
    name: str
    version: str
    sha256: str
    size: int


@dataclass(frozen=True)
class RemoteSnapshot:
    main_tip: str
    pages_tip: str
    bad_tag_target: str | None
    bad_release_state: str | None
