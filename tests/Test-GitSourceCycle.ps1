# Windows disposable test of orchestration. Fake Python providers, zero network.
$ErrorActionPreference='Stop'
$tmp=Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString('N'))
$fakeTools=Join-Path $tmp 'tools-source'
$fakeProvider=Join-Path $tmp 'provider-source'
$checkout=Join-Path $tmp 'checkout'
$manifestFile=Join-Path $tmp 'change-set.json'
$pwsh=(Get-Command pwsh -CommandType Application -ErrorAction Stop).Source
$sourceRoot=Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Path (Join-Path $fakeTools 'tools') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $fakeTools 'config') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $fakeProvider 'tools') -Force | Out-Null
New-Item -ItemType Directory -Path $checkout -Force | Out-Null
try {
    foreach ($file in @('Invoke-MainAltoGitSourceCycle.ps1',
        'Invoke-MainAltoGitChanges.ps1','Invoke-MainAltoGitPublish.ps1')) {
        if ($file -eq 'Invoke-MainAltoGitPublish.ps1') {
            # This source is in main, not copied into this local scratch suite.
            $source=Join-Path $sourceRoot 'tools/Invoke-MainAltoGitPublish.ps1'
        } else {$source=Join-Path $sourceRoot ('tools/'+$file)}
        Copy-Item -LiteralPath $source -Destination (Join-Path $fakeTools ('tools/'+$file))
    }
    $fakeChanges=@'
import argparse,json,subprocess,uuid
p=argparse.ArgumentParser()
p.add_argument('--manifest');p.add_argument('--checkout');p.add_argument('--mode')
a=p.parse_args();m=json.load(open(a.manifest,encoding='utf-8'))
head=None
if a.mode=='Commit':
    subprocess.run(['git','-C',a.checkout,'-c','commit.gpgsign=false','commit','--allow-empty','--no-verify','-qm','cycle test'],check=True)
    head=subprocess.check_output(['git','-C',a.checkout,'rev-parse','HEAD'],text=True).strip()
o=dict(schema_version='mainalto.git-change-observation/1',observation_id=str(uuid.uuid4()),
       mode=a.mode,status='READY' if a.mode=='Plan' else 'COMMITTED',
       operation_id=m['operation_id'],repository=m['repository'],branch=m['branch'],
       base_head=m['base_head'],new_head=head,attempted_commit=(a.mode=='Commit'))
print(json.dumps(o))
'@
    $fakePublish=@'
import argparse,json,os,pathlib,uuid
p=argparse.ArgumentParser()
p.add_argument('--checkout');p.add_argument('--repository');p.add_argument('--branch')
p.add_argument('--expected-head');p.add_argument('--mode');p.add_argument('--allow-create-branch',action='store_true')
a=p.parse_args();mark=pathlib.Path(a.checkout)/'.git'/'cycle-remote-ref'
counter=pathlib.Path(a.checkout)/'.git'/'cycle-publish-count'
if a.mode=='Publish':
    n=int(counter.read_text() if counter.exists() else '0')+1
    counter.write_text(str(n))
    if os.environ.get('MAINALTO_CYCLE_TEST_UNCERTAIN')=='1':
        print(json.dumps(dict(schema_version='mainalto.git-publish-observation/1',
          observation_id=str(uuid.uuid4()),mode=a.mode,status='UNKNOWN',attempted_push=None,
          repository=a.repository,branch=a.branch,expected_head=a.expected_head,
          remote_head=None,reason_code='SIMULATED_UNCERTAIN')))
        raise SystemExit(3)
    mark.write_text(a.expected_head)
status='VERIFIED' if mark.exists() and mark.read_text()==a.expected_head else 'UNKNOWN'
o=dict(schema_version='mainalto.git-publish-observation/1',observation_id=str(uuid.uuid4()),
       mode=a.mode,status=status,attempted_push=(a.mode=='Publish'),
       repository=a.repository,branch=a.branch,expected_head=a.expected_head,
       remote_head=mark.read_text() if mark.exists() else None)
print(json.dumps(o))
'@
    $changesFile=Join-Path $fakeProvider 'tools/mainalto_git_changes_provider.py'
    $publishFile=Join-Path $fakeProvider 'tools/mainalto_git_publish_provider.py'
    [IO.File]::WriteAllText($changesFile,$fakeChanges,[Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($publishFile,$fakePublish,[Text.UTF8Encoding]::new($false))
    $pins=@{
        'tools/mainalto_git_changes_provider.py'=(Get-FileHash -LiteralPath $changesFile -Algorithm SHA256).Hash.ToLowerInvariant()
        'tools/mainalto_git_publish_provider.py'=(Get-FileHash -LiteralPath $publishFile -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $lock=@{schema_version='mainalto.git-source-release/1';status='INTEGRATED_ON_DEMAND';
        provider=@{files_sha256=$pins}}
    [IO.File]::WriteAllText((Join-Path $fakeTools 'config/git-source-v1.release.json'),
        (ConvertTo-Json -InputObject $lock -Depth 20),[Text.UTF8Encoding]::new($false))
    & git -C $checkout init -q -b 'work/validation'
    if ($LASTEXITCODE -ne 0) {throw 'GIT_INIT_FAILED'}
    & git -C $checkout config user.name 'MainAlto Test'
    & git -C $checkout config user.email 'mainalto@test.invalid'
    & git -C $checkout commit --allow-empty --no-gpg-sign -qm 'base'
    if ($LASTEXITCODE -ne 0) {throw 'GIT_BASE_FAILED'}
    $base=(& git -C $checkout rev-parse HEAD).Trim()

    function Write-Manifest([string]$Base,[string]$Id) {
        $manifest=@{schema_version='mainalto.git-change-set/1';operation_id=$Id
            repository='example/public-source';branch='work/validation';base_head=$Base
            commit_message='cycle test';changes=@(@{op='create';path='test.txt'
                expected_sha256=$null;content_base64='eA=='})}
        [IO.File]::WriteAllText($manifestFile,(ConvertTo-Json -InputObject $manifest -Depth 20),
            [Text.UTF8Encoding]::new($false))
    }
    $cycle=Join-Path $fakeTools 'tools/Invoke-MainAltoGitSourceCycle.ps1'
    $args=@('-ManifestPath',$manifestFile,'-CheckoutPath',$checkout,'-ProviderRoot',$fakeProvider)
    Write-Manifest $base 'source-test-0001'
    $raw=& $pwsh -NoProfile -File $cycle -Mode Inspect @args
    $code=$LASTEXITCODE
    $report=$raw | ConvertFrom-Json
    if ($code -ne 0 -or $report.status -ne 'READY' -or $report.commit_invoked -or
        ((& git -C $checkout rev-parse HEAD).Trim() -ne $base)) {throw 'PLAN_ONLY_CYCLE_FAILED'}

    $raw=& $pwsh -NoProfile -File $cycle -Mode Execute @args -AllowCreateRemoteBranch
    $code=$LASTEXITCODE
    $report=$raw | ConvertFrom-Json
    if ($code -ne 0 -or $report.status -ne 'VERIFIED' -or
        $report.operation_id -ne 'source-test-0001' -or
        $report.plan.status -ne 'READY' -or $report.commit.status -ne 'COMMITTED' -or
        $report.publish.status -ne 'VERIFIED' -or $report.remote_readback.status -ne 'VERIFIED' -or
        $report.commit_head -ne $report.remote_readback.remote_head) {
        throw 'CORRELATED_CYCLE_FAILED'
    }
    $newHead=(& git -C $checkout rev-parse HEAD).Trim()
    Write-Manifest $newHead 'source-test-0002'
    $env:MAINALTO_CYCLE_TEST_UNCERTAIN='1'
    $raw=& $pwsh -NoProfile -File $cycle -Mode Execute @args -AllowCreateRemoteBranch
    $code=$LASTEXITCODE
    $report=$raw | ConvertFrom-Json
    Remove-Item Env:MAINALTO_CYCLE_TEST_UNCERTAIN -ErrorAction SilentlyContinue
    $count=Get-Content -LiteralPath (Join-Path $checkout '.git/cycle-publish-count') -Raw
    if ($code -ne 3 -or $report.status -ne 'UNKNOWN' -or
        $report.next_action -ne 'RECONCILE_REMOTE_REF_READ_ONLY' -or
        [int]$count -ne 2) {throw 'UNKNOWN_PUBLISH_WAS_RETRIED_OR_MISCLASSIFIED'}
    # Do not leak the intentionally observed child exit 3 into the Actions pwsh wrapper.
    $global:LASTEXITCODE=0
    'MAINALTO_GIT_SOURCE_CYCLE_TEST=PASS'
} finally {
    Remove-Item Env:MAINALTO_CYCLE_TEST_UNCERTAIN -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
