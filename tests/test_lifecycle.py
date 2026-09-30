from __future__ import annotations

import copy
import io
import json
import shutil
import urllib.error
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from chartpub.archive import package_chart
from chartpub.errors import PublicationError, RemoteConflict
from chartpub.git import Git, run
from chartpub.github import GitHubClient
from chartpub.lifecycle import Lifecycle
from chartpub.models import PublicationContract

from .test_config import valid_contract


class Reply(io.BytesIO):
    status = 200
    headers = {"ETag": '"revision"'}


class Server:
    def __init__(self):
        self.releases = {}
        self.assets = {}
        self.refs = {}
        self.writes = []
        self.fail_upload = False
        self.corrupt = False

    def open(self, request, timeout=0):
        path = urlsplit(request.full_url).path.split("/repos/owner/repo")[-1]
        method = request.method
        body = None
        if method != "GET":
            self.writes.append((method, path))
        if path.startswith("/git/ref/"):
            ref = path.removeprefix("/git/ref/")
            if ref not in self.refs:
                raise urllib.error.HTTPError(request.full_url, 404, "", {}, None)
            body = {"object": {"sha": self.refs[ref]}}
        elif path == "/releases" and method == "GET":
            body = list(self.releases.values())
        elif path == "/releases" and method == "POST":
            body = json.loads(request.data)
            body.update(id=2, assets=[], html_url="https://example.test/release/2", updated_at="0")
            self.releases[2] = body
        elif path.startswith("/releases/assets/"):
            data = self.assets[int(path.rsplit("/", 1)[1])]
            return Reply(b"corrupt" if self.corrupt else data)
        elif path.endswith("/assets"):
            release = self.releases[int(path.split("/")[2])]
            if method == "GET":
                body = release["assets"]
            else:
                name = parse_qs(urlsplit(request.full_url).query)["name"][0]
                asset_id = len(self.assets) + 10
                body = {"id": asset_id, "name": name}
                self.assets[asset_id] = request.data
                release["assets"].append(body)
                if self.fail_upload:
                    self.fail_upload = False
                    raise OSError("lost upload response")
        else:
            release_id = int(path.rsplit("/", 1)[1])
            if release_id not in self.releases:
                raise urllib.error.HTTPError(request.full_url, 404, "", {}, None)
            body = self.releases[release_id]
            if method == "PATCH":
                body.update(json.loads(request.data))
        return Reply(json.dumps(body).encode())


@pytest.fixture
def world(tmp_path, monkeypatch, chart_dir):
    root = tmp_path / "repo"
    root.mkdir()
    run(["git", "init", "-b", "main"], cwd=root)
    run(["git", "config", "user.email", "test@example.test"], cwd=root)
    run(["git", "config", "user.name", "Test"], cwd=root)
    run(["git", "remote", "add", "origin", "https://github.com/owner/repo.git"], cwd=root)
    shutil.copytree(chart_dir, root / "charts/ledger-api")
    shutil.copytree(Path("tests/fixtures"), root / "tests/fixtures")
    run(["git", "add", "."], cwd=root)
    run(["git", "commit", "-m", "source"], cwd=root)
    git = Git(root, "owner/repo", "fake-token")
    server = Server()
    server.refs["heads/main"] = git.call("rev-parse", "HEAD")
    contract = PublicationContract.from_mapping(valid_contract())
    original = git.call

    def call(*args, data=None):
        if args[0] == "ls-remote":
            sha = server.refs.get(args[-1].removeprefix("refs/"))
            return f"{sha}\t{args[-1]}" if sha else ""
        if args[0] == "fetch":
            return ""
        if args[0] == "push":
            value, ref = args[-1].split(":")
            if value:
                server.refs[ref.removeprefix("refs/")] = value
            else:
                server.refs.pop(ref.removeprefix("refs/"), None)
            return ""
        return original(*args, data=data)

    monkeypatch.setattr(git, "call", call)
    monkeypatch.setattr("chartpub.github.open_url", server.open)
    monkeypatch.setattr("chartpub.lifecycle.validate", lambda *args: None)
    tip = git.snapshot({"index.yaml": b"apiVersion: v1\nentries: {}\n", "README.md": b"preserve"})
    server.refs["heads/gh-pages"] = tip
    server.refs["tags/chart-v0.4.0"] = contract.expected_bad_tag_target
    server.releases[1] = {
        "id": 1,
        "tag_name": contract.bad_tag,
        "draft": False,
        "assets": [],
        "name": "incident evidence",
        "body": "keep me",
        "updated_at": "0",
    }
    contract = replace(contract, expected_pages_tip=tip)
    lifecycle = Lifecycle(
        contract, git, GitHubClient("owner/repo", "fake-token"), tmp_path / "state"
    )
    monkeypatch.setattr(
        lifecycle.api,
        "public_bytes",
        lambda url: lifecycle.files(server.refs.get("heads/gh-pages")).get("index.yaml", b""),
    )
    return lifecycle, server


