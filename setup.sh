#!/bin/bash
# So Clover Online — Quick Setup Script
set -euo pipefail

echo "🍀 Setting up So Clover Online..."

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt --quiet

# Validate environment
if [ -z "${DJANGO_SECRET_KEY:-}" ]; then
    echo "WARNING: DJANGO_SECRET_KEY not set. Using insecure default for development."
fi

# Database setup (migrations only - never run makemigrations in setup)
python manage.py migrate --no-input

# Collect static files
python manage.py collectstatic --no-input

# Verify setup
echo "Running tests..."
python manage.py test --verbosity=2

echo ""
echo "✅ Setup complete!"
echo ""
echo "▶  Start the server:"
echo "   python manage.py runserver"
echo ""
echo "   Then open: http://localhost:8000"
