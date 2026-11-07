# MainAlto tools facade: no Git writes, no ledger, no retry in this layer.
# Requires a pinned, separately installed LocalExecution native provider.
[CmdletBinding()]
param(
    [ValidateSet('Inspect','Publish')][string]$Mode='Inspect',
    [Parameter(Mandatory)][string]$CheckoutPath,
    [Parameter(Mandatory)][string]$Repository,
    [Parameter(Mandatory)][string]$Branch,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedHead,
    [Parameter(Mandatory)][string]$ProviderRoot,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ExpectedProviderSha256,
    [switch]$AllowCreateBranch,
    [string]$Python='python'
)
$ErrorActionPreference='Stop'

function New-Failure([string]$Reason,[string]$Status) {
    return [ordered]@{
        schema_version='mainalto.git-publish-observation/1'
        observation_id=[guid]::NewGuid().ToString()
        repository=$Repository;branch=$Branch;expected_head=$ExpectedHead
        remote_head=$null;observed_at=[DateTime]::UtcNow.ToString('o')
        mode=$Mode;attempted_push=$false;status=$Status;reason_code=$Reason
    }
}

try {
    $provider = Join-Path $ProviderRoot 'tools/mainalto_git_publish_provider.py'
    if (-not (Test-Path -LiteralPath $provider -PathType Leaf)) {
        $report=New-Failure 'PROVIDER_NOT_FOUND' 'BLOCKED'
    } elseif ((Get-FileHash -LiteralPath $provider -Algorithm SHA256).Hash -ne $ExpectedProviderSha256) {
        $report=New-Failure 'PROVIDER_HASH_MISMATCH' 'BLOCKED'
    } else {
        $pythonExe=Get-Command $Python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $pythonExe) {
            $report=New-Failure 'PYTHON_NOT_FOUND' 'BLOCKED'
        } else {
            $start=[Diagnostics.ProcessStartInfo]::new()
            $start.FileName=$pythonExe.Source
            $start.UseShellExecute=$false
            $start.CreateNoWindow=$true
            $start.RedirectStandardOutput=$true
            $start.RedirectStandardError=$true
            $start.StandardOutputEncoding=[Text.UTF8Encoding]::new($false)
            $start.StandardErrorEncoding=[Text.UTF8Encoding]::new($false)
            $resolved=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($CheckoutPath)
            $argv=@('-I','-B','-S',$provider,'--checkout',$resolved,
                    '--repository',$Repository,'--branch',$Branch,
                    '--expected-head',$ExpectedHead,'--mode',$Mode)
            foreach ($arg in $argv) {$start.ArgumentList.Add($arg)}
            if ($AllowCreateBranch) {$start.ArgumentList.Add('--allow-create-branch')}
            $process=[Diagnostics.Process]::new()
            $process.StartInfo=$start
            try {
                [void]$process.Start()
                $readOut=$process.StandardOutput.ReadToEndAsync()
                $readErr=$process.StandardError.ReadToEndAsync()
                if (-not $process.WaitForExit(150000)) {
                    $process.Kill($true)
                    $report=New-Failure 'PROVIDER_TIMEOUT_OUTCOME_UNKNOWN' 'UNKNOWN'
                    if ($Mode -eq 'Publish') {$report.attempted_push=$null}
                } else {
                    $raw=$readOut.GetAwaiter().GetResult().Trim()
                    # One JSON object is the complete provider protocol.
                    $parsed=ConvertFrom-Json -InputObject $raw -AsHashtable -ErrorAction Stop
                    if ($parsed.schema_version -ne 'mainalto.git-publish-observation/1' -or
                        $parsed.status -notin @('READY','VERIFIED','BLOCKED','UNKNOWN') -or
                        $parsed.repository -ne $Repository -or $parsed.branch -ne $Branch -or
                        $parsed.expected_head -ne $ExpectedHead) {
                        throw 'PROVIDER_REPORT_INVALID'
                    }
                    $report=$parsed
                }
            } finally {$process.Dispose()}
        }
    }
} catch {
    $report=New-Failure 'FACADE_INVOCATION_FAILED' 'UNKNOWN'
    if ($Mode -eq 'Publish') {$report.attempted_push=$null}
}
$json=ConvertTo-Json -InputObject $report -Depth 30 -Compress
$bytes=[Text.UTF8Encoding]::new($false).GetBytes($json+"`n")
$stdout=[Console]::OpenStandardOutput()
$stdout.Write($bytes,0,$bytes.Length)
$stdout.Flush()
switch ($report.status) {
    'READY' {exit 0}
    'VERIFIED' {exit 0}
    'UNKNOWN' {exit 3}
    default {exit 2}
}
