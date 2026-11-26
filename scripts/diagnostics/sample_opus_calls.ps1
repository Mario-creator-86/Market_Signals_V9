# Which parts of the app call the Claude subscription? Each call is one "claude -p" process whose
# command line carries the caller's system prompt. Sample them for a while and write one line per call
# (start time, pid, model, first 170 characters of the system prompt). Read-only.
#
#   powershell -File scripts\diagnostics\sample_opus_calls.ps1 -Minutes 10
#
# A call without a system prompt is the Pre-Market catalyst classifier; "You are a disciplined trading
# analyst." is the decision engine (Pre-Earnings decisions and autonomous reviews).
param([int]$Minutes = 8, [string]$Out = (Join-Path $env:TEMP 'opus_calls.tsv'))
$seen = @{}
$end = (Get-Date).AddMinutes($Minutes)
"started`tpid`tmodel`tsystem" | Out-File -FilePath $Out -Encoding utf8
while ((Get-Date) -lt $end) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='claude.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '* -p --model *' }
    foreach ($p in $procs) {
        $key = "$($p.ProcessId)|$($p.CreationDate.Ticks)"
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $cmd = [string]$p.CommandLine
        $model = if ($cmd -match '--model\s+(\S+)') { $Matches[1] } else { '' }
        $system = ''
        $at = $cmd.IndexOf('--system-prompt')
        if ($at -ge 0) { $system = $cmd.Substring($at + 15).Trim() }
        $system = ($system -replace '\s+', ' ')
        if ($system.Length -gt 170) { $system = $system.Substring(0, 170) }
        "$($p.CreationDate.ToString('HH:mm:ss'))`t$($p.ProcessId)`t$model`t$system" | Out-File -FilePath $Out -Encoding utf8 -Append
    }
    Start-Sleep -Milliseconds 1500
}
"done: $($seen.Count) calls seen in $Minutes minutes, written to $Out"
