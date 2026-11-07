# Public source boundary — PS7 → Git

This repository contains the reusable **source operations** layer formerly kept alongside the private MainAlto implementation. No private repository has been modified by this export.

| Public portable components | Private MainAlto-owned components |
| --- | --- |
| `tools/Invoke-MainAltoGitChanges.ps1`, `Invoke-MainAltoGitPublish.ps1`, `Invoke-MainAltoGitSourceCycle.ps1` | `Invoke-MainAltoGitSourceBound.ps1`: machine-specific binding, policy and local MainRoot |
| `tools/Invoke-MainAltoRepoCreate.ps1` (private-create behavior only) | Installation/activation, authority and grant policies |
| Three corresponding Python providers | Notebook runtime paths, workers, queues, receipts, Chess and UIA |
| Offline tests and immutable SHA-256 provider pins | Private repository catalog references and pinned integration manifests |

## Contracts and compatibility

- Git change manifest: `mainalto.git-change-set/1`.
- Change report: `mainalto.git-change-observation/1`.
- Publish report: `mainalto.git-publish-observation/1`.
- Correlated source cycle: `mainalto.git-source-cycle/1`.
- Release metadata: `mainalto.git-source-release/1`.

The source-cycle wrapper still requires explicit `-ManifestPath`, `-CheckoutPath`, and `-ProviderRoot`. It performs no work without valid preconditions, and every remote effect requires a separate `Execute` request. The existing private binding/pins are **not automatically migrated**. No LIVE activation is implied.

## Validation limits

Public CI performs isolated tests using disposable repositories and fake/local remotes. It cannot prove authenticated publishing to a private remote, notebook installation, real UI automation, or the MainAlto worker lifecycle. Those require separately authorized integration evidence.

## Provenance

The initial portable files were exported from already integrated MainAlto source operations. Only the source-cycle child PowerShell executable resolution and cross-platform smoke tests were adjusted for public Windows/Ubuntu CI. The three provider hashes match the previously approved integrated source baseline. Private file paths, runtime manifests and local-binding catalog are intentionally absent.

No open-source license is granted merely by making the source publicly readable; licensing remains a separate owner decision.
