$ErrorActionPreference = "Stop"

Write-Host "Installing all packages in development mode..."

pip install -e packages/pfa-parser
pip install -e packages/pfa-ir-consolidator
pip install -e packages/pfa-categorize
pip install -e packages/pfa-analysis
pip install -e apps/pfa-cli

Write-Host ""
Write-Host "All packages installed. Try: pfa --help"
