# MainAlto source cycle/1: composition only. Git/gh effects remain in
# LocalExecution providers behind pinned existing facades. Never retries effects.
[CmdletBinding()]
param(
    [ValidateSet('Inspect','Execute')][string]$Mode='Inspect',
    [Parameter(Mandatory)][string]$ManifestPath,
    [Parameter(Mandatory)][string]$CheckoutPath,
    [Parameter(Mandatory)][string]$ProviderRoot,
    [switch]$AllowCreateRemoteBranch,
    [string]$Python='python'
)
$ErrorActionPreference='Stop'
$toolsRoot=Split-Path -Parent $PSScriptRoot
$schema='mainalto.git-source-cycle/1'
$result=[ordered]@{
    schema_version=$schema
    observation_id=[guid]::NewGuid().ToString()
    observed_at=[DateTime]::UtcNow.ToString('o')
    mode=$Mode;operation_id=$null;manifest_sha256=$null
    repository=$null;branch=$null;base_head=$null;commit_head=$null
    plan=$null;commit=$null;publish=$null;remote_readback=$null
    commit_invoked=$false;publish_invoked=$false
    attempted_commit=$false;attempted_push=$false
    status='UNKNOWN';reason_code='NOT_STARTED';next_action='RECONCILE_READ_ONLY'
}

