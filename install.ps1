$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$claudeDir = Join-Path $env:USERPROFILE ".claude"
$hooksDir = Join-Path $claudeDir "hooks"
$settingsPath = Join-Path $claudeDir "settings.json"
$backupPath = "$settingsPath.bak.$(Get-Date -Format 'yyyyMMddHHmmss')"

New-Item -ItemType Directory -Path $hooksDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo "hooks\stall_recover.py") -Destination (Join-Path $hooksDir "stall_recover.py") -Force
Copy-Item -LiteralPath (Join-Path $repo "hooks\tool_call_guard.py") -Destination (Join-Path $hooksDir "tool_call_guard.py") -Force

if (Test-Path $settingsPath) {
  Copy-Item -LiteralPath $settingsPath -Destination $backupPath -Force
  $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json -Depth 100
} else {
  $settings = [pscustomobject]@{}
}

if (-not $settings.PSObject.Properties.Name.Contains("env")) {
  $settings | Add-Member -NotePropertyName env -NotePropertyValue ([pscustomobject]@{})
}
$settings.env | Add-Member -NotePropertyName CLAUDE_CODE_STOP_HOOK_BLOCK_CAP -NotePropertyValue "64" -Force

if (-not $settings.PSObject.Properties.Name.Contains("hooks")) {
  $settings | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{})
}

$hookMap = [ordered]@{
  SessionStart = @(@{ hooks = @(@{ type = "command"; command = "python C:/Users/user/.claude/hooks/tool_call_guard.py"; timeout = 5 }) })
  UserPromptSubmit = @(@{ hooks = @(@{ type = "command"; command = "python C:/Users/user/.claude/hooks/tool_call_guard.py"; timeout = 5 }) })
  PostToolBatch = @(@{ hooks = @(@{ type = "command"; command = "python C:/Users/user/.claude/hooks/tool_call_guard.py"; timeout = 5 }) })
  Stop = @(@{ hooks = @(@{ type = "command"; command = "python C:/Users/user/.claude/hooks/stall_recover.py"; timeout = 15 }) })
}

foreach ($name in $hookMap.Keys) {
  $settings.hooks | Add-Member -NotePropertyName $name -NotePropertyValue $hookMap[$name] -Force
}

$settings | ConvertTo-Json -Depth 100 | Set-Content -LiteralPath $settingsPath -Encoding UTF8
[Environment]::SetEnvironmentVariable("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", "64", "User")

python (Join-Path $hooksDir "stall_recover.py") --help 2>$null | Out-Null
python (Join-Path $hooksDir "tool_call_guard.py") --help 2>$null | Out-Null

Write-Output "Installed hooks into $hooksDir"
Write-Output "Updated $settingsPath"
if (Test-Path $backupPath) {
  Write-Output "Backup: $backupPath"
}
Write-Output "Restart Claude Code or run /hooks to verify."

