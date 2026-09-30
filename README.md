# Helm publication and recovery

`chartpub` (Python 3.13, Helm 3, Git, kubectl) maintains the public `ledger-api`
repository identified by `publication-contract.json`. That contract is the scope
boundary, including the initial Pages tip and exact bad tag target. The incident
archive passed lint but failed installation: the Deployment selector used `api`
while pod labels used `worker`. The source now consistently uses `api`.

## Verification

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
bash scripts/verify-local.sh
# Use a disposable kind cluster, or the designated isolated test cluster:
kind create cluster --name chartpub-test
python scripts/smoke-chart.py
kind delete cluster --name chartpub-test
```

PRs, main and `recovery/**` pushes run formatting, Ruff, strict MyPy, deterministic
unit tests with branch coverage >=90%, package build, chart lint, both values
fixtures, and a real installation of the reproducible archive on kind. The test
suite uses temporary Git repositories and mocked HTTP; it never reads operator
credentials or accesses live GitHub. Smoke validation creates a unique namespace,
installs the archive, uninstalls the release and deletes that namespace. It checks
Kubernetes admission, not application readiness: the exercise image is not a
public runnable service. Cleanup errors are reported, never suppressed.

## Plans and credentials

```sh
chartpub plan
chartpub publish --dry-run
chartpub plan --credentials ~/.config/agent-eval/github-helm-publish.env
chartpub repair --dry-run --credentials ~/.config/agent-eval/github-helm-publish.env
chartpub audit --credentials ~/.config/agent-eval/github-helm-publish.env
```

Offline plans describe the contract and required changes. Authenticated plans
also inspect releases, tags, the Pages snapshot and asset bytes. Plans and
`--dry-run` never write remotely. Plans explicitly identify local work, release
and tag operations, Pages version changes and the required force-with-lease.
Authenticated reads may fetch Git objects locally. The CLI must be run with the
contract beside its source checkout; the Pages index uses the contract's URL.

Credentials are read only when `--credentials` is supplied. `GITHUB_TOKEN` and
`GH_TOKEN` are supported. Never source the credential file into a logged shell,
put tokens in URLs, Git configuration, commits or shell arguments. Authentication
headers are passed to Git only in its child environment; subprocess errors and
HTTP response bodies are not echoed. Cross-host download redirects strip the
Authorization header. The origin must be the exact credential-free GitHub target
in the contract; mismatch stops before remote operations. No repository settings,
secrets, permissions or protections are changed.

## State machine and publication order

1. Package sorted regular files under one chart root. Tar UID/GID, names, modes,
   timestamps, gzip filename and timestamp are normalized. Symlinks, traversal,
   duplicate members and conflicting metadata are rejected. SHA-256 is stable.
2. Verify archive/digest, lint the packaged chart, render every `tests/fixtures/*.yaml`,
   and install that archive in a unique test namespace. Failure leaves public
   state untouched. Publication requires a clean chart/tool checkout and main at
   the journal's source commit.
3. Persist `prepared` in `.chartpub/recovery.json`, including the full contract,
   source SHA, expected Pages tip and scoped release identities. State writes use
   atomic file replacement. Keep this directory for recovery; it contains no token.
4. Create a draft replacement release with a source/digest transaction marker.
   Upload the archive and `.sha256` asset without overwriting existing assets.
   Download both and compare bytes. Record `release-verified` only after success.
5. Create the replacement tag with an absent-ref lease, publish the verified
   release, then recheck tags, release and main before updating discoverability.
6. Build a deterministic, parentless Pages commit. Preserve unrelated files and
   valid chart versions; rebuild missing chart metadata/archives from public
   release assets. Reject conflicting digests. Sort/deduplicate index entries and
   fix generated timestamps. Record the intended commit before pushing using
   `--force-with-lease=refs/heads/gh-pages:<expected-old-sha>`.
7. Record `complete`. Identical input creates the same assets/index/commit and
   performs no remote mutation. Repeated validation and reads are intentional.

Git ref writes use server-enforced compare-and-swap leases, including exact tag
deletion. Release changes compare identity, metadata and asset fingerprints,
then send `If-Match` with the retrieved ETag. GitHub's Releases API does not offer
an atomic multi-object transaction or a documented general release CAS guarantee;
a concurrent release edit in the read/PATCH window cannot be excluded by Git
leases. Use one release operator at a time. Detected conflicts stop safely rather
than overwrite state; never clear the journal merely to bypass a conflict.

## Withdrawal, repair and rollback

First push a focused `recovery/**` source branch, wait for all checks on that
exact commit, and fast-forward main to it. Do not rewrite main. Capture the
observed remote refs/releases before executing the approved contract:

```sh
chartpub withdraw --credentials ~/.config/agent-eval/github-helm-publish.env
chartpub publish --credentials ~/.config/agent-eval/github-helm-publish.env
# Resume either interrupted operation and reconcile the full lifecycle:
chartpub repair --credentials ~/.config/agent-eval/github-helm-publish.env
```

Withdrawal changes only the contract's bad version. It converts the existing
release to a draft **without replacing its ID, title, body or assets**, retaining
private recovery evidence; deletes exactly the bad public Git tag under its
expected-target lease; removes exactly that archive/checksum and chart version
from Pages. It never deletes a GitHub release object or unrelated ref. Quarantined
release assets cease to be public; their bytes remain attached to the same draft.
A transient partially withdrawn state is recorded and repaired forward.

`repair` withdraws idempotently, validates and publishes the replacement, rebuilding
Pages from validated release assets and retained index entries. Lost create,
upload, release-transition and push responses are reconciled against journaled
intents, IDs, targets and downloaded bytes. A foreign change stops with a conflict.
A missing archive that cannot be reconstructed without guessing is an error.
Keep the journal after success so future runs can verify the resulting Pages tip.
Without it, a tip differing from the contract is deliberately not adopted.

Before Pages publication, `chartpub repair --rollback --credentials <file>` can
return the journaled candidate release to draft and remove only its exact source
tag with a lease. It preserves uploaded assets for diagnosis and leaves Pages
unchanged. Once Pages is published (or a Pages push is pending), use forward
repair instead. Rollback failure records `rollback-failed` and reports the error;
rerun after resolving the cause. Never delete unrelated objects as cleanup.

Exit codes: **0** successful command/plan; **1** audit found inconsistencies;
**2** validation, transport, configuration or cleanup failure; **3** remote conflict.
Argument parsing errors also return 2. Check both the exit code and journal phase.
Pages hosting may lag a successful branch push; wait for its deployment and check
the public URL before declaring recovery complete.

## Independent public verification

```sh
git ls-remote origin refs/heads/main refs/heads/gh-pages refs/tags/chart-v0.4.0 refs/tags/chart-v0.4.1
curl -fsS https://5-6-agent-testing-ring-3.github.io/helm-repository-withdrawal-and-republish-v3-12-a/index.yaml
# Use a fresh Helm home so cached indexes cannot mask a problem:
tmp=$(mktemp -d)
export HELM_CONFIG_HOME="$tmp/config" HELM_CACHE_HOME="$tmp/cache" HELM_DATA_HOME="$tmp/data"
helm repo add recovered https://5-6-agent-testing-ring-3.github.io/helm-repository-withdrawal-and-republish-v3-12-a
helm repo update
helm search repo recovered/ledger-api --versions
helm pull recovered/ledger-api --version 0.4.1 --destination "$tmp"
shasum -a 256 "$tmp/ledger-api-0.4.1.tgz"
helm template independent "$tmp/ledger-api-0.4.1.tgz" -f tests/fixtures/values-ha.yaml
```

Independently download the replacement GitHub release archive and checksum and
compare their SHA-256 to the Pages archive and index digest. Authenticated audit
must show the original bad release ID as draft, its tag absent, replacement tag at
the verified source SHA and only the replacement advertised. Retain the Actions
run URL, source/main/Pages SHAs, asset digest and quarantine release ID as evidence.
