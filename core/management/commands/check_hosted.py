"""Report whether the current settings are safe to expose (hosted mode)."""
from django.core.management.base import BaseCommand

from core import hosted


class Command(BaseCommand):
    help = "Check the hosted-mode safety preflight without starting the server."

    def handle(self, *args, **options):
        if not hosted.is_hosted():
            self.stdout.write("hosted mode is OFF (CLI2UI_HOSTED != 1): local defaults, nothing to check.")
            return
        findings = hosted.preflight()
        for x in findings:
            label = self.style.ERROR("ERROR  ") if x.level == "error" else self.style.WARNING("WARNING")
            self.stdout.write(f"{label} [{x.id}] {x.message}")
        off = sorted(hosted.disabled_capabilities())
        self.stdout.write("\nOff until named in CLI2UI_HOSTED_ALLOW: " + (", ".join(off) or "(nothing)"))
        errors = sum(x.level == "error" for x in findings)
        if errors:
            raise SystemExit(f"\n{errors} error(s): would refuse to start.")
        self.stdout.write(self.style.SUCCESS("\nOK: safe to start in hosted mode."))
