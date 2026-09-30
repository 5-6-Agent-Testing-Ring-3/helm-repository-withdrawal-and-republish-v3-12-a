from __future__ import annotations

import uuid
from pathlib import Path

from chartpub.archive import verify_archive
from chartpub.errors import PublicationError
from chartpub.git import run
from chartpub.models import Artifact


def validate(artifact: Artifact, fixtures: Path) -> None:
    if not verify_archive(artifact.path, artifact.sha256):
        raise PublicationError("candidate digest mismatch")
    run(["helm", "lint", str(artifact.path)])
    values = sorted(fixtures.glob("*.yaml"))
    if not values:
        raise PublicationError("no values fixtures supplied")
    for fixture in values:
        run(["helm", "template", "chartpub-test", str(artifact.path), "-f", str(fixture)])
    namespace = "chartpub-" + uuid.uuid4().hex[:12]
    run(["kubectl", "create", "namespace", namespace])
    failure: Exception | None = None
    try:
        run(["helm", "install", "candidate", str(artifact.path), "--namespace", namespace])
    except PublicationError as exc:
        failure = exc
    cleanup: list[str] = []
    for command in (
        ["helm", "uninstall", "candidate", "--namespace", namespace, "--ignore-not-found"],
        ["kubectl", "delete", "namespace", namespace, "--wait=false"],
    ):
        try:
            run(command)
        except PublicationError:
            cleanup.append(command[0])
    if cleanup:
        raise PublicationError(f"validation cleanup failed: {cleanup}; install failure: {failure}")
    if failure:
        raise failure
