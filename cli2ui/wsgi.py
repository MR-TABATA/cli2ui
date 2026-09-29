import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "cli2ui.settings")

application = get_wsgi_application()

# Under gunicorn etc. no system checks run, so enforce hosted-mode preflight here.
from core import hosted  # noqa: E402

hosted.enforce()
