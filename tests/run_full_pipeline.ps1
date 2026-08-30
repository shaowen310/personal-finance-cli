<#
.SYNOPSIS
    Run the full PFA pipeline: parse PDFs -> consolidate IR -> categorize -> report.

.DESCRIPTION
    Batch pipeline that mirrors the old run_full_pipeline.py:
      1. Parse each PDF in tests/cache into a ParsedStatement IR (.ir.json) via the
         canonical convert_statement.py CLI.
      2. Consolidate all .ir.json into tests/outputs/consolidated.ir.json via the
         consolidate.py CLI (--embed-fx).
      3. Categorize transactions from consolidated.ir.json -> categories.json via the
         categorize.py CLI.
      4. Generate finance_report.md from consolidated.ir.json + categories.json via the
         report.py CLI, renamed from consolidated_Finance_Report.md.

.PARAMETER Start
    Start date (YYYYMMDD or YYYYMM). YYYYMM uses the 1st of the month.
    Transactions before this date are excluded.

.PARAMETER End
    End date (YYYYMMDD or YYYYMM). YYYYMM uses the last day of the month.
    Transactions after this date are excluded.

.EXAMPLE
    .\run_full_pipeline.ps1
    .\run_full_pipeline.ps1 -s 20260601 -e 20260615
    .\run_full_pipeline.ps1 -s 202606 -e 202606
#>
param(
    [Alias("s")]
    [string]$Start,

    [Alias("e")]
    [string]$End
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
$RepoRoot   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PY         = "python"

# The pipeline shells out to the package CLIs (consolidate/categorize/report) and
# to ``python -m pfa_parser.convert_statement``. Those packages are normally
# available via editable installs, but ``python -m`` resolves imports relative to
# the current directory, which can vary depending on how the script is launched.
# Pin the working directory and put every package's ``src`` on PYTHONPATH so all
# imports resolve identically no matter the launch context.
Set-Location $RepoRoot
$srcDirs = @(
    (Join-Path $RepoRoot "packages" "pfa-parser" "src")
    (Join-Path $RepoRoot "packages" "pfa-ir-consolidator" "src")
    (Join-Path $RepoRoot "packages" "pfa-analysis" "src")
    (Join-Path $RepoRoot "packages" "pfa-ir-verifier" "src")
)
$env:PYTHONPATH = $srcDirs -join [System.IO.Path]::PathSeparator
# Silence the harmless "found in sys.modules" RuntimeWarning emitted when running
# ``python -m pfa_parser.convert_statement`` (a module inside the package we import).
$env:PYTHONWARNINGS = "ignore::RuntimeWarning"

$CACHE_DIR  = Join-Path $RepoRoot "tests" "cache"
$OUTPUT_DIR = Join-Path $RepoRoot "tests" "outputs"
$IR_DIR     = Join-Path $OUTPUT_DIR "ir"
$RULES_PATH = Join-Path $RepoRoot "packages" "pfa-analysis" "references" "categories.yaml"

$consolidateCli = Join-Path $RepoRoot "packages" "pfa-ir-consolidator" "src" "pfa_ir_consolidator" "consolidate.py"
$categorizeCli  = Join-Path $RepoRoot "packages" "pfa-analysis" "src" "pfa_analysis" "categorize.py"
$reportCli      = Join-Path $RepoRoot "packages" "pfa-analysis" "src" "pfa_analysis" "report.py"

# ---------------------------------------------------------------------------
# Date normalization (mirrors pfa_cli.dates.parse_start_date / parse_end_date)
# ---------------------------------------------------------------------------
function Format-StartDate {
    param([string]$Raw)
    if (-not $Raw) { return $null }
    $Raw = $Raw.Trim()
    if ($Raw -match '^\d{8}$') {
        return "$($Raw.Substring(0,4))-$($Raw.Substring(4,2))-$($Raw.Substring(6,2))"
    }
    if ($Raw -match '^\d{6}$') {
        return "$($Raw.Substring(0,4))-$($Raw.Substring(4,2))-01"
    }
    throw "Invalid start date '$Raw'. Expected YYYYMMDD or YYYYMM."
}

function Format-EndDate {
    param([string]$Raw)
    if (-not $Raw) { return $null }
    $Raw = $Raw.Trim()
    if ($Raw -match '^\d{8}$') {
        return "$($Raw.Substring(0,4))-$($Raw.Substring(4,2))-$($Raw.Substring(6,2))"
    }
    if ($Raw -match '^\d{6}$') {
        $year  = [int]$Raw.Substring(0, 4)
        $month = [int]$Raw.Substring(4, 2)
        $last  = [DateTime]::DaysInMonth($year, $month)
        return "$($Raw.Substring(0,4))-$($Raw.Substring(4,2))-$($last.ToString('00'))"
    }
    throw "Invalid end date '$Raw'. Expected YYYYMMDD or YYYYMM."
}

# Date ranges are supplied only via the native PowerShell forms:
#   -Start / -End   (full parameter names)
#   -s / -e         (aliases)
# Double-dash (--start / --end) is intentionally not supported.
$StartDate = if ($Start) { Format-StartDate $Start } else { $null }
$EndDate   = if ($End)   { Format-EndDate   $End   } else { $null }

# ---------------------------------------------------------------------------
# Output directory reset (Remove-Item -Force tolerates read-only / OneDrive flags)
# ---------------------------------------------------------------------------
if (Test-Path $OUTPUT_DIR) {
    Remove-Item $OUTPUT_DIR -Recurse -Force
}
New-Item -ItemType Directory -Path $IR_DIR -Force | Out-Null
Write-Host "Cleaned: $OUTPUT_DIR"

# ---------------------------------------------------------------------------
# Step 1: Parse all PDFs
# ---------------------------------------------------------------------------
$pdfs = Get-ChildItem -Path $CACHE_DIR -Filter *.pdf | Sort-Object Name
if ($pdfs.Count -eq 0) {
    Write-Host "No PDF files found in $CACHE_DIR"
    exit
}

Write-Host "`n── Step 1: Parse PDFs ──"
$step1ok = $true
foreach ($pdf in $pdfs) {
    $stem = $pdf.BaseName
    Write-Host "  Parsing: $($pdf.Name) … " -NoNewline
    # convert_statement.py writes the IR as <out>.ir.json (sibling of the .md path).
    $irMd = Join-Path $IR_DIR "$stem.md"
    $out  = & $PY -m pfa_parser.convert_statement $pdf.FullName $irMd --ir-only 2>&1
    if ($LASTEXITCODE -ne 0) {
        $step1ok = $false
        Write-Host "FAILED"
        $out | ForEach-Object { Write-Host "    $_" }
    }
    else {
        Write-Host "OK"
        $out | ForEach-Object { Write-Host "    $_" }
    }
}
if (-not $step1ok) {
    Write-Host "`nStep 1 had errors.  Continuing with available outputs …`n"
}

# ---------------------------------------------------------------------------
# Step 2: Consolidate IR
# ---------------------------------------------------------------------------
Write-Host "`n── Step 2: Consolidate IR ──"
$irFiles = Get-ChildItem -Path $IR_DIR -Filter *.ir.json | Sort-Object Name
if ($irFiles.Count -eq 0) {
    Write-Host "  No .ir.json files found — skipping consolidation."
    $consolidatedPath = $null
}
else {
    $outPath = Join-Path $OUTPUT_DIR "consolidated.ir.json"
    $irArgArray = @($irFiles.FullName)
    Write-Host "  Consolidating $($irFiles.Count) .ir.json files via CLI … " -NoNewline
    $res = & $PY $consolidateCli @irArgArray -o $outPath --embed-fx 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED"
        $res | ForEach-Object { Write-Host "    $_" }
        $consolidatedPath = $null
    }
    else {
        Write-Host "OK -> $($outPath.Name)"
        $res | ForEach-Object { Write-Host "  $_" }
        $consolidatedPath = $outPath
    }
}
if (-not $consolidatedPath) {
    Write-Host "ERROR: Consolidation failed — cannot proceed."
    exit
}

