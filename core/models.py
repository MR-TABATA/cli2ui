from django.db import models


class Connection(models.Model):
    """A saved database connection.

    Stored in the local SQLite management DB. Passwords are kept in plaintext
    on purpose: this is a local-only, single-user tool (no SaaS, no shared
    server). If that assumption ever changes, encrypt this field first.
    """

    KIND_POSTGRES = "postgres"
    KIND_MYSQL = "mysql"
    KIND_CHOICES = [
        (KIND_POSTGRES, "PostgreSQL"),
        (KIND_MYSQL, "MySQL"),
    ]

    name = models.CharField(max_length=100, blank=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_POSTGRES)
    host = models.CharField(max_length=255, default="localhost")
    port = models.IntegerField(default=5432)
    dbname = models.CharField(max_length=255)
    user = models.CharField(max_length=255)
    password = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        label = self.name or self.dbname
        return f"{label} ({self.get_kind_display()} @ {self.host}:{self.port})"

    @property
    def display_name(self):
        return self.name or f"{self.dbname}@{self.host}"


class PlanSnapshot(models.Model):
    """A saved EXPLAIN plan, so you can diff "before vs after an index" instead
    of copy-pasting plans into a scratch file. Stored in the management DB."""

    connection = models.ForeignKey(
        Connection, on_delete=models.CASCADE, related_name="snapshots"
    )
    label = models.CharField(max_length=200)
    sql = models.TextField()
    plan_text = models.TextField()
    # Serialized PlanNode tree (see plan_diff.node_to_dict). Blank for snapshots
    # saved before structured diff existed — those fall back to a text diff.
    plan_json = models.TextField(blank=True, default="")
    analyzed = models.BooleanField(default=False)  # EXPLAIN ANALYZE (real timings)?
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.label


class Backup(models.Model):
    """An automatic safety snapshot taken just before a destructive operation
    (drop/truncate). The dump (pg_dump custom format) is stored as a blob in the
    management DB so the data can be recovered — restored into a NEW database, so
    an existing one is never overwritten. Snapshots above a size threshold aren't
    stored (the operation proceeds with a warning instead)."""

    KIND_TABLE = "table"
    KIND_DATABASE = "database"

    connection = models.ForeignKey(
        Connection, on_delete=models.CASCADE, related_name="backups"
    )
    operation = models.CharField(max_length=40)   # e.g. "drop table"
    kind = models.CharField(max_length=16)        # table | database
    target = models.CharField(max_length=255)     # "public.orders" or a db name
    dbname = models.CharField(max_length=255)     # source database (for context)
    data = models.BinaryField()                   # custom-format pg_dump archive
    byte_size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.operation} {self.target}"

    @property
    def pretty_size(self):
        n = self.byte_size
        if n >= 1048576:
            return f"{n / 1048576:.1f} MB"
        if n >= 1024:
            return f"{n / 1024:.0f} kB"
        return f"{n} B"


class Command(models.Model):
    """One SQL statement run through the ad-hoc runner, logged to the management
    DB so you can see (and re-open) what you ran. Read-only queries and writes
    are both recorded; the flag and status distinguish them."""

    STATUS_OK = "ok"
    STATUS_ERROR = "error"

    connection = models.ForeignKey(
        Connection, on_delete=models.CASCADE, related_name="commands"
    )
    sql = models.TextField()
    read_only = models.BooleanField(default=True)
    status = models.CharField(max_length=8, default=STATUS_OK)
    rowcount = models.IntegerField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.sql[:60]
