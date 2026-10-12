<#
.SYNOPSIS
Build a consolidated IR JSON and a finance report for a given year/month.

.DESCRIPTION
Prompts for a year and month, then:
  1. Gathers every *.ir.json under <IrRoot> whose filename embeds the YYYYMM
     (e.g. DBS_202606_cons.ir.json).
  2. Consolidates them into <ReportRoot>\<YYYYMM>\<YYYYMM>.ir.json via the
     `pfa consolidate` command (conda "doc" environment).
  3. Generates the finance report via `pfa analyze -m <YYYYMM>` into the same
     monthly folder.

.PARAMETER Year
Year (e.g. 2026). If omitted you are prompted (default when blank: current year).

.PARAMETER Month
Month 1-12. If omitted you are prompted (default when blank: last month;
for January that means December of the previous year).

.PARAMETER IrRoot
Folder containing per-bank IR JSON files (output of the PDF->IR step).
Default: <script dir>\ir

.PARAMETER ReportRoot
Output root for monthly reports. Default: <script dir>\report\monthly

.PARAMETER CondaEnv
Name of the conda environment in which pfa is installed. Default: doc

.PARAMETER CondaExe
Path to conda.exe. Auto-detected when not supplied.

.PARAMETER ExcludeNextMonthCC
By default the consolidation also includes the following month's *_cc_*
(credit-card) statements, because credit cards bill on a cycle that lags the
spend month (some of month M's spending only shows up on the next statement).
Pass -ExcludeNextMonthCC to skip that and use only month M's statements.

.EXAMPLE
.\build_monthly_report.ps1
.\build_monthly_report.ps1 -Year 2026 -Month 6
.\build_monthly_report.ps1 -Year 2026 -Month 7 -ExcludeNextMonthCC
#>

[CmdletBinding()]
param(
    [int]$Year    = 0,
    [int]$Month   = 0,
    [string]$IrRoot      = '',
    [string]$ReportRoot  = '',
    [string]$CondaEnv    = 'doc',
    [string]$CondaExe    = '',
    [switch]$ExcludeNextMonthCC,
    [switch]$SkipCategorize,
    [string]$CategoriesRules = '',
    [switch]$Llm,
    [string]$LlmModel   = 'gpt-4o-mini',
    [string]$LlmApiKey  = '',
    [string]$LlmBaseUrl = ''
)

$ErrorActionPreference = 'Stop'

# Resolve script-relative defaults after the param block ($PSScriptRoot is
# unavailable while param defaults are evaluated).
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
if (-not $IrRoot)     { $IrRoot     = Join-Path $scriptDir 'ir' }
if (-not $ReportRoot) { $ReportRoot = Join-Path $scriptDir 'report\monthly' }

# ---------------------------------------------------------------------------
# Collect year / month
# ---------------------------------------------------------------------------
if ($Year -le 0) {
    $Year = [int](Read-Host "Enter year (default: $((Get-Date).Year))")
    if ($Year -le 0) { $Year = (Get-Date).Year }
}
if ($Month -le 0) {
    # Default to last month (handles January -> previous year December).
    $last = (Get-Date).AddMonths(-1)
    $Month = [int](Read-Host "Enter month 1-12 (default: $($last.Month))")
    if ($Month -le 0) {
        $Month = $last.Month
        if ($Month -eq 12 -and (Get-Date).Month -eq 1) { $Year = $last.Year }
    }
}
if ($Year -le 0 -or $Month -lt 1 -or $Month -gt 12) {
    throw "Invalid year/month: $Year / $Month"
}
$yyyymm = "{0:D4}{1:D2}" -f $Year, $Month
Write-Host "Building consolidated IR + report for $yyyymm"

# ---------------------------------------------------------------------------
# Locate conda / the target environment's pfa.exe (via its python path)
# ---------------------------------------------------------------------------
function Find-Conda {
    if ($CondaExe -and (Test-Path $CondaExe)) { return (Resolve-Path $CondaExe).Path }
    $cmd = Get-Command conda -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($base in @(
        "$env:USERPROFILE\miniconda3",
        "$env:USERPROFILE\anaconda3",
        "$env:USERPROFILE\miniforge3",
        "C:\ProgramData\miniconda3",
        "C:\ProgramData\Anaconda3"
    )) {
        $exe = Join-Path $base 'Scripts\conda.exe'
        if (Test-Path $exe) { return $exe }
    }
    throw "conda.exe not found. Pass -CondaExe explicitly."
}

function Get-EnvPythonPath {
    param([string]$CondaPath, [string]$EnvName)
    try {
        $base = (& $CondaPath info --base 2>$null | Select-Object -First 1).Trim()
    } catch { $base = '' }
    if (-not $base) {
        foreach ($candidate in @(
            "$env:USERPROFILE\miniconda3",
            "$env:USERPROFILE\anaconda3",
            "$env:USERPROFILE\miniforge3"
        )) {
            if (Test-Path $candidate) { $base = $candidate; break }
        }
    }
    if (-not $base) { throw "Could not resolve conda base path from $CondaPath." }
    $py = Join-Path $base ("envs\{0}\python.exe" -f $EnvName)
    if (-not (Test-Path $py)) {
        throw "Environment '$EnvName' python not found: $py"
    }
    return $py
}

if (-not (Test-Path $IrRoot)) { throw "IrRoot not found: $IrRoot" }
$monthDir = Join-Path $ReportRoot $yyyymm
New-Item -ItemType Directory -Force -Path $monthDir | Out-Null

$conda  = Find-Conda
$python = Get-EnvPythonPath -CondaPath $conda -EnvName $CondaEnv
$pfa    = Join-Path (Split-Path $python) 'Scripts\pfa.exe'
if (-not (Test-Path $pfa)) { throw "pfa.exe not found in env '$CondaEnv': $pfa" }
Write-Host "conda : $conda"
Write-Host "pfa   : $pfa (env '$CondaEnv')"

# ---------------------------------------------------------------------------
# 1) Collect matching IR files
# ---------------------------------------------------------------------------
# All per-bank variants for the month are consolidated together (e.g. UOB:
# one / sa / cons are all included; UOB credit cards below). Other banks
# (DBS/OCBC/ICBC) behave the same way.
$matching = @()
foreach ($f in Get-ChildItem -Path $IrRoot -Filter *.ir.json -Recurse -File) {
    if ($f.BaseName -match $yyyymm) { $matching += $f.FullName }
}

# Credit cards bill a cycle that lags the spend month: part of month M's
# spending only appears on the following statement. By default, pull the next
# month's *_cc_* statements into the consolidation so those late transactions
# are captured. (The `analyze -m` report still scopes to month M.) Use
# -ExcludeNextMonthCC to skip this.
if (-not $ExcludeNextMonthCC) {
    $next = [datetime]::ParseExact($yyyymm, 'yyyyMM', $null)
    $next = $next.AddMonths(1)
    $nextYYMM = $next.ToString('yyyyMM')
    foreach ($f in Get-ChildItem -Path $IrRoot -Filter *.ir.json -Recurse -File) {
        if ($f.BaseName -match $nextYYMM -and $f.BaseName -match '_cc_') {
            $matching += $f.FullName
        }
    }
}

if ($matching.Count -eq 0) {
    throw "No IR files matching '$yyyymm' found under $IrRoot"
}
Write-Host ("Found {0} IR file(s) for {1}{2}:" -f $matching.Count, $yyyymm, $(if ($IncludeNextMonthCC) { " (+ next-month CC)" } else { '' }))
$matching | ForEach-Object { Write-Host ("  {0}" -f $_) -ForegroundColor DarkGray }

$irOut = Join-Path $monthDir ("{0}.ir.json" -f $yyyymm)

# ---------------------------------------------------------------------------
# 2) Consolidate via `pfa consolidate INPUTS... -o <out>`
# ---------------------------------------------------------------------------
Write-Host "Consolidating -> $irOut"
$procArgs = @('consolidate') + $matching + @('-o', $irOut)
$procArgs = $procArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }

