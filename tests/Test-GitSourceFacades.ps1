# Windows disposable smoke test: the only effects are files under RUNNER_TEMP.
$ErrorActionPreference='Stop'
$toolsRoot=Split-Path -Parent $PSScriptRoot
$pwsh=(Get-Command pwsh -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$tmpRoot=Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString('N'))
$providerTools=Join-Path $tmpRoot 'provider/tools'
New-Item -Path $providerTools -ItemType Directory -Force | Out-Null
try {
    $repoFacade=Join-Path $toolsRoot 'tools/Invoke-MainAltoRepoCreate.ps1'
    $changesFacade=Join-Path $toolsRoot 'tools/Invoke-MainAltoGitChanges.ps1'
    $zero=('0' * 64)
    $common=@('-ProviderRoot',(Join-Path $tmpRoot 'provider'),'-ExpectedProviderSha256',$zero)
    $raw=& $pwsh -NoProfile -File $repoFacade -Mode Inspect -Repository 'example/fake-private' @common
    $code=$LASTEXITCODE
    $report=$raw | ConvertFrom-Json
    if ($code -ne 2 -or $report.status -ne 'BLOCKED' -or $report.reason_code -ne 'PROVIDER_NOT_FOUND') {
        throw 'REPO_MISSING_PROVIDER_CONTRACT_FAILED'
    }
    $raw=& $pwsh -NoProfile -File $repoFacade -Mode Create -Repository 'example/fake-private' @common
    $code=$LASTEXITCODE
    $report=$raw | ConvertFrom-Json
    if ($code -ne 2 -or $report.reason_code -ne 'EXPLICIT_REPOSITORY_CONFIRMATION_REQUIRED') {
        throw 'REPO_CONFIRMATION_GATE_FAILED'
    }
    $fakeRepo=@'
import json, sys
mode=sys.argv[sys.argv.index('--mode')+1]
repository=sys.argv[sys.argv.index('--repository')+1]
print(json.dumps(dict(schema_version='mainalto.repo-create-observation/1', mode=mode,
  repository=repository,status='READY',visibility=None,attempted_create=False)))
'@
    $repoProvider=Join-Path $providerTools 'mainalto_repo_create_provider.py'
    [IO.File]::WriteAllText($repoProvider,$fakeRepo,[Text.UTF8Encoding]::new($false))
    $repoHash=(Get-FileHash -LiteralPath $repoProvider -Algorithm SHA256).Hash
    $raw=& $pwsh -NoProfile -File $repoFacade -Mode Inspect -Repository 'example/fake-private' `
        -ProviderRoot (Join-Path $tmpRoot 'provider') -ExpectedProviderSha256 $repoHash
    $code=$LASTEXITCODE
    $report=$raw | ConvertFrom-Json
    if ($code -ne 0 -or $report.status -ne 'READY') {throw 'REPO_FAKE_PROVIDER_CONTRACT_FAILED'}
    $raw=& $pwsh -NoProfile -File $repoFacade -Mode Inspect -Repository 'example/fake-private' @common
    $code=$LASTEXITCODE
    $report=$raw | ConvertFrom-Json
    if ($code -ne 2 -or $report.reason_code -ne 'PROVIDER_HASH_MISMATCH') {throw 'REPO_PROVIDER_PIN_GATE_FAILED'}

    $manifest=Join-Path $tmpRoot 'manifest.json'
    [IO.File]::WriteAllText($manifest,'{}',[Text.UTF8Encoding]::new($false))
    $raw=& $pwsh -NoProfile -File $changesFacade -Mode Plan -ManifestPath $manifest -CheckoutPath $tmpRoot @common
    $code=$LASTEXITCODE
    $report=$raw | ConvertFrom-Json
    if ($code -ne 2 -or $report.reason_code -ne 'PROVIDER_NOT_FOUND') {
        throw 'CHANGES_MISSING_PROVIDER_CONTRACT_FAILED'
    }
    $fakeChanges=@'
import json, sys
mode=sys.argv[sys.argv.index('--mode')+1]
print(json.dumps(dict(schema_version='mainalto.git-change-observation/1',mode=mode,
  status='READY',attempted_commit=False,new_head=None)))
'@
    $changesProvider=Join-Path $providerTools 'mainalto_git_changes_provider.py'
    [IO.File]::WriteAllText($changesProvider,$fakeChanges,[Text.UTF8Encoding]::new($false))
    $changesHash=(Get-FileHash -LiteralPath $changesProvider -Algorithm SHA256).Hash
    $raw=& $pwsh -NoProfile -File $changesFacade -Mode Plan -ManifestPath $manifest -CheckoutPath $tmpRoot `
        -ProviderRoot (Join-Path $tmpRoot 'provider') -ExpectedProviderSha256 $changesHash
    $code=$LASTEXITCODE
    $report=$raw | ConvertFrom-Json
    if ($code -ne 0 -or $report.status -ne 'READY') {throw 'CHANGES_FAKE_PROVIDER_CONTRACT_FAILED'}
    'MAINALTO_GIT_SOURCE_FACADE_SMOKE=PASS'
} finally {
    Remove-Item -LiteralPath $tmpRoot -Force -Recurse -ErrorAction SilentlyContinue
}
