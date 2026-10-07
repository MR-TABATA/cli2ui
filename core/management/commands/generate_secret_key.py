from django.core.management.base import BaseCommand

from core import secret_store


class Command(BaseCommand):
    help = ("Print a new key for CLI2UI_SECRET_KEYS. Keep it out of the database and out of "
            "version control; with several keys, the first one encrypts.")

    def handle(self, *args, **options):
        self.stdout.write(secret_store.generate_key())
