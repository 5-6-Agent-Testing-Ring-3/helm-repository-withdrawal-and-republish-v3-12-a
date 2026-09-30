from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from chartpub.errors import PublicationError
from chartpub.models import Artifact


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_archive(data: bytes) -> dict[str, Any]:
    seen: set[str] = set()
    roots: set[str] = set()
    metadata: dict[str, Any] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or str(path) != member.name.rstrip("/")
                    or str(path) in seen
                    or not path.parts
                    or not (member.isfile() or member.isdir())
                ):
                    raise PublicationError("unsafe or duplicate archive member")
                seen.add(str(path))
                roots.add(path.parts[0])
                if len(path.parts) == 2 and path.name == "Chart.yaml":
                    stream = archive.extractfile(member)
                    if stream is not None:
                        value = yaml.safe_load(stream)
                        if not isinstance(value, dict):
                            raise PublicationError("chart metadata must be a mapping")
                        metadata = value
        if len(roots) != 1 or not metadata or metadata.get("name") not in roots:
            raise PublicationError("archive must contain one chart with matching metadata")
        return metadata
    except (tarfile.TarError, OSError, ValueError, yaml.YAMLError) as exc:
        raise PublicationError("invalid chart archive") from exc


def package_chart(chart_dir: Path, output_dir: Path, version: str) -> Artifact:
    if chart_dir.is_symlink():
        raise PublicationError("chart directory must not be a symlink")
    output_dir.mkdir(parents=True, exist_ok=True)
    name = chart_dir.name
    output = output_dir / f"{name}-{version}.tgz"
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive,
    ):
        for path in sorted(chart_dir.rglob("*")):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise PublicationError("chart contains unsafe filesystem entries")
            if path.is_file():
                data = path.read_bytes()
                info = tarfile.TarInfo(f"{name}/{path.relative_to(chart_dir).as_posix()}")
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    data = buffer.getvalue()
    metadata = inspect_archive(data)
    if metadata.get("version") != version:
        raise PublicationError("chart version disagrees with contract")
    output.write_bytes(data)
    return Artifact(output, name, version, sha256_file(output), len(data))


def verify_archive(path: Path, expected_sha256: str) -> bool:
    inspect_archive(path.read_bytes())
    return sha256_file(path) == expected_sha256
