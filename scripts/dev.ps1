param(
  [Parameter(Mandatory = $true, Position = 0)]
  [ValidateSet("up", "down", "status")]
  [string]$Command,
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$candidates = @(
  [pscustomobject]@{ File = $env:PYTHON; Prefix = @() },
  [pscustomobject]@{ File = "py"; Prefix = @("-3.11") },
  [pscustomobject]@{ File = "py"; Prefix = @("-3") },
  [pscustomobject]@{ File = "python"; Prefix = @() }
)

# Native commands below write to stderr on benign paths: `py -3.11` when that
# runtime is absent, and vite build warnings relayed by dev.py. Under
# $ErrorActionPreference = "Stop" those surface as a terminating
# NativeCommandError, so rely on exit codes for the rest of the script.
$ErrorActionPreference = "Continue"

$python = $null
foreach ($candidate in $candidates) {
  if (-not $candidate.File) { continue }
  if (-not (Get-Command $candidate.File -ErrorAction SilentlyContinue)) { continue }
  & $candidate.File @($candidate.Prefix) -c "import sys; assert sys.version_info >= (3, 11)" 2>&1 | Out-Null
  if ($LASTEXITCODE -eq 0) {
    $python = $candidate
    break
  }
}
if (-not $python) {
  throw "Python 3.11 or newer is required."
}

& $python.File @($python.Prefix) "$root\scripts\dev.py" $Command @RemainingArgs
exit $LASTEXITCODE
