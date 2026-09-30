from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from chartpub.archive import inspect_archive, package_chart
from chartpub.errors import PublicationError, RemoteConflict
from chartpub.git import Git, run
from chartpub.github import GitHubClient, fingerprint
from chartpub.index import add_artifact, load_index, remove_version, write_index
from chartpub.models import Artifact, PublicationContract
from chartpub.validation import validate


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Lifecycle:
    def __init__(
        self, contract: PublicationContract, git: Git, github: GitHubClient, state_dir: Path
    ) -> None:
        self.c = contract
        self.git = git
        self.api = github
        self.directory = state_dir
        self.state_path = state_dir / "recovery.json"
        self.state: dict[str, Any] = (
            json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        )
        if self.state and self.state["contract"] != vars(contract):
            raise RemoteConflict("journal belongs to another contract")

    def save(self, **updates: Any) -> None:
        self.state.update(updates)
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, sort_keys=True, indent=2) + "\n")
        temporary.replace(self.state_path)

    def observe(self) -> dict[str, Any]:
        releases = self.api.list_releases()
        selected: dict[str, Any] = {}
        for tag in (self.c.bad_tag, self.c.replacement_tag):
            matches = [r for r in releases if r["tag_name"] == tag]
            if len(matches) > 1:
                raise RemoteConflict(f"multiple releases for {tag}")
            selected[tag] = matches[0] if matches else None
        return {
            "main": self.api.get_ref("heads/" + self.c.source_branch),
            "pages": self.api.get_ref("heads/" + self.c.pages_branch),
            "bad_tag": self.api.get_ref("tags/" + self.c.bad_tag),
            "replacement_tag": self.api.get_ref("tags/" + self.c.replacement_tag),
            "releases": selected,
        }

    def begin(self, observed: dict[str, Any]) -> None:
        if not self.state:
            if observed["pages"] != self.c.expected_pages_tip:
                raise RemoteConflict(
                    f"gh-pages: expected {self.c.expected_pages_tip}, "
                    f"observed {observed['pages']}; no recovery journal"
                )
            if observed["bad_tag"] not in (None, self.c.expected_bad_tag_target):
                raise RemoteConflict("bad tag differs from contract")
            self.save(
                contract=vars(self.c),
                pages=observed["pages"],
                source=self.git.call("rev-parse", "HEAD"),
                releases=observed["releases"],
                phase="prepared",
            )
        expected = self.state.get("pages_next")
        if observed["pages"] == expected and expected:
            self.save(pages=expected, pages_next=None)
        if observed["pages"] != self.state["pages"]:
            raise RemoteConflict(
                f"gh-pages: expected {self.state['pages']}, observed {observed['pages']}"
            )
        if observed["main"] != self.state["source"]:
            raise RemoteConflict("main must equal the verified source commit in the journal")
        if self.git.call("rev-parse", "HEAD") != self.state["source"]:
            raise RemoteConflict("checkout differs from journal source")

    def files(self, tip: str | None) -> dict[str, bytes]:
        if not tip:
            return {}
        self.git.call("fetch", "--no-tags", "origin", tip)
        names = self.git.call("ls-tree", "-r", "--name-only", tip).splitlines()
        return {name: run(["git", "show", f"{tip}:{name}"], cwd=self.git.root) for name in names}

    def reconstruct(self, files: dict[str, bytes]) -> tuple[dict[str, bytes], dict[str, Any]]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.yaml"
            if "index.yaml" in files:
                path.write_bytes(files["index.yaml"])
            index = load_index(path)
        # Public release assets are a recovery source if the Pages archive/index is absent.
        for release in self.api.list_releases():
            if release["draft"] or release["tag_name"] == self.c.bad_tag:
                continue
            for asset in release["assets"]:
                name = asset["name"]
                if not name.endswith(".tgz"):
                    continue
                if Path(name).name != name:
                    raise PublicationError("unsafe asset name")
                data = self.api.download(asset["id"])
                metadata = inspect_archive(data)
                actual = digest(data)
                if asset.get("digest") and asset["digest"] != "sha256:" + actual:
                    raise PublicationError("release asset digest mismatch")
                if name in files and files[name] != data:
                    raise PublicationError("Pages/release archive disagreement")
                files[name] = data
                add_artifact(
                    index,
                    Artifact(Path(name), metadata["name"], metadata["version"], actual, len(data)),
                    self.c.pages_url,
                )
        for chart, entries in index["entries"].items():
            for entry in entries:
                if chart == self.c.chart and entry["version"] == self.c.bad_version:
                    continue
                for url in entry["urls"]:
                    name = url.rsplit("/", 1)[-1]
                    if name not in files or digest(files[name]) != entry["digest"]:
                        raise PublicationError(f"cannot reconstruct indexed archive {name}")
                    metadata = inspect_archive(files[name])
                    if metadata["name"] != chart or metadata["version"] != entry["version"]:
                        raise PublicationError("index/archive metadata disagreement")
        return files, index

    def snapshot(self, files: dict[str, bytes], index: dict[str, Any]) -> str:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.yaml"
            write_index(path, index)
            files["index.yaml"] = path.read_bytes()
        files[".nojekyll"] = b""
        return self.git.snapshot(files)

    def write_pages(self, files: dict[str, bytes], index: dict[str, Any]) -> None:
        new = self.snapshot(files, index)
        if new == self.state["pages"]:
            return
        self.save(pages_next=new, phase="pages-prepared")
        self.git.cas("refs/heads/" + self.c.pages_branch, self.state["pages"], new)
        self.save(pages=new, pages_next=None, phase="pages-updated")

    def guarded_release(self, tag: str, observed: dict[str, Any] | None) -> None:
        expected = self.state["releases"].get(tag)
        if fingerprint(observed or {}) != fingerprint(expected or {}):
            upload = self.state.get("uploading")
            if upload and tag == self.c.replacement_tag and observed and expected:
                added = [a for a in observed["assets"] if a["name"] == upload["name"]]
                comparison = dict(observed)
                comparison["assets"] = [
                    a for a in observed["assets"] if a["name"] != upload["name"]
                ]
                comparison["updated_at"] = expected.get("updated_at")
                if len(added) == 1 and fingerprint(comparison) == fingerprint(expected):
                    if digest(self.api.download(added[0]["id"])) != upload["sha256"]:
                        raise PublicationError("uploaded asset digest mismatch")
                    self.remember(tag, observed)
                    return
            intent = self.state.get("release_intent")
            # Accept only the exact intended draft transition after a lost response.
            comparison = dict(observed or {})
            previous = dict(expected or {})
            if intent and intent["tag"] == tag and previous:
                comparison.pop("updated_at", None)
                previous.pop("updated_at", None)
                previous["draft"] = intent["draft"]
                if fingerprint(comparison) == fingerprint(previous):
                    self.remember(tag, observed)
                    return
            raise RemoteConflict(f"release {tag} changed since journal capture")

    def remember(self, tag: str, release: dict[str, Any] | None) -> None:
        self.state["releases"][tag] = release
        self.save(release_intent=None)

    def transition(self, release: dict[str, Any], draft: bool) -> dict[str, Any]:
        self.save(release_intent={"tag": release["tag_name"], "draft": draft})
        updated = self.api.patch_release(release, {"draft": draft})
        self.remember(release["tag_name"], updated)
        return updated

    def withdraw(self) -> dict[str, Any]:
        observed = self.observe()
        self.begin(observed)
        tag = observed["bad_tag"]
        if tag not in (None, self.c.expected_bad_tag_target):
            raise RemoteConflict(
                f"{self.c.bad_tag}: expected {self.c.expected_bad_tag_target}, observed {tag}"
            )
        release = observed["releases"][self.c.bad_tag]
        self.guarded_release(self.c.bad_tag, release)
        # Prepare/verify the full snapshot before the first destructive transition.
        files, index = self.reconstruct(self.files(observed["pages"]))
        remove_version(index, self.c.chart, self.c.bad_version)
        files.pop(self.c.asset_name(self.c.bad_version), None)
        files.pop(self.c.asset_name(self.c.bad_version) + ".sha256", None)
        if release and not release["draft"]:
            self.transition(release, True)
        if tag:
            self.git.cas("refs/tags/" + self.c.bad_tag, tag, None)
        self.write_pages(files, index)
        self.save(phase="withdrawn")
        return self.audit()

    def publish(self) -> dict[str, Any]:
        self.git.call(
            "diff",
            "--exit-code",
            "HEAD",
            "--",
            "charts",
            "tests/fixtures",
            "publication-contract.json",
            "src",
        )
        artifact = package_chart(
            self.git.root / self.c.chart_dir,
            self.directory / "artifacts",
            self.c.replacement_version,
        )
        validate(artifact, self.git.root / "tests/fixtures")
        observed = self.observe()
        self.begin(observed)
        if observed["bad_tag"] or (
            observed["releases"][self.c.bad_tag]
            and not observed["releases"][self.c.bad_tag]["draft"]
        ):
            raise RemoteConflict("withdraw the bad public version before publication")
        files, index = self.reconstruct(self.files(observed["pages"]))
        remove_version(index, self.c.chart, self.c.bad_version)
        files.pop(self.c.asset_name(self.c.bad_version), None)
        expected_tag = observed["replacement_tag"]
        if expected_tag not in (None, self.state["source"]):
            raise RemoteConflict("replacement tag target conflict")
        tag = self.c.replacement_tag
        release = observed["releases"][tag]
        # Reconcile a lost create response by its unique transaction marker and target.
        marker = f"chartpub {self.state['source']} sha256:{artifact.sha256}"
        if release and not self.state["releases"].get(tag) and self.state.get("creating") == marker:
            if (
                release.get("body") != marker
                or release.get("target_commitish") != self.state["source"]
            ):
                raise RemoteConflict("unexpected replacement release")
            self.remember(tag, release)
        self.guarded_release(tag, release)
        if not release:
            self.save(creating=marker, phase="release-prepared")
            release = self.api.request(
                "POST",
                self.api.base + "/releases",
                payload={
                    "tag_name": tag,
                    "target_commitish": self.state["source"],
                    "name": tag,
                    "body": marker,
                    "draft": True,
                },
            ).body
            self.remember(tag, release)
        checksum = f"{artifact.sha256}  {artifact.path.name}\n".encode()
        for name, content in (
            (artifact.path.name, artifact.path.read_bytes()),
            (artifact.path.name + ".sha256", checksum),
        ):
            current = self.api.release(release["id"]).body
            self.guarded_release(tag, current)
            matches = [a for a in current["assets"] if a["name"] == name]
            if len(matches) > 1:
                raise RemoteConflict("duplicate remote assets")
            if not matches:
                self.guarded_release(tag, current)
                if not current["draft"]:
                    raise RemoteConflict("refusing to modify a public release with missing assets")
                self.save(uploading={"name": name, "sha256": digest(content)})
                self.api.request(
                    "POST",
                    f"https://uploads.github.com/repos/{self.c.repository}"
                    f"/releases/{release['id']}/assets?name={name}",
                    data=content,
                    content_type="application/octet-stream",
                )
                current = self.api.release(release["id"]).body
                self.guarded_release(tag, current)
                matches = [a for a in current["assets"] if a["name"] == name]
            if not matches or self.api.download(matches[0]["id"]) != content:
                raise PublicationError(f"downloaded asset digest mismatch: {name}")
            self.remember(tag, current)
            release = current
        self.save(phase="release-verified", sha256=artifact.sha256, uploading=None)
        if expected_tag is None:
            self.git.cas("refs/tags/" + tag, None, self.state["source"])
        if release["draft"]:
            release = self.transition(release, False)
        # Recheck all public pointers after uploads and immediately before discoverability.
        latest = self.observe()
        if (
            latest["replacement_tag"] != self.state["source"]
            or latest["bad_tag"]
            or latest["main"] != self.state["source"]
        ):
            raise RemoteConflict("tag changed before Pages publication")
        self.guarded_release(tag, latest["releases"][tag])
        files[artifact.path.name] = artifact.path.read_bytes()
        files[artifact.path.name + ".sha256"] = checksum
        add_artifact(index, artifact, self.c.pages_url)
        self.write_pages(files, index)
        self.save(phase="complete", release_url=release["html_url"])
        return self.audit()

    def rollback(self) -> dict[str, Any]:
        observed = self.observe()
        self.begin(observed)
        if self.state.get("phase") == "complete" or self.state.get("pages_next"):
            raise RemoteConflict(
                "rollback requires an undiscoverable candidate; use forward repair"
            )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.yaml"
            files = self.files(observed["pages"])
            if "index.yaml" in files:
                path.write_bytes(files["index.yaml"])
            index = load_index(path)
        if any(
            entry["version"] == self.c.replacement_version
            for entry in index["entries"].get(self.c.chart, [])
        ):
            raise RemoteConflict("candidate is already discoverable; use forward repair")
        release = observed["releases"][self.c.replacement_tag]
        self.guarded_release(self.c.replacement_tag, release)
        try:
            if release and not release["draft"]:
                self.transition(release, True)
            tag = observed["replacement_tag"]
            if tag:
                if tag != self.state["source"]:
                    raise RemoteConflict("rollback tag conflict")
                self.git.cas("refs/tags/" + self.c.replacement_tag, tag, None)
        except PublicationError as exc:
            self.save(phase="rollback-failed")
            raise PublicationError(f"rollback incomplete: {exc}") from None
        self.save(phase="rolled-back")
        return self.audit()

    def plan(self, action: str) -> dict[str, Any]:
        observed = self.observe()
        files, index = self.reconstruct(self.files(observed["pages"]))
        changes: list[str] = []
        bad = observed["releases"][self.c.bad_tag]
        if action != "publish":
            if bad and not bad["draft"]:
                changes.append(f"quarantine release {bad['id']} (retain identity/assets)")
            if observed["bad_tag"]:
                changes.append(f"delete {self.c.bad_tag} at {self.c.expected_bad_tag_target}")
        remove_version(index, self.c.chart, self.c.bad_version)
        files.pop(self.c.asset_name(self.c.bad_version), None)
        files.pop(self.c.asset_name(self.c.bad_version) + ".sha256", None)
        sha256: str | None = None
        if action != "withdraw":
            with tempfile.TemporaryDirectory() as directory:
                artifact = package_chart(
                    self.git.root / self.c.chart_dir, Path(directory), self.c.replacement_version
                )
                sha256 = artifact.sha256
                files[artifact.path.name] = artifact.path.read_bytes()
                files[artifact.path.name + ".sha256"] = f"{sha256}  {artifact.path.name}\n".encode()
                add_artifact(index, artifact, self.c.pages_url)
            replacement = observed["releases"][self.c.replacement_tag]
            if not replacement:
                changes.append(
                    f"create draft release {self.c.replacement_tag}; upload/verify assets"
                )
            elif replacement["draft"]:
                changes.append(f"resume/verify assets and publish release {replacement['id']}")
            if not observed["replacement_tag"]:
                changes.append(f"create {self.c.replacement_tag} at verified source")
        target = self.snapshot(files, index)
        return {
            "github_changes": changes,
            "pages_old": observed["pages"],
            "pages_new": target,
            "force_with_lease_required": target != observed["pages"],
            "sha256": sha256,
            "local_changes": ["temporary package and Git snapshot objects"],
            "remote_writes_performed": [],
            "validation_required_before_publish": True,
        }

    def audit(self) -> dict[str, Any]:
        observed = self.observe()
        files = self.files(observed["pages"])
        problems: list[str] = []
        try:
            live = self.api.public_bytes(self.c.pages_url + "/index.yaml")
            if live != files.get("index.yaml"):
                problems.append("live Pages index differs from branch (deployment may be pending)")
        except PublicationError as exc:
            problems.append(str(exc))
        try:
            self.reconstruct(dict(files))
        except PublicationError as exc:
            problems.append(str(exc))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.yaml"
            path.write_bytes(files.get("index.yaml", b"apiVersion: v1\nentries: {}\n"))
            index = load_index(path)
        versions = [e["version"] for e in index["entries"].get(self.c.chart, [])]
        if self.c.bad_version in versions or observed["bad_tag"]:
            problems.append("bad version is still public")
        bad = observed["releases"][self.c.bad_tag]
        if bad and not bad["draft"]:
            problems.append("bad release is not quarantined")
        replacement = observed["releases"][self.c.replacement_tag]
        if self.c.replacement_version not in versions:
            problems.append("replacement is not indexed")
        elif (
            not replacement
            or replacement["draft"]
            or observed["replacement_tag"] != observed["main"]
        ):
            problems.append("indexed replacement release/tag disagrees with main")
        if len(versions) != len(set(versions)):
            problems.append("duplicate index versions")
        return {
            "repository": self.c.repository,
            "refs": {k: v for k, v in observed.items() if k != "releases"},
            "versions": versions,
            "problems": problems,
            "phase": self.state.get("phase"),
            "releases": {
                tag: {"id": r["id"], "draft": r["draft"]} if r else None
                for tag, r in observed["releases"].items()
            },
        }
