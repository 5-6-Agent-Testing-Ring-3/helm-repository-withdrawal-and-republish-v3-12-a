from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

from chartpub.errors import PublicationError, RemoteConflict


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    data: bytes | None = None,
) -> bytes:
    result = subprocess.run(args, cwd=cwd, env=env, input=data, capture_output=True)
    if result.returncode:
        # Never propagate subprocess output, which can contain authentication headers.
        raise PublicationError(f"{args[0]} operation failed (exit {result.returncode})")
    return result.stdout


class Git:
    def __init__(self, root: Path, repository: str, token: str = "") -> None:
        self.root = root
        self.repository = repository
        self.env = os.environ.copy()
        if token:
            auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            self.env.update(
                GIT_CONFIG_COUNT="2",
                GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {auth}",
                GIT_CONFIG_KEY_1="credential.helper",
                GIT_CONFIG_VALUE_1="",
                GIT_TERMINAL_PROMPT="0",
            )
        self.check_origin()

    def call(self, *args: str, data: bytes | None = None) -> str:
        return run(["git", *args], cwd=self.root, env=self.env, data=data).decode().strip()

    def check_origin(self) -> None:
        origin = self.call("remote", "get-url", "origin")
        allowed = {
            f"https://github.com/{self.repository}.git",
            f"https://github.com/{self.repository}",
            f"git@github.com:{self.repository}.git",
        }
        if origin not in allowed:
            raise RemoteConflict("credential-free origin disagrees with configured repository")

    def tip(self, ref: str) -> str | None:
        output = self.call("ls-remote", "origin", ref)
        return output.split()[0] if output else None

    def cas(self, ref: str, old: str | None, new: str | None) -> None:
        self.check_origin()
        if self.tip(ref) != old:
            raise RemoteConflict(f"{ref}: expected {old}, observed {self.tip(ref)}")
        self.call("push", f"--force-with-lease={ref}:{old or ''}", "origin", f"{new or ''}:{ref}")

    def snapshot(self, files: dict[str, bytes]) -> str:
        # A temporary index prevents modifying the operator's checkout/index.
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            env = self.env | {
                "GIT_INDEX_FILE": str(Path(directory) / "index"),
                "GIT_AUTHOR_NAME": "chartpub",
                "GIT_AUTHOR_EMAIL": "chartpub@localhost",
                "GIT_COMMITTER_NAME": "chartpub",
                "GIT_COMMITTER_EMAIL": "chartpub@localhost",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
            }

            def command(*args: str, data: bytes | None = None) -> str:
                return run(["git", *args], cwd=self.root, env=env, data=data).decode().strip()

            command("read-tree", "--empty")
            for name, content in sorted(files.items()):
                blob = command("hash-object", "-w", "--stdin", data=content)
                command("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
            tree = command("write-tree")
            return command("commit-tree", tree, data=b"Generated Helm repository snapshot\n")
