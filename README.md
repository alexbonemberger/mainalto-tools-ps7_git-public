# MainAlto Tools — Public PS7 → Git

Public, portable source for the approved **PowerShell 7 → Git** operations capability. GitHub Actions **Git event → runner → PS7** is used only for offline tests; it does not control the user's Windows notebook.

## Included

- PowerShell facades: repository inspection/private creation, bounded change-set Plan/Commit, one-shot Publish/Inspect, correlated Plan → Commit → Publish → remote readback.
- Python 3 providers implementing Git/GitHub effects, with explicit identities, preimages and SHA-256 pins.
- Offline Python/PS7 tests for Windows and Ubuntu.
- `config/git-source-v1.release.json` pins the exact provider bytes.

## Explicitly excluded

No MainAlto runtime, Chess, UIA, worker, scheduler, STOP, authority/grant, private queue, credentials, user profile, API keys, installed local binding or historical receipts. Private MainAlto bindings and integration policy remain in their owning repositories.

## Prerequisites and operation

PowerShell 7, Python 3.12+ and Git. Repository creation also requires an already authenticated GitHub CLI (`gh`) and **only creates private repositories**, never public ones. No CLI acquires credentials.

A source cycle uses a pre-existing, clean Git checkout with `origin` matching the manifest's GitHub repository and explicit author config. First inspect:

```powershell
pwsh -NoProfile -File ./tools/Invoke-MainAltoGitSourceCycle.ps1 `
  -Mode Inspect -ManifestPath ./my-change-set.json `
  -CheckoutPath ./my-checkout -ProviderRoot .
```

`Inspect` does not publish. `-Mode Execute` performs a **separately authorized, single-attempt** local commit and remote push; it must not be invoked by an unattended CI workflow. After uncertain effects, reconcile by read-only inspection, **never automatically repeat** a push.

See `docs/PUBLIC-SOURCE-BOUNDARY.md` for the migration boundary and existing MainAlto integration.

## Validation

```powershell
python -B -m unittest discover -s tests -p "test_mainalto_*_provider.py" -v
pwsh -NoProfile -File tests/Test-GitPublishFacade.ps1
pwsh -NoProfile -File tests/Test-GitSourceFacades.ps1
pwsh -NoProfile -File tests/Test-GitSourceCycle.ps1
```

All CI effects are confined to disposable local fixtures; no public CI credentials or remote pushes.

**Status:** public source candidate pending cross-platform CI and controlled adoption by MainAlto. Publication here is not evidence of local installation or LIVE activation.
