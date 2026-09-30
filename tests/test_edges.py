from __future__ import annotations

import io
import json
import runpy
import tarfile
import urllib.error
from pathlib import Path
from unittest.mock import Mock

import pytest

from chartpub import cli
from chartpub.archive import inspect_archive, package_chart
from chartpub.config import load_contract
from chartpub.errors import ChartpubError, ContractError, PublicationError, RemoteConflict
from chartpub.git import Git, run
from chartpub.github import GitHubClient, Response
from chartpub.index import add_artifact, load_index, write_index
from chartpub.models import Artifact, PublicationContract
from chartpub.security import read_env_file, require_token
from chartpub.validation import validate

from .test_config import valid_contract, write_contract
from .test_index import artifact
from .test_lifecycle import Reply


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", "file"),
        ("/absolute", "file"),
        ("chart/./bad", "file"),
        ("chart/a", "link"),
        ("chart/a", "duplicate"),
        ("chart\\bad", "file"),
    ],
)
def test_reject_archive_members(name, kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        if kind == "link":
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
        archive.addfile(member)
        if kind == "duplicate":
            archive.addfile(member)
    with pytest.raises(PublicationError, match="unsafe|duplicate"):
        inspect_archive(buffer.getvalue())


def test_archive_invalid_metadata_and_symlinks(tmp_path, chart_dir):
    with pytest.raises(PublicationError):
        inspect_archive(b"not a tarball")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.addfile(tarfile.TarInfo("empty/file"))
    with pytest.raises(PublicationError, match="matching metadata"):
        inspect_archive(buffer.getvalue())
    with pytest.raises(PublicationError, match="version"):
        package_chart(chart_dir, tmp_path, "9.9.9")
    chart = tmp_path / "chart"
    chart.mkdir()
    (chart / "link").symlink_to(chart_dir / "Chart.yaml")
    with pytest.raises(PublicationError, match="unsafe"):
        package_chart(chart, tmp_path / "out", "0.4.1")


def test_index_validation_and_duplicates(tmp_path):
    path = tmp_path / "index.yaml"
    assert load_index(path)["entries"] == {}
    path.write_text("[]")
    with pytest.raises(PublicationError):
        load_index(path)
    index = {"apiVersion": "v1", "entries": {}}
    add_artifact(index, artifact(), "https://example.test")
    index["entries"]["ledger-api"] *= 2
    write_index(path, index)
    assert len(load_index(path)["entries"]["ledger-api"]) == 1
    other = Artifact(Path("ledger-api-0.4.1.tgz"), "ledger-api", "0.4.1", "b" * 64, 42)
    with pytest.raises(PublicationError, match="immutable"):
        add_artifact(index, other, "https://example.test")
    index["entries"]["ledger-api"].append(
        dict(index["entries"]["ledger-api"][0], digest="different")
    )
    with pytest.raises(PublicationError, match="duplicate"):
        write_index(path, index)


@pytest.mark.parametrize(
    "key,value",
    [
        ("chart", "../escape"),
        ("repository", "bad"),
        ("replacement_version", "0.4.0"),
        ("source_branch", "gh-pages"),
        ("pages_url", "http://bad"),
        ("release_asset_name", "../{version}"),
        ("expected_pages_tip", "bad"),
        ("chart", 42),
    ],
)
def test_contract_invalid(key, value):
    raw = valid_contract()
    raw[key] = value
    with pytest.raises(ContractError):
        PublicationContract.from_mapping(raw)


def test_contract_io_and_missing(tmp_path):
    path = tmp_path / "missing"
    with pytest.raises(ContractError):
        load_contract(path)
    path.write_text("{")
    with pytest.raises(ContractError):
        load_contract(path)
    write_contract(path, {"schema_version": 1})
    with pytest.raises(ContractError, match="malformed"):
        load_contract(path)


def test_credentials_invalid_do_not_echo(tmp_path):
    path = tmp_path / "credentials"
    with pytest.raises(ChartpubError):
        read_env_file(path)
    path.write_text("# comment\n\nsecret-no-equals")
    with pytest.raises(ChartpubError) as error:
        read_env_file(path)
    assert "secret-no-equals" not in str(error.value)
    with pytest.raises(ChartpubError):
        require_token({})


@pytest.mark.parametrize(
    "status,error",
    [
        (401, PublicationError),
        (412, RemoteConflict),
        (422, RemoteConflict),
        (500, PublicationError),
    ],
)
def test_http_errors_redacted(monkeypatch, status, error):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.github.com/secret", status, "secret", {}, io.BytesIO(b"secret")
        )

    monkeypatch.setattr("chartpub.github.open_url", fail)
    with pytest.raises(error) as exc:
        GitHubClient("owner/repo", "secret").request("POST", "/repos/owner/repo")
    assert "secret" not in str(exc.value)


