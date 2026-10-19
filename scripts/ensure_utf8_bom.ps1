# Ensure every .ps1 in this folder that contains non-ASCII text is saved as
# UTF-8 *WITH* BOM.
#
# Why this file exists (2026-10-02, real incident):
#   Windows PowerShell 5.1 is what start.bat / start_cloud.bat actually use.
#   It decodes a BOM-less UTF-8 script as GBK. Chinese text then turns into
#   garbage AND some characters swallow the following quote, so PowerShell
#   reports "The string is missing the terminator" / "Missing closing '}'"
#   and the script refuses to run at all.
#   Some editors and tools silently strip the BOM, so every launcher (.bat)
#   runs this guard FIRST. This file is pure ASCII on purpose, so it works
#   with or without a BOM itself.
#
# Exit code is always 0 - a launcher must never be blocked by the guard.

$ErrorActionPreference = 'SilentlyContinue'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$fixed = 0

Get-ChildItem -Path (Join-Path $dir '*.ps1') -File -ErrorAction SilentlyContinue | ForEach-Object {
    $path = $_.FullName
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if ($bytes.Length -lt 3) { return }

    $hasBom = ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    if ($hasBom) { return }

    $nonAscii = $false
    foreach ($b in $bytes) {
        if ($b -gt 127) { $nonAscii = $true; break }
    }
    if (-not $nonAscii) { return }

    $text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($true)))
    Write-Host "  [guard] fixed UTF-8 BOM: $($_.Name)"
    $fixed++
}

if ($fixed -gt 0) {
    Write-Host "  [guard] $fixed script(s) repaired - Chinese text will display correctly."
}

exit 0
