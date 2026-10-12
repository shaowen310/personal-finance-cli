<#
.SYNOPSIS
Convert bank statement PDFs in pdf/<bank>/estatements to IR JSON in ir/<bank>.

.DESCRIPTION
Uses the conda "doc" environment and the manually installed `pfa` CLI to
convert every PDF under <PdfRoot>\<bank>\estatements into an intermediate
representation (IR) JSON file via `pfa parse -o`, written to
<IrRoot>\<bank>\<stem>.ir.json.

.PARAMETER PdfRoot
Root folder containing per-bank subfolders that have an "estatements"
subfolder. Default: <script dir>\pdf

.PARAMETER IrRoot
Output folder for IR JSON files. Default: <script dir>\ir

.PARAMETER CondaEnv
Name of the conda environment in which pfa is installed. Default: doc

.PARAMETER CondaExe
Path to conda.exe. Auto-detected when not supplied.

.EXAMPLE
.\convert_estatements_to_ir.ps1

.EXAMPLE
.\convert_estatements_to_ir.ps1 -PdfRoot D:\stmts -IrRoot D:\ir -CondaEnv doc

.PARAMETER Force
Reprocess every PDF even if a matching, up-to-date IR output already exists.
#>

[CmdletBinding()]
param(
    [string]$PdfRoot  = '',
    [string]$IrRoot   = '',
    [string]$CondaEnv = 'doc',
    [string]$CondaExe = '',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot is not available while param defaults are evaluated, so
# resolve script-relative defaults after the param block.
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
if (-not $PdfRoot) { $PdfRoot = Join-Path $scriptDir 'pdf' }
if (-not $IrRoot)  { $IrRoot  = Join-Path $scriptDir 'ir' }

# ---------------------------------------------------------------------------
# Locate conda / the target environment's pfa.exe
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

function Get-PfaPath {
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
    $pfa = Join-Path $base ("envs\{0}\Scripts\pfa.exe" -f $EnvName)
    if (-not (Test-Path $pfa)) {
        throw "pfa not found in env '$EnvName' (expected $pfa). Is pfa installed there?"
    }
    return $pfa
}

# ---------------------------------------------------------------------------
# Verify prerequisites
# ---------------------------------------------------------------------------
if (-not (Test-Path $PdfRoot)) {
    throw "PdfRoot not found: $PdfRoot"
}
New-Item -ItemType Directory -Force -Path $IrRoot | Out-Null

# pfa cannot handle OneDrive paths, so we run it from a
# plain temp work directory and move results back. See the conversion loop.
$WorkDir = Join-Path $env:TEMP ('pfa_work_{0}' -f [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

$conda = Find-Conda
$pfa    = Get-PfaPath -CondaPath $conda -EnvName $CondaEnv
Write-Host "conda : $conda"
Write-Host "pfa   : $pfa (env '$CondaEnv')"

# ---------------------------------------------------------------------------
# Convert every PDF in every bank's estatements folder via `pfa parse -o`
# ---------------------------------------------------------------------------
$banks = Get-ChildItem -Path $PdfRoot -Directory | Where-Object {
    Test-Path (Join-Path $_.FullName 'estatements')
}

if (-not $banks) {
    Write-Warning "No bank folders containing an 'estatements' subfolder under $PdfRoot"
    exit 0
}

$ok = 0
$skipped = 0
$failed = 0
$failures = @()

foreach ($bank in $banks) {
    $estDir = Join-Path $bank.FullName 'estatements'
    $pdfs = Get-ChildItem -Path $estDir -Filter *.pdf -File
    Write-Host ""
    Write-Host ("== {0} ({1} PDFs) ==" -f $bank.Name, $pdfs.Count)
    if (-not $pdfs) { continue }

    foreach ($pdf in $pdfs) {
        $outFile = Join-Path (Join-Path $IrRoot $bank.Name) ("{0}.ir.json" -f $pdf.BaseName)

        # Skip if a valid IR output already exists and is newer than the source
        # PDF (unless -Force is supplied). This makes re-runs incremental: only
        # new or changed PDFs are reprocessed. A failed run deletes its partial
        # output, so a stale file never fools this check.
        if (-not $Force -and (Test-Path $outFile)) {
            $outItem = Get-Item $outFile
            if ($outItem.LastWriteTime -ge $pdf.LastWriteTime) {
                Write-Host ("  {0} -> SKIP (already converted)" -f $pdf.Name) -ForegroundColor DarkGray
                $skipped++
                continue
            }
        }

        Write-Host ("  {0} -> {1}" -f $pdf.Name, $outFile) -NoNewline

        # pfa cannot handle OneDrive paths: it silently
        # fails ("filename syntax is incorrect") and writes an EMPTY IR file
        # when given an input/output under C:\Users\<user>\OneDrive\... . To work
        # around this, copy the PDF into a plain temp work dir, run pfa there,
        # then move the resulting .ir.json back to the real output location.
        $workPdf = Join-Path $WorkDir ($pdf.Name)
        $workOut = Join-Path $WorkDir ("{0}.ir.json" -f $pdf.BaseName)
        Copy-Item -Path $pdf.FullName -Destination $workPdf -Force

        # Run pfa via Start-Process so stdout/stderr are captured as raw text
        # (native stderr redirected inside PowerShell turns into noisy
        # ErrorRecords, or a terminating error under EAP='Stop' in 5.1).
        $stdFile = Join-Path $env:TEMP ("pfa_out_{0}.log" -f [guid]::NewGuid().ToString('N'))
        $errFile = Join-Path $env:TEMP ("pfa_err_{0}.log" -f [guid]::NewGuid().ToString('N'))
        # Quote any argument containing spaces (Start-Process does not auto-quote).
        $procArgs = @('parse', '-i', $workPdf, '-o', $workOut) | ForEach-Object {
            if ($_ -match '\s') { "`"$_`"" } else { $_ }
        }
        $proc = Start-Process -FilePath $pfa `
            -ArgumentList $procArgs `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdFile -RedirectStandardError $errFile

        $rawOut = Get-Content $stdFile -Raw
        $result = if ($null -ne $rawOut) { $rawOut.Trim() } else { '' }
        if ($proc.ExitCode -eq 0 -and (Test-Path $workOut)) {
            # Move the freshly parsed IR back to the real (OneDrive) output path.
            Move-Item -Path $workOut -Destination $outFile -Force
            $ok++
            Write-Host "  OK" -ForegroundColor Green
        } else {
            $failed++
            $failures += $pdf.FullName
            Write-Host "  FAILED" -ForegroundColor Red
            if (Test-Path $errFile) {
                Get-Content $errFile | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
            }
        }
        Remove-Item $workPdf -ErrorAction SilentlyContinue
        Remove-Item $stdFile, $errFile -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host ("Done: {0} converted, {1} skipped, {2} failed." -f $ok, $skipped, $failed)
if ($failures.Count -gt 0) {
    Write-Host "Failed files:" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 1
}