def test_http_security_and_pagination(monkeypatch):
    client = GitHubClient("owner/repo", "secret")
    with pytest.raises(PublicationError, match="unexpected host"):
        client.request("GET", "https://attacker.test/")
    request = Mock(
        side_effect=[
            Response(200, [{"id": n} for n in range(100)], {}),
            Response(200, [{"id": 100}], {}),
        ]
    )
    monkeypatch.setattr(client, "request", request)
    assert len(client.paginate("/releases")) == 101
    assert "page=2" in request.call_args.args[1]
    request.side_effect = None
    request.return_value = Response(404, None, {})
    assert client.get_ref("tags/missing") is None
    with pytest.raises(PublicationError):
        client.paginate("/missing")
    with pytest.raises(RemoteConflict):
        client.release(4)
    with pytest.raises(PublicationError):
        client.download(4)
    request.return_value = Response(200, {"id": 4, "draft": False}, {})
    with pytest.raises(RemoteConflict):
        client.patch_release({"id": 4, "draft": True}, {"draft": False})


def test_http_binary_empty_and_conditional(monkeypatch):
    requests = []

    def respond(req, **kwargs):
        requests.append(req)
        return Reply(b"")

    monkeypatch.setattr("chartpub.github.open_url", respond)
    client = GitHubClient("owner/repo", "secret")
    assert client.request("DELETE", "/x", etag="revision").body is None
    assert requests[0].get_header("If-match") == "revision"
    assert client.request("GET", "/x", binary=True).body == b""


@pytest.mark.parametrize("failure", ["none", "install", "cleanup", "digest", "fixtures"])
def test_validation_order_and_cleanup(tmp_path, monkeypatch, chart_dir, failure):
    candidate = package_chart(chart_dir, tmp_path, "0.4.1")
    calls = []

    def command(args):
        calls.append(args)
        if (failure == "install" and args[:2] == ["helm", "install"]) or (
            failure == "cleanup" and args[:2] == ["helm", "uninstall"]
        ):
            raise PublicationError("test failure")
        return b""

    monkeypatch.setattr("chartpub.validation.run", command)
    if failure == "digest":
        monkeypatch.setattr("chartpub.validation.verify_archive", lambda *a: False)
    fixtures = tmp_path / "fixtures" if failure == "fixtures" else Path("tests/fixtures")
    if failure == "none":
        validate(candidate, fixtures)
        assert len([c for c in calls if c[:2] == ["helm", "template"]]) == 2
    else:
        with pytest.raises(PublicationError):
            validate(candidate, fixtures)
    if failure in ("install", "cleanup"):
        assert calls[-1][:3] == ["kubectl", "delete", "namespace"]