# Run via Start-Process so stdout/stderr are captured as raw text (native
# stderr redirected inside PowerShell turns into noisy ErrorRecords, or a
# terminating error under EAP='Stop' in 5.1).
$stdFile = Join-Path $env:TEMP ("pfa_con_out_{0}.log" -f [guid]::NewGuid().ToString('N'))
$errFile = Join-Path $env:TEMP ("pfa_con_err_{0}.log" -f [guid]::NewGuid().ToString('N'))
$proc = Start-Process -FilePath $pfa -ArgumentList $procArgs `
    -NoNewWindow -Wait -PassThru -RedirectStandardOutput $stdFile -RedirectStandardError $errFile
Get-Content $stdFile | ForEach-Object { if ($_.Trim()) { Write-Host "  $_" } }
if ($proc.ExitCode -ne 0) {
    Write-Host "  Consolidation FAILED:" -ForegroundColor Red
    if (Test-Path $errFile) { Get-Content $errFile | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
    Remove-Item $stdFile, $errFile -ErrorAction SilentlyContinue
    throw "Consolidation failed (exit $($proc.ExitCode))."
}
Remove-Item $stdFile, $errFile -ErrorAction SilentlyContinue
if (-not (Test-Path $irOut)) { throw "Consolidated IR was not produced: $irOut" }

# ---------------------------------------------------------------------------
# 2.5) Categorization is now performed inside `pfa analyze` (it generates
#      categories.json next to the consolidated IR when missing and reuses it
#      otherwise). LLM credentials are passed via OPENAI_* env vars below.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 3) Generate the finance report via `pfa analyze -m <YYYYMM>`
#    `analyze` auto-categorizes (reusing/generating categories.json next to the
#    IR). LLM credentials are supplied through the standard OPENAI_* env vars.
# ---------------------------------------------------------------------------

# Feed the LLM credentials through the standard OPENAI_* env vars so the
# `analyze` subcommand's categorization picks them up (no secret on the command line).
if ($LlmApiKey)  { $env:OPENAI_API_KEY  = $LlmApiKey }
if ($LlmBaseUrl) { $env:OPENAI_BASE_URL = $LlmBaseUrl }

Write-Host "Analyzing -> $monthDir"
$procArgs = @('analyze', '-m', $yyyymm, '-i', $irOut, '-o', $monthDir)
if ($CategoriesRules) { $procArgs += @('--rules', $CategoriesRules) }
if ($Llm) { $procArgs += '--llm' }
$procArgs += @('--model', $LlmModel)
if ($SkipCategorize) { $procArgs += '--no-categorize' }
$procArgs = $procArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }
$stdFile = Join-Path $env:TEMP ("pfa_an_out_{0}.log" -f [guid]::NewGuid().ToString('N'))
$errFile = Join-Path $env:TEMP ("pfa_an_err_{0}.log" -f [guid]::NewGuid().ToString('N'))
$proc = Start-Process -FilePath $pfa -ArgumentList $procArgs -NoNewWindow -Wait -PassThru `
    -RedirectStandardOutput $stdFile -RedirectStandardError $errFile
