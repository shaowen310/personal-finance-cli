$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Write-Host "Installing all packages in development mode (repo: $RepoRoot)..."
Write-Host "Installing pfa-ir-schema (leaf dependency, must go first)..."
pip install -e (Join-Path $RepoRoot "packages/pfa-ir-schema")
Write-Host "Installing pfa-fx (leaf FX dependency)..."
pip install -e (Join-Path $RepoRoot "packages/pfa-fx")
Write-Host "Installing pfa-parser..."
pip install -e (Join-Path $RepoRoot "packages/pfa-parser")
Write-Host "Installing pfa-ir-consolidator..."
pip install -e (Join-Path $RepoRoot "packages/pfa-ir-consolidator")
Write-Host "Installing pfa-analysis..."
pip install -e (Join-Path $RepoRoot "packages/pfa-analysis")
Write-Host "Installing pfa-cli..."
pip install -e (Join-Path $RepoRoot "apps/pfa-cli")
Write-Host ""
Write-Host "All packages installed. Try: pfa --help"
