# MainAlto v1 candidate facade: LocalExecution alone edits files and commits.
[CmdletBinding()]
param(
    [ValidateSet('Plan','Commit')][string]$Mode='Plan',
    [Parameter(Mandatory)][string]$ManifestPath,
    [Parameter(Mandatory)][string]$CheckoutPath,
    [Parameter(Mandatory)][string]$ProviderRoot,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ExpectedProviderSha256,
    [string]$Python='python'
)
$ErrorActionPreference='Stop'

function New-Result([string]$Reason,[string]$Status) {
    return [ordered]@{
        schema_version='mainalto.git-change-observation/1'
        observation_id=[guid]::NewGuid().ToString()
        operation_id=$null;repository=$null;branch=$null;base_head=$null
        new_head=$null;changed_paths=$null;mode=$Mode
        observed_at=[DateTime]::UtcNow.ToString('o')
        attempted_commit=$null;status=$Status;reason_code=$Reason
        next_action='RECONCILE_READ_ONLY'
    }
}

try {
    $provider=Join-Path $ProviderRoot 'tools/mainalto_git_changes_provider.py'
    if (-not (Test-Path -LiteralPath $provider -PathType Leaf)) {
        $report=New-Result 'PROVIDER_NOT_FOUND' 'BLOCKED'
        $report.attempted_commit=$false
    } elseif ((Get-FileHash -LiteralPath $provider -Algorithm SHA256).Hash -ne $ExpectedProviderSha256) {
        $report=New-Result 'PROVIDER_HASH_MISMATCH' 'BLOCKED'
        $report.attempted_commit=$false
    } elseif (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        $report=New-Result 'MANIFEST_NOT_FOUND' 'BLOCKED'
        $report.attempted_commit=$false
    } else {
        $exe=Get-Command $Python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $exe) {
            $report=New-Result 'PYTHON_NOT_FOUND' 'BLOCKED'
            $report.attempted_commit=$false
        } else {
            $start=[Diagnostics.ProcessStartInfo]::new()
            $start.FileName=$exe.Source
            $start.UseShellExecute=$false
            $start.CreateNoWindow=$true
            $start.RedirectStandardOutput=$true
            $start.RedirectStandardError=$true
            $start.StandardOutputEncoding=[Text.UTF8Encoding]::new($false)
            $start.StandardErrorEncoding=[Text.UTF8Encoding]::new($false)
            $manifest=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ManifestPath)
            $checkout=$ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($CheckoutPath)
            foreach ($arg in @('-I','-B','-S',$provider,'--manifest',$manifest,'--checkout',$checkout,'--mode',$Mode)) {
                $start.ArgumentList.Add($arg)
            }
            $process=[Diagnostics.Process]::new()
            $process.StartInfo=$start
            try {
                [void]$process.Start()
                $readOut=$process.StandardOutput.ReadToEndAsync()
                $readErr=$process.StandardError.ReadToEndAsync()
                if (-not $process.WaitForExit(150000)) {
                    $process.Kill($true)
                    $report=New-Result 'PROVIDER_TIMEOUT_OUTCOME_UNKNOWN' 'UNKNOWN'
                } else {
                    $raw=$readOut.GetAwaiter().GetResult().Trim()
                    $parsed=ConvertFrom-Json -InputObject $raw -AsHashtable -ErrorAction Stop
                    if ($parsed.schema_version -ne 'mainalto.git-change-observation/1' -or
                        $parsed.mode -ne $Mode -or
                        $parsed.status -notin @('READY','COMMITTED','BLOCKED','UNKNOWN') -or
                        ($parsed.status -in @('READY','COMMITTED') -and $process.ExitCode -ne 0) -or
                        ($parsed.status -eq 'COMMITTED' -and
                          ($Mode -ne 'Commit' -or $parsed.new_head -notmatch '^[0-9a-f]{40}$'))) {
                        throw 'PROVIDER_REPORT_INVALID'
                    }
                    $report=$parsed
                }
            } finally {$process.Dispose()}
        }
    }
} catch {
    $report=New-Result 'FACADE_INVOCATION_FAILED' 'UNKNOWN'
}
$json=ConvertTo-Json -InputObject $report -Depth 30 -Compress
$bytes=[Text.UTF8Encoding]::new($false).GetBytes($json+"`n")
$stdout=[Console]::OpenStandardOutput()
$stdout.Write($bytes,0,$bytes.Length)
$stdout.Flush()
switch ($report.status) {
    {$_ -in @('READY','COMMITTED')} {exit 0}
    'UNKNOWN' {exit 3}
    default {exit 2}
}
