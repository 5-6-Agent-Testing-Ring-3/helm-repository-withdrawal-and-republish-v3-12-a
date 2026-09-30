from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from chartpub.models import Artifact

PageWriter = Callable[[Artifact], None]
ReleaseWriter = Callable[[Artifact], None]


class Publisher:
    def __init__(
        self, state_dir: Path, write_pages: PageWriter, write_release: ReleaseWriter
    ) -> None:
        self.state_dir = state_dir
        self.write_pages = write_pages
        self.write_release = write_release

    def publish(self, artifact: Artifact) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._write_state("prepared", artifact)
        self.write_release(artifact)
        self._write_state("release-verified", artifact)
        self.write_pages(artifact)
        self._write_state("complete", artifact)

    def _write_state(self, phase: str, artifact: Artifact) -> None:
        payload = {
            "phase": phase,
            "artifact": artifact.path.name,
            "sha256": artifact.sha256,
        }
        (self.state_dir / "transaction.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
