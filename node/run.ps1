# One-command native node, Windows edition (PowerShell 5.1+):
# collector (foreground, --show works normally) + hourly shipper (background).
# Auto-restarts the collector on crash, blocks idle sleep, cleans up the
# shipper on exit. Mirrors run.sh.
#
# Setup once:  Copy-Item .env.example .env   # put your TW_CENTRAL_DSN in it
# Run:         .\run.ps1 [--show] [watch.py args...]
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"

# .env -> process environment (KEY=VALUE lines, # comments)
if (Test-Path .env) {
    foreach ($line in Get-Content .env) {
        if ($line -match '^\s*([^#=\s][^=]*)=(.*)$') {
            [Environment]::SetEnvironmentVariable(
                $Matches[1].Trim(), $Matches[2].Trim(), "Process")
        }
    }
}

# block idle sleep while we run (display may still turn off) -- the
# caffeinate of Windows. ES_CONTINUOUS | ES_SYSTEM_REQUIRED.
# (values via [uint32]: PowerShell parses 0x80000001 as a negative Int32)
Add-Type -Name Power -Namespace TW -MemberDefinition @'
[DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
[TW.Power]::SetThreadExecutionState([uint32]2147483649) | Out-Null

$shipper = $null
if ($env:TW_CENTRAL_DSN) {
    New-Item -ItemType Directory -Force data | Out-Null
    $shipper = Start-Process -FilePath $py -ArgumentList "-u", "aggregator.py", "--loop" `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput "data\shipper.log" -RedirectStandardError "data\shipper.err.log"
    Write-Host "shipper running (pid $($shipper.Id), log: node/data/shipper.log)"
} else {
    Write-Warning "TW_CENTRAL_DSN not set (node/.env) -- collecting locally, NOT shipping"
}

try {
    while ($true) {
        & $py watch.py @args
        $code = $LASTEXITCODE
        if ($code -eq 0) { break }        # clean exit (ctrl-c / q) stops the loop
        Write-Warning "watch.py exited with $code -- restarting in 10s"
        Start-Sleep -Seconds 10
    }
} finally {
    if ($shipper -and -not $shipper.HasExited) {
        Stop-Process -Id $shipper.Id -Force -ErrorAction SilentlyContinue
    }
    [TW.Power]::SetThreadExecutionState([uint32]2147483648) | Out-Null  # ES_CONTINUOUS: release
}
