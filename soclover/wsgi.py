import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'soclover.settings')

import django
django.setup()

from django.db import connection
from django.core.management import call_command

tables = connection.introspection.table_names()
if 'django_migrations' not in tables:
    call_command('migrate', '--no-input')

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
