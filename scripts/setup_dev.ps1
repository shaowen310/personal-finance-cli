Write-Host "Installing all packages in development mode..."
Write-Host "Installing pfa-ir-schema (leaf dependency, must go first)..."
pip install -e packages/pfa-ir-schema
Write-Host "Installing pfa-parser..."
pip install -e packages/pfa-parser
Write-Host "Installing pfa-ir-consolidator..."
pip install -e packages/pfa-ir-consolidator
Write-Host "Installing pfa-categorize..."
pip install -e packages/pfa-categorize
Write-Host "Installing pfa-analysis..."
pip install -e packages/pfa-analysis
Write-Host "Installing pfa-cli..."
pip install -e apps/pfa-cli
Write-Host ""
Write-Host "All packages installed. Try: pfa --help"
