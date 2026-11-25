# gosha-release-control
## Gosha release control

This repository contains release controls and public metadata only. It never checks out
the private application, fetches its git objects, or publishes private CI logs or artifacts.

The default-branch **Gosha release control** workflow accepts a full SHA and `test` or
`release`. It validates the successful unit-test step of the private repository's
push workflow for that exact SHA. A release additionally requires the owner's approval
for the current workflow run in `gosha-production`.

Two main-only GitHub Environments keep credentials separate:

- `gosha-ci-read` holds `GOSHA_PRIVATE_CI_TOKEN`, scoped to Actions read on the private repository.
- `gosha-production` holds the dedicated Ed25519 key in `GOSHA_RELEASE_SIGNING_KEY`.
  Keep Required reviewers, owner approval and branch protection enabled.

The public repository variable `GOSHA_APPROVER_SSH_PUBLIC_KEY` contains only the
public key in OpenSSH format without the comment. Never add private key material,
account credentials or application data to repository files or workflow inputs.

An approved run atomically updates the standalone metadata branch `release/gosha`.
It contains one `current.json` file with a versioned manifest and detached
OpenSSH signature under namespace `gosha-release-v1`. Signed fields are the
private repository name and immutable repository ID, the commit SHA, the public
workflow run ID and its increasing sequence. Approved SHAs and this metadata
are intentionally public. Application code and operational data remain private.