Get-Content $stdFile | ForEach-Object { if ($_.Trim()) { Write-Host "  $_" } }
if ($proc.ExitCode -ne 0) {
    Write-Host "  pfa analyze FAILED:" -ForegroundColor Red
    if (Test-Path $errFile) { Get-Content $errFile | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray } }
    Remove-Item $stdFile, $errFile -ErrorAction SilentlyContinue
    # Clean up env vars even on failure.
    if ($LlmApiKey)  { Remove-Item Env:\OPENAI_API_KEY  -ErrorAction SilentlyContinue }
    if ($LlmBaseUrl) { Remove-Item Env:\OPENAI_BASE_URL -ErrorAction SilentlyContinue }
    throw "pfa analyze failed (exit $($proc.ExitCode))."
}
Remove-Item $stdFile, $errFile -ErrorAction SilentlyContinue
# Clean up env vars.
if ($LlmApiKey)  { Remove-Item Env:\OPENAI_API_KEY  -ErrorAction SilentlyContinue }
if ($LlmBaseUrl) { Remove-Item Env:\OPENAI_BASE_URL -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------------------
# Rename the report from "<stem>_Finance_Report.md" to a cleaner name
# ---------------------------------------------------------------------------
$rawReport = Join-Path $monthDir ("{0}.ir_Finance_Report.md" -f $yyyymm)
$cleanReport = Join-Path $monthDir ("{0}_Finance_Report.md" -f $yyyymm)
if (Test-Path $rawReport) {
    Move-Item -Path $rawReport -Destination $cleanReport -Force
}

Write-Host ""
Write-Host ("Done for {0}:" -f $yyyymm) -ForegroundColor Green
Write-Host ("  IR    : {0}" -f $irOut)
Write-Host ("  Report: {0}" -f $cleanReport)
