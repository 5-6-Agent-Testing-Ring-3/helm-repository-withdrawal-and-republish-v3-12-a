"""Validate the reproducible package against the selected disposable Kubernetes cluster."""

from pathlib import Path

from chartpub.archive import package_chart
from chartpub.config import load_contract
from chartpub.validation import validate

contract = load_contract(Path("publication-contract.json"))
artifact = package_chart(contract.chart_dir, Path("dist/charts"), contract.replacement_version)
validate(artifact, Path("tests/fixtures"))
print(f"validated {artifact.path.name} sha256:{artifact.sha256}")
