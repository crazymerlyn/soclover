#!/bin/bash
# So Clover Online — Quick Setup Script
set -e

echo "🍀 Setting up So Clover Online..."

# Install dependencies
pip install -r requirements.txt --quiet

# Create database & run migrations
python manage.py makemigrations game
python manage.py migrate

echo ""
echo "✅ Setup complete!"
echo ""
echo "▶  Start the server:"
echo "   python manage.py runserver"
echo ""
echo "   Then open: http://localhost:8000"