def test_end_to_end_idempotent_and_exact_scope(world):
    life, server = world
    server.releases[9] = {"id": 9, "tag_name": "unrelated", "draft": False, "assets": []}
    server.refs["tags/unrelated"] = "f" * 40
    before = copy.deepcopy(server.releases[1])
    assert life.withdraw()["phase"] == "withdrawn"
    assert server.releases[1] == before | {"draft": True}
    assert life.publish()["problems"] == []
    tips = dict(server.refs)
    writes = list(server.writes)
    assert life.publish()["problems"] == []
    life.withdraw()
    assert server.refs == tips
    assert server.writes == writes
    assert server.releases[9]["tag_name"] == "unrelated"
    assert life.files(tips["heads/gh-pages"])["README.md"] == b"preserve"
    assert server.refs["tags/unrelated"] == "f" * 40


def test_partial_upload_recovery(world):
    life, server = world
    life.withdraw()
    old = server.refs["heads/gh-pages"]
    server.fail_upload = True
    with pytest.raises(PublicationError):
        life.publish()
    assert server.refs["heads/gh-pages"] == old
    resumed = Lifecycle(life.c, life.git, life.api, life.directory)
    assert resumed.publish()["problems"] == []
    assert len(server.releases[2]["assets"]) == 2


def test_validation_and_digest_failures_leave_pages_unchanged(world, monkeypatch):
    life, server = world
    before = dict(server.refs)

    def fail(*args):
        raise PublicationError("invalid deployment")

    monkeypatch.setattr("chartpub.lifecycle.validate", fail)
    with pytest.raises(PublicationError, match="deployment"):
        life.publish()
    assert not server.writes
    assert server.refs == before
    monkeypatch.setattr("chartpub.lifecycle.validate", lambda *a: None)
    life.withdraw()
    before = dict(server.refs)
    server.corrupt = True
    with pytest.raises(PublicationError, match="digest mismatch"):
        life.publish()
    assert server.refs == before


@pytest.mark.parametrize("kind", ["pages", "bad_tag", "main", "source", "release", "replacement"])
def test_conflicts(world, kind):
    life, server = world
    if kind == "pages":
        server.refs["heads/gh-pages"] = "f" * 40
    elif kind == "bad_tag":
        server.refs["tags/chart-v0.4.0"] = "f" * 40
    else:
        life.withdraw()
        if kind == "main":
            server.refs["heads/main"] = "f" * 40
        elif kind == "source":
            life.state["source"] = "f" * 40
            server.refs["heads/main"] = "f" * 40
        elif kind == "release":
            server.releases[1]["body"] = "concurrent edit"
        elif kind == "replacement":
            server.refs["tags/chart-v0.4.1"] = "f" * 40
    with pytest.raises(RemoteConflict):
        life.publish() if kind == "replacement" else life.withdraw()


def test_pages_push_lost_response_recovers(world, monkeypatch):
    life, server = world
    original = life.git.cas

    def lost(ref, old, new):
        original(ref, old, new)
        if ref == "refs/heads/gh-pages":
            raise PublicationError("lost push response")

    monkeypatch.setattr(life.git, "cas", lost)
    with pytest.raises(PublicationError):
        life.withdraw()
    monkeypatch.setattr(life.git, "cas", original)
    assert life.withdraw()["phase"] == "withdrawn"