# ---------------------------------------------------------------------------
# Step 3: Categorize
# ---------------------------------------------------------------------------
Write-Host "`n── Step 3: Categorize ──"
$catOut = Join-Path $OUTPUT_DIR "categories.json"
Write-Host "  Categorizing $($consolidatedPath | Split-Path -Leaf) via CLI … " -NoNewline
$res = & $PY $categorizeCli $consolidatedPath -o $catOut --rules $RULES_PATH 2>&1
if ($LASTEXITCODE -ne 0 -and -not (Test-Path $catOut)) {
    Write-Host "FAILED"
    $res | ForEach-Object { Write-Host "    $_" }
}
else {
    if ($LASTEXITCODE -ne 0) {
        Write-Host "OK (with coverage warnings)"
    }
    else {
        Write-Host "OK -> $($catOut.Name)"
    }
    $res | ForEach-Object { Write-Host "  $_" }
}

# ---------------------------------------------------------------------------
# Step 4: Render Markdown Reports
# ---------------------------------------------------------------------------
Write-Host "`n── Step 4: Render Reports ──"
$catPath = Join-Path $OUTPUT_DIR "categories.json"
if (-not (Test-Path $catPath)) {
    Write-Host "  categories.json not found — skipping reports."
}
else {
    $dateArgs = @()
    if ($StartDate) { $dateArgs += "--start-date", $StartDate }
    if ($EndDate)   { $dateArgs += "--end-date",   $EndDate }

    Write-Host "  4a. Finance report via CLI … " -NoNewline
    $res = & $PY $reportCli $consolidatedPath $OUTPUT_DIR --categories $catPath @dateArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED"
        $res | ForEach-Object { Write-Host "    $_" }
    }
    else {
        # report CLI writes "<stem>_Finance_Report.md"; rename to finance_report.md.
        $cliMd   = Join-Path $OUTPUT_DIR "$([System.IO.Path]::GetFileNameWithoutExtension($consolidatedPath))_Finance_Report.md"
        $finalMd = Join-Path $OUTPUT_DIR "finance_report.md"
        if ((Test-Path $cliMd) -and ($cliMd -ne $finalMd)) {
            Move-Item $cliMd $finalMd -Force
        }
        Write-Host "OK -> $($finalMd.Name)"
        $res | ForEach-Object { Write-Host "  $_" }
    }
}

Write-Host "`nDone — outputs in $OUTPUT_DIR"
