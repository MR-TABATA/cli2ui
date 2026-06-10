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
    analyzed = models.BooleanField(default=False)  # EXPLAIN ANALYZE (real timings)?
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.label
