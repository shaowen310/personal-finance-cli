#!/bin/bash
echo "Installing all packages in development mode..."
echo "Installing pfa-ir-schema (leaf dependency, must go first)..."
pip install -e packages/pfa-ir-schema
echo "Installing pfa-parser..."
pip install -e packages/pfa-parser
echo "Installing pfa-ir-consolidator..."
pip install -e packages/pfa-ir-consolidator
echo "Installing pfa-categorize..."
pip install -e packages/pfa-categorize
echo "Installing pfa-analysis..."
pip install -e packages/pfa-analysis
echo "Installing pfa-cli..."
pip install -e apps/pfa-cli
echo ""
echo "All packages installed. Try: pfa --help"