function Invoke-Child([string]$File,[string[]]$ChildArguments,[string]$ExpectedSchema) {
    $pwsh=(Get-Command pwsh -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $lines=@(& $pwsh -NoProfile -File $File @ChildArguments)
    $code=$LASTEXITCODE
    if ($lines.Count -ne 1) {throw 'CHILD_STDOUT_NOT_EXACTLY_ONE_JSON'}
    $report=ConvertFrom-Json -InputObject $lines[0] -AsHashtable -ErrorAction Stop
    if ($report.schema_version -ne $ExpectedSchema -or
        $report.status -notin @('READY','COMMITTED','VERIFIED','BLOCKED','UNKNOWN')) {
        throw 'CHILD_REPORT_INVALID'
    }
    return @{ExitCode=$code;Report=$report}
}

function Read-Head([string]$Path) {
    $lines=@(& git -C $Path rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or $lines.Count -ne 1 -or
        $lines[0] -notmatch '^[0-9a-f]{40}$') {throw 'LOCAL_HEAD_READ_FAILED'}
    return [string]$lines[0]
}

function Invoke-CycleBody {
    $lockPath=Join-Path $toolsRoot 'config/git-source-v1.release.json'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {throw 'RELEASE_LOCK_MISSING'}
    $lock=Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json -AsHashtable -ErrorAction Stop
    if ($lock.schema_version -ne 'mainalto.git-source-release/1' -or
        $lock.status -ne 'INTEGRATED_ON_DEMAND') {throw 'RELEASE_LOCK_INVALID'}
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {throw 'MANIFEST_NOT_FOUND'}
    $manifest=Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json -AsHashtable -ErrorAction Stop
    if ($manifest.schema_version -ne 'mainalto.git-change-set/1' -or
        $manifest.operation_id -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]{7,99}$' -or
        $manifest.repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or
        $manifest.branch -notmatch '^[A-Za-z0-9_./-]{1,160}$' -or
        $manifest.base_head -notmatch '^[0-9a-f]{40}$') {throw 'MANIFEST_CORRELATION_INVALID'}
    $result.operation_id=$manifest.operation_id
    $result.manifest_sha256=(Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $result.repository=$manifest.repository
    $result.branch=$manifest.branch
    $result.base_head=$manifest.base_head

    $changesFacade=Join-Path $toolsRoot 'tools/Invoke-MainAltoGitChanges.ps1'
    $publishFacade=Join-Path $toolsRoot 'tools/Invoke-MainAltoGitPublish.ps1'
    $changeHash=$lock['provider']['files_sha256']['tools/mainalto_git_changes_provider.py']
    $publishHash=$lock['provider']['files_sha256']['tools/mainalto_git_publish_provider.py']
    if ($changeHash -notmatch '^[0-9a-f]{64}$' -or $publishHash -notmatch '^[0-9a-f]{64}$') {
        throw 'PROVIDER_PINS_INVALID'
    }
    $changeArgs=@('-ManifestPath',$ManifestPath,'-CheckoutPath',$CheckoutPath,
        '-ProviderRoot',$ProviderRoot,'-ExpectedProviderSha256',$changeHash,'-Python',$Python)
    $plan=Invoke-Child $changesFacade (@('-Mode','Plan')+$changeArgs) 'mainalto.git-change-observation/1'
    $result.plan=$plan.Report
    if ($plan.ExitCode -ne 0 -or $plan.Report.status -ne 'READY' -or
        $plan.Report.operation_id -cne $manifest.operation_id -or
        $plan.Report.repository -cne $manifest.repository -or
        $plan.Report.branch -cne $manifest.branch -or
        $plan.Report.base_head -cne $manifest.base_head -or
        $plan.Report.attempted_commit -ne $false) {
        $result.status='BLOCKED';$result.reason_code='PLAN_PREFLIGHT_NOT_VERIFIED'
        $result.next_action='REVIEW_PLAN';return
    }
    if ($Mode -eq 'Inspect') {
        $result.status='READY';$result.reason_code='PLAN_VERIFIED_NO_EFFECT'
        $result.next_action='EXPLICIT_EXECUTE';return
    }
    if ((Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $result.manifest_sha256) {
        $result.status='BLOCKED';$result.reason_code='MANIFEST_CHANGED_AFTER_PLAN'
        $result.next_action='FIX_PREFLIGHT';return
    }
    if ((Read-Head $CheckoutPath) -ne $manifest.base_head) {throw 'BASE_CHANGED_AFTER_PLAN'}
    $result.commit_invoked=$true
    $result.attempted_commit=$null
    $commit=Invoke-Child $changesFacade (@('-Mode','Commit')+$changeArgs) 'mainalto.git-change-observation/1'
    $result.commit=$commit.Report
    $result.attempted_commit=$commit.Report.attempted_commit
    if ($commit.ExitCode -ne 0 -or $commit.Report.status -ne 'COMMITTED' -or
        $commit.Report.operation_id -cne $manifest.operation_id -or
        $commit.Report.repository -cne $manifest.repository -or
        $commit.Report.branch -cne $manifest.branch -or
        $commit.Report.base_head -cne $manifest.base_head -or
        $commit.Report.new_head -notmatch '^[0-9a-f]{40}$') {
        $result.status='UNKNOWN';$result.reason_code='LOCAL_COMMIT_OUTCOME_UNPROVEN'
        $result.next_action='RECONCILE_LOCAL_HEAD_READ_ONLY';return
    }
    $result.commit_head=$commit.Report.new_head
    if ((Read-Head $CheckoutPath) -ne $result.commit_head) {
        $result.status='UNKNOWN';$result.reason_code='LOCAL_COMMIT_HEAD_CHANGED'
        $result.next_action='RECONCILE_LOCAL_HEAD_READ_ONLY';return
    }
    if ((Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $result.manifest_sha256) {
        $result.status='UNKNOWN';$result.reason_code='MANIFEST_CHANGED_AFTER_LOCAL_COMMIT_NO_PUBLISH'
        $result.next_action='RECONCILE_LOCAL_HEAD_READ_ONLY';return
    }

    $pubArgs=@('-CheckoutPath',$CheckoutPath,'-Repository',$manifest.repository,
        '-Branch',$manifest.branch,'-ExpectedHead',$result.commit_head,
        '-ProviderRoot',$ProviderRoot,'-ExpectedProviderSha256',$publishHash,'-Python',$Python)
    if ($AllowCreateRemoteBranch) {$pubArgs+=@('-AllowCreateBranch')}
    $result.publish_invoked=$true
    $result.attempted_push=$null
    $published=Invoke-Child $publishFacade (@('-Mode','Publish')+$pubArgs) 'mainalto.git-publish-observation/1'
    $result.publish=$published.Report
    $result.attempted_push=$published.Report.attempted_push
    if ($published.ExitCode -ne 0 -or $published.Report.status -ne 'VERIFIED' -or
        $published.Report.repository -cne $manifest.repository -or
        $published.Report.branch -cne $manifest.branch -or
        $published.Report.expected_head -cne $result.commit_head -or
        $published.Report.remote_head -cne $result.commit_head) {
        $result.status='UNKNOWN';$result.reason_code='PUBLISH_NOT_VERIFIED_NO_RETRY'
        $result.next_action='RECONCILE_REMOTE_REF_READ_ONLY';return
    }
    $remote=Invoke-Child $publishFacade (@('-Mode','Inspect')+$pubArgs) 'mainalto.git-publish-observation/1'
    $result.remote_readback=$remote.Report
    if ($remote.ExitCode -ne 0 -or $remote.Report.status -ne 'VERIFIED' -or
        $remote.Report.repository -cne $manifest.repository -or
        $remote.Report.branch -cne $manifest.branch -or
        $remote.Report.expected_head -cne $result.commit_head -or
        $remote.Report.remote_head -cne $result.commit_head -or
        $remote.Report.attempted_push -ne $false) {
        $result.status='UNKNOWN';$result.reason_code='REMOTE_READBACK_UNPROVEN_NO_RETRY'
        $result.next_action='RECONCILE_REMOTE_REF_READ_ONLY';return
    }
    $result.status='VERIFIED';$result.reason_code='CORRELATED_LOCAL_COMMIT_AND_REMOTE_REF_VERIFIED'
    $result.next_action='NONE'
}

try {
    Invoke-CycleBody
} catch {
    $result.status='UNKNOWN';$result.reason_code='CYCLE_INVOCATION_OR_STATE_UNCERTAIN'
    if (-not $result.commit_invoked) {$result.status='BLOCKED';$result.reason_code='CYCLE_PREFLIGHT_FAILED'}
    $result.next_action=if ($result.publish_invoked) {'RECONCILE_REMOTE_REF_READ_ONLY'} elseif ($result.commit_invoked) {'RECONCILE_LOCAL_HEAD_READ_ONLY'} else {'FIX_PREFLIGHT'}
} finally {
    $result.observed_at=[DateTime]::UtcNow.ToString('o')
    $json=ConvertTo-Json -InputObject $result -Depth 40 -Compress
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes($json+"`n")
    $stdout=[Console]::OpenStandardOutput()
    $stdout.Write($bytes,0,$bytes.Length)
    $stdout.Flush()
}
switch ($result.status) {
    {$_ -in @('READY','VERIFIED')} {exit 0}
    'UNKNOWN' {exit 3}
    default {exit 2}
}