def test_reconstruct_from_release_and_retain_valid_versions(world, tmp_path):
    life, server = world
    artifact = package_chart(life.git.root / life.c.chart_dir, tmp_path, "0.4.1")
    server.assets[90] = artifact.path.read_bytes()
    server.releases[9] = {
        "id": 9,
        "tag_name": "retained",
        "draft": False,
        "assets": [{"id": 90, "name": artifact.path.name}],
    }
    files, index = life.reconstruct({})
    assert files[artifact.path.name] == artifact.path.read_bytes()
    assert index["entries"]["ledger-api"][0]["digest"] == artifact.sha256
    server.releases[9]["assets"][0]["digest"] = "sha256:wrong"
    with pytest.raises(PublicationError, match="digest"):
        life.reconstruct({})
    del server.releases[9]["assets"][0]["digest"]
    with pytest.raises(PublicationError, match="disagreement"):
        life.reconstruct({artifact.path.name: b"wrong"})
    server.releases[9]["assets"][0]["name"] = "../unsafe.tgz"
    with pytest.raises(PublicationError, match="unsafe"):
        life.reconstruct({})


def test_rollback_and_failure_reporting(world, monkeypatch):
    life, server = world
    life.withdraw()
    original = life.write_pages
    monkeypatch.setattr(
        life, "write_pages", lambda *a: (_ for _ in ()).throw(PublicationError("pages"))
    )
    with pytest.raises(PublicationError):
        life.publish()
    original_cas = life.git.cas
    monkeypatch.setattr(
        life.git, "cas", lambda *a: (_ for _ in ()).throw(PublicationError("lease"))
    )
    with pytest.raises(PublicationError, match="rollback incomplete"):
        life.rollback()
    assert life.state["phase"] == "rollback-failed"
    monkeypatch.setattr(life.git, "cas", original_cas)
    assert life.rollback()["phase"] == "rolled-back"
    assert server.releases[2]["draft"]
    monkeypatch.setattr(life, "write_pages", original)
    life.publish()
    with pytest.raises(RemoteConflict, match="forward repair"):
        life.rollback()


def test_contract_journal_and_release_identity(world):
    life, server = world
    life.withdraw()
    with pytest.raises(RemoteConflict, match="another contract"):
        Lifecycle(replace(life.c, chart="different"), life.git, life.api, life.directory)
    server.releases[3] = dict(server.releases[1], id=3)
    with pytest.raises(RemoteConflict, match="multiple releases"):
        life.observe()


def test_lost_draft_transition(world, monkeypatch):
    life, server = world
    patch = life.api.patch_release

    def lost(*args):
        patch(*args)
        raise PublicationError("response lost")

    monkeypatch.setattr(life.api, "patch_release", lost)
    with pytest.raises(PublicationError):
        life.withdraw()
    monkeypatch.setattr(life.api, "patch_release", patch)
    life.withdraw()
    assert server.releases[1]["draft"]


def test_plan_is_deterministic_dry_and_reports_noop(world):
    life, server = world
    first = life.plan("plan")
    assert first == life.plan("plan")
    assert first["force_with_lease_required"]
    assert not server.writes
    assert not life.state_path.exists()
    assert life.plan("withdraw")["sha256"] is None
    life.withdraw()
    server.fail_upload = True
    with pytest.raises(PublicationError):
        life.publish()
    assert any("resume" in item for item in life.plan("publish")["github_changes"])
    life.publish()
    assert life.plan("plan")["github_changes"] == []
    assert not life.plan("plan")["force_with_lease_required"]


def test_audit_detects_live_lag(world, monkeypatch):
    life, server = world
    monkeypatch.setattr(life.api, "public_bytes", lambda url: b"stale")
    assert any("differs" in problem for problem in life.audit()["problems"])
    monkeypatch.setattr(
        life.api, "public_bytes", lambda url: (_ for _ in ()).throw(PublicationError("offline"))
    )
    assert "offline" in life.audit()["problems"]


def test_pages_lease_conflict(world):
    life, server = world
    with pytest.raises(RemoteConflict, match="expected"):
        life.git.cas("refs/heads/gh-pages", "a" * 40, "b" * 40)


def test_lost_create_response_resumes_same_release(world, monkeypatch):
    life, server = world
    life.withdraw()
    original = life.api.request

    def lost(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if method == "POST" and path.endswith("/releases"):
            raise PublicationError("lost create response")
        return result

    monkeypatch.setattr(life.api, "request", lost)
    with pytest.raises(PublicationError):
        life.publish()
    release_id = server.releases[2]["id"]
    monkeypatch.setattr(life.api, "request", original)
    life.publish()
    assert server.releases[2]["id"] == release_id
    assert len(server.releases) == 2


def test_rollback_never_removes_indexed_candidate_after_withdraw_rerun(world):
    life, server = world
    life.withdraw()
    life.publish()
    life.withdraw()
    with pytest.raises(RemoteConflict, match="already discoverable"):
        life.rollback()
