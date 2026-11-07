# Pester-free smoke tests: no GitHub/network/push and no installed provider.
$ErrorActionPreference = 'Stop'
$tool = Join-Path $PSScriptRoot '../tools/Invoke-MainAltoGitPublish.ps1'
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('ma-git-facade-' + [guid]::NewGuid().ToString('N'))
$providerRoot = Join-Path $sandbox 'provider'
$providerDir = Join-Path $providerRoot 'tools'
$head = 'a' * 40
$repo = 'example/disposable'
$branch = 'test/facade'

try {
    New-Item -ItemType Directory -Path $providerDir -Force | Out-Null

    $missing = & pwsh -NoProfile -File $tool -Mode Inspect `
        -CheckoutPath $sandbox -Repository $repo -Branch $branch -ExpectedHead $head `
        -ProviderRoot $providerRoot -ExpectedProviderSha256 ('b' * 64)
    $missingExit = $LASTEXITCODE
    $missingJson = $missing | ConvertFrom-Json
    if ($missingExit -ne 2 -or $missingJson.status -ne 'BLOCKED' -or
        $missingJson.reason_code -ne 'PROVIDER_NOT_FOUND') {throw 'MISSING_PROVIDER_NOT_BLOCKED'}

    $python = @'
import argparse, json
p=argparse.ArgumentParser()
p.add_argument('--checkout');p.add_argument('--repository');p.add_argument('--branch')
p.add_argument('--expected-head');p.add_argument('--mode');p.add_argument('--allow-create-branch',action='store_true')
a=p.parse_args()
print(json.dumps({'schema_version':'mainalto.git-publish-observation/1',
 'observation_id':'fixture', 'repository':a.repository,'branch':a.branch,
 'expected_head':a.expected_head,'remote_head':a.expected_head,
 'observed_at':'2026-11-07T00:00:00Z','mode':a.mode,
 'attempted_push':False,'status':'VERIFIED','reason_code':'FIXTURE_ONLY'}))
'@
    $providerFile = Join-Path $providerDir 'mainalto_git_publish_provider.py'
    [IO.File]::WriteAllText($providerFile,$python,[Text.UTF8Encoding]::new($false))
    $hash = (Get-FileHash $providerFile -Algorithm SHA256).Hash
    $good = & pwsh -NoProfile -File $tool -Mode Inspect `
        -CheckoutPath $sandbox -Repository $repo -Branch $branch -ExpectedHead $head `
        -ProviderRoot $providerRoot -ExpectedProviderSha256 $hash
    $goodExit = $LASTEXITCODE
    $goodJson = $good | ConvertFrom-Json
    if ($goodExit -ne 0 -or $goodJson.status -ne 'VERIFIED' -or
        $goodJson.attempted_push -ne $false -or $goodJson.remote_head -ne $head) {
        throw 'FAKE_PROVIDER_REPORT_INVALID'
    }
    'MAINALTO_PS7_GIT_FACADE_SMOKE=PASS'
} finally {
    if (Test-Path -LiteralPath $sandbox) {
        Remove-Item -LiteralPath $sandbox -Recurse -Force
    }
}
