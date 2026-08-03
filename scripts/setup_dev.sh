#!/bin/bash
set -e

echo "Installing all packages in development mode..."

pip install -e packages/pfa-parser
pip install -e packages/pfa-ir-consolidator
pip install -e packages/pfa-categorize
pip install -e packages/pfa-analysis
pip install -e apps/pfa-cli

echo ""
echo "All packages installed. Try: pfa --help"
