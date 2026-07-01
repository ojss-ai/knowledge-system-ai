# Installs the claude-kit into .claude/ so Claude Code auto-discovers skills, commands, and agents.
# Run from the repo root:  powershell -ExecutionPolicy Bypass -File claude-kit\install.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$kit  = Join-Path $root "claude-kit"
$dest = Join-Path $root ".claude"

foreach ($dir in @("skills", "commands", "agents")) {
    $src = Join-Path $kit $dir
    $dst = Join-Path $dest $dir
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Copy-Item -Recurse -Force -Path (Join-Path $src "*") -Destination $dst
    Write-Host "Installed $dir -> .claude\$dir"
}
Write-Host "`nDone. Restart Claude Code in this repo; /kb-status should be available."
