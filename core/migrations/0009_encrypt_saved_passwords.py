"""Encrypt the passwords of saved connections that are still stored as plain text.

From now on cli2ui never keeps a saved password in plain text. This writes every existing one
back encrypted (creating the key file next to the database the first time, unless
CLI2UI_SECRET_KEYS names the keys). Reversing it writes them back as plain text.
"""
from django.db import migrations

from core import secret_store


def encrypt_existing(apps, schema_editor):
    Connection = apps.get_model("core", "Connection")
    db = schema_editor.connection.alias
    for pk, stored in list(Connection.objects.using(db).values_list("pk", "password")):
        if not stored or stored.startswith(secret_store.PREFIX) or secret_store.is_reference(stored):
            continue
        # update() goes through the field, which encrypts what it is given.
        Connection.objects.using(db).filter(pk=pk).update(password=str(stored))


def decrypt_existing(apps, schema_editor):
    Connection = apps.get_model("core", "Connection")
    db = schema_editor.connection.alias
    table = Connection._meta.db_table
    for pk, stored in list(Connection.objects.using(db).values_list("pk", "password")):
        if stored and stored.startswith(secret_store.PREFIX):
            plain = secret_store.decrypt(stored)
            with schema_editor.connection.cursor() as cur:
                cur.execute(f"UPDATE {table} SET password = %s WHERE id = %s", [plain, pk])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_connection_password_secret"),
    ]

    operations = [
        migrations.RunPython(encrypt_existing, decrypt_existing),
    ]
