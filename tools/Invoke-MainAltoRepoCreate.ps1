# MainAlto v1 candidate facade: LocalExecution owns all GitHub CLI effects.
[CmdletBinding()]
param(
    [ValidateSet('Inspect','Create')][string]$Mode='Inspect',
    [Parameter(Mandatory)][string]$Repository,
    [string]$ConfirmRepository,
    [Parameter(Mandatory)][string]$ProviderRoot,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ExpectedProviderSha256,
    [string]$Python='python'
)
$ErrorActionPreference='Stop'

function New-Result([string]$Reason,[string]$Status) {
    return [ordered]@{
        schema_version='mainalto.repo-create-observation/1'
        observation_id=[guid]::NewGuid().ToString()
        repository=$Repository;authenticated_owner=$null;visibility=$null
        observed_at=[DateTime]::UtcNow.ToString('o');mode=$Mode
        attempted_create=$null;status=$Status;reason_code=$Reason
    }
}

try {
    if ($Mode -eq 'Create' -and $ConfirmRepository -cne $Repository) {
        $report=New-Result 'EXPLICIT_REPOSITORY_CONFIRMATION_REQUIRED' 'BLOCKED'
        $report.attempted_create=$false
    } else {
        $provider=Join-Path $ProviderRoot 'tools/mainalto_repo_create_provider.py'
        if (-not (Test-Path -LiteralPath $provider -PathType Leaf)) {
            $report=New-Result 'PROVIDER_NOT_FOUND' 'BLOCKED'
            $report.attempted_create=$false
        } elseif ((Get-FileHash -LiteralPath $provider -Algorithm SHA256).Hash -ne $ExpectedProviderSha256) {
            $report=New-Result 'PROVIDER_HASH_MISMATCH' 'BLOCKED'
            $report.attempted_create=$false
        } else {
            $exe=Get-Command $Python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $exe) {
                $report=New-Result 'PYTHON_NOT_FOUND' 'BLOCKED'
                $report.attempted_create=$false
            } else {
                $start=[Diagnostics.ProcessStartInfo]::new()
                $start.FileName=$exe.Source
                $start.UseShellExecute=$false
                $start.CreateNoWindow=$true
                $start.RedirectStandardOutput=$true
                $start.RedirectStandardError=$true
                $start.StandardOutputEncoding=[Text.UTF8Encoding]::new($false)
                $start.StandardErrorEncoding=[Text.UTF8Encoding]::new($false)
                $args=@('-I','-B','-S',$provider,'--repository',$Repository,'--mode',$Mode)
                if ($Mode -eq 'Create') {$args+=@('--confirm-repository',$ConfirmRepository)}
                foreach ($arg in $args) {$start.ArgumentList.Add($arg)}
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
                        if ($parsed.schema_version -ne 'mainalto.repo-create-observation/1' -or
                            $parsed.mode -ne $Mode -or $parsed.repository -cne $Repository -or
                            $parsed.status -notin @('READY','PRESENT','CREATED','BLOCKED','UNKNOWN') -or
                            ($parsed.status -in @('READY','PRESENT','CREATED') -and $process.ExitCode -ne 0) -or
                            ($parsed.status -eq 'CREATED' -and ($Mode -ne 'Create' -or $parsed.visibility -ne 'PRIVATE'))) {
                            throw 'PROVIDER_REPORT_INVALID'
                        }
                        $report=$parsed
                    }
                } finally {$process.Dispose()}
            }
        }
    }
} catch {
    $report=New-Result 'FACADE_INVOCATION_FAILED' 'UNKNOWN'
}
$json=ConvertTo-Json -InputObject $report -Depth 20 -Compress
$bytes=[Text.UTF8Encoding]::new($false).GetBytes($json+"`n")
$stdout=[Console]::OpenStandardOutput()
$stdout.Write($bytes,0,$bytes.Length)
$stdout.Flush()
switch ($report.status) {
    {$_ -in @('READY','PRESENT','CREATED')} {exit 0}
    'UNKNOWN' {exit 3}
    default {exit 2}
}
