from django.core.management.base import BaseCommand, CommandError

from core import secret_store
from core.models import Connection


class Command(BaseCommand):
    help = ("Encrypt every saved connection password that is still plain text, and re-encrypt the "
            "rest with the newest key (this is how a key is rotated: put the new key in front in "
            "CLI2UI_SECRET_KEYS or the key file, run this, then drop the old key). References "
            "(secret://...) are left as they are.")

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true",
                            help="Change nothing; fail if any password is plain text or cannot be decrypted.")

    def handle(self, *args, check, **options):
        plain, unreadable, done = [], [], 0
        raw_rows = dict(Connection.objects.values_list("pk", "password"))
        for conn in Connection.objects.all():
            stored = raw_rows.get(conn.pk) or ""
            if not stored or secret_store.is_reference(stored):
                continue                    # nothing to encrypt; a reference is never resolved here
            if not stored.startswith(secret_store.PREFIX):
                plain.append(conn.pk)
                password = str(stored)      # plain text: nothing to decrypt
            else:
                try:
                    password = conn.password
                except secret_store.SecretUnavailable:
                    unreadable.append(conn.pk)
                    continue
            if not check:
                conn.password = password    # handed back as typed text, so it is written with the newest key
                conn.save(update_fields=["password"])
                done += 1
        if unreadable:
            self.stderr.write(f"Cannot decrypt connection(s) {unreadable}: no available key opens them. "
                              "Put the key they were encrypted with back into the list.")
        if check:
            if plain:
                self.stderr.write(f"Still plain text: connection(s) {plain}.")
            if plain or unreadable:
                raise CommandError("Not every saved password is encrypted and readable.")
            self.stdout.write("All saved passwords are encrypted and readable.")
            return
        self.stdout.write(f"Encrypted with the newest key: {done} password(s)"
                          f"{f'; {len(plain)} were plain text' if plain else ''}.")
        if unreadable:
            raise CommandError("Some passwords could not be re-encrypted (see above).")
