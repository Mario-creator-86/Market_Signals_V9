param(
    [int]$Step = 4
)

Add-Type -AssemblyName System.Drawing

$baseDir = Join-Path $PSScriptRoot "..\output\ui_audit_baseline"
$currDir = Join-Path $PSScriptRoot "..\output\ui_audit"
$diffDir = Join-Path $PSScriptRoot "..\output\ui_audit_diff"

function Normalize-Path([string]$path) {
    try {
        return [IO.Path]::GetFullPath($path)
    } catch {
        return $path
    }
}

if (-not (Test-Path $diffDir)) { New-Item -ItemType Directory -Path $diffDir | Out-Null }

$report = [ordered]@{
    baseline = (Normalize-Path $baseDir)
    current = (Normalize-Path $currDir)
    diff_dir = (Normalize-Path $diffDir)
    step = $Step
    files = @()
}

Get-ChildItem -Path $currDir -Filter *.png | Sort-Object Name | ForEach-Object {
    $name = $_.Name
    $basePath = Join-Path $baseDir $name
    $entry = [ordered]@{ name = $name; status = "ok" }

    if (-not (Test-Path $basePath)) {
        $entry.status = "missing_baseline"
        $report.files += $entry
        return
    }

    try {
        $bmpA = New-Object System.Drawing.Bitmap($basePath)
        $bmpB = New-Object System.Drawing.Bitmap($_.FullName)

        if ($bmpA.Width -ne $bmpB.Width -or $bmpA.Height -ne $bmpB.Height) {
            $entry.status = "size_mismatch"
            $entry.baseline_size = "$($bmpA.Width)x$($bmpA.Height)"
            $entry.current_size = "$($bmpB.Width)x$($bmpB.Height)"
            $report.files += $entry
            $bmpA.Dispose(); $bmpB.Dispose()
            return
        }

        $w = $bmpA.Width
        $h = $bmpA.Height
        $total = 0
        $diff = 0

        for ($y = 0; $y -lt $h; $y += $Step) {
            for ($x = 0; $x -lt $w; $x += $Step) {
                $total++
                if ($bmpA.GetPixel($x, $y).ToArgb() -ne $bmpB.GetPixel($x, $y).ToArgb()) {
                    $diff++
                }
            }
        }

        $pct = 0.0
        if ($total -gt 0) { $pct = [math]::Round(($diff / $total) * 100, 4) }
        $entry.diff_samples = $diff
        $entry.total_samples = $total
        $entry.diff_pct = $pct

        $report.files += $entry
        $bmpA.Dispose(); $bmpB.Dispose()
    } catch {
        $entry.status = "error"
        $entry.error = $_.Exception.Message
        $report.files += $entry
        try { $bmpA.Dispose() } catch {}
        try { $bmpB.Dispose() } catch {}
    }
}

$reportPath = Join-Path $diffDir "report.json"
$report | ConvertTo-Json -Depth 6 | Set-Content -Path $reportPath -Encoding utf8
Write-Output "Diff report written to $reportPath"
