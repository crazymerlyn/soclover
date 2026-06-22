import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'soclover.settings')

application = get_wsgi_application()

# Auto-run migrations on cold start for external databases (e.g. PostgreSQL on Vercel)
from django.core.management import call_command
try:
    call_command('migrate', verbosity=0)
except Exception:
    pass