def test_cli_commands_and_redaction(tmp_path, monkeypatch, capsys):
    path = tmp_path / "contract.json"
    write_contract(path, valid_contract())
    credentials = tmp_path / "creds"
    credentials.write_text("GH_TOKEN=secret")
    lifecycle = Mock()
    lifecycle.audit.return_value = {"problems": []}
    lifecycle.plan.return_value = {"force_with_lease_required": False}
    lifecycle.publish.return_value = {"phase": "complete"}
    lifecycle.withdraw.return_value = {"phase": "withdrawn"}
    lifecycle.rollback.return_value = {"phase": "rolled-back"}
    monkeypatch.setattr(cli, "Git", Mock())
    monkeypatch.setattr(cli, "Lifecycle", Mock(return_value=lifecycle))
    base = ["--contract", str(path), "--credentials", str(credentials)]
    for command in ("publish", "withdraw", "repair", "audit", "plan"):
        assert cli.main([command, *base]) == 0
        json.loads(capsys.readouterr().out)
    assert cli.main(["repair", "--rollback", *base]) == 0
    lifecycle.reset_mock()
    assert cli.main(["publish", "--dry-run", *base]) == 0
    lifecycle.publish.assert_not_called()
    lifecycle.withdraw.assert_not_called()
    assert cli.main(["publish", "--dry-run", "--contract", str(path)]) == 0
    assert cli.main(["publish", "--contract", str(path)]) == 2
    lifecycle.audit.return_value = {"problems": ["broken"]}
    assert cli.main(["audit", *base]) == 1
    lifecycle.publish.side_effect = RemoteConflict("secret")
    assert cli.main(["publish", *base]) == 3
    assert "secret" not in capsys.readouterr().err
    monkeypatch.setattr("sys.argv", ["chartpub", "plan", "--contract", str(path)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("chartpub", run_name="__main__")
    assert exc.value.code == 0


def test_git_origin_and_subprocess_redaction(tmp_path):
    run(["git", "init"], cwd=tmp_path)
    run(["git", "remote", "add", "origin", "https://github.com/wrong/repo.git"], cwd=tmp_path)
    with pytest.raises(RemoteConflict, match="origin"):
        Git(tmp_path, "owner/repo")
    with pytest.raises(PublicationError) as exc:
        run(["git", "not-a-command", "secret"], cwd=tmp_path)
    assert "secret" not in str(exc.value)


def test_redirect_strips_credentials_and_rejects_http():
    import urllib.request

    from chartpub.github import SafeRedirect

    redirect = SafeRedirect()
    request = urllib.request.Request(
        "https://api.github.com/asset", headers={"Authorization": "secret"}
    )
    result = redirect.redirect_request(
        request, None, 302, "", {}, "https://release-assets.githubusercontent.com/file"
    )
    assert result.get_header("Authorization") is None
    result = redirect.redirect_request(request, None, 302, "", {}, "https://api.github.com/other")
    assert result.get_header("Authorization") == "secret"
    with pytest.raises(PublicationError):
        redirect.redirect_request(request, None, 302, "", {}, "http://unsafe.test/file")


def test_open_url_uses_safe_redirect(monkeypatch):
    import urllib.request

    from chartpub.github import SafeRedirect, open_url

    opener = Mock()
    factory = Mock(return_value=opener)
    monkeypatch.setattr("urllib.request.build_opener", factory)
    request = urllib.request.Request("https://api.github.com")
    open_url(request, 10)
    assert isinstance(factory.call_args.args[0], SafeRedirect)
    opener.open.assert_called_once_with(request, timeout=10)


def test_public_download_never_authenticates(monkeypatch):
    captured = []

    def respond(request, **kwargs):
        captured.append(request)
        return Reply(b"public index")

    monkeypatch.setattr("chartpub.github.open_url", respond)
    client = GitHubClient("owner/repo", "secret")
    assert client.public_bytes("https://owner.example/index.yaml") == b"public index"
    assert captured[0].get_header("Authorization") is None
    monkeypatch.setattr("chartpub.github.open_url", Mock(side_effect=OSError("secret")))
    with pytest.raises(PublicationError, match="public Pages download failed"):
        client.public_bytes("https://owner.example/index.yaml")
