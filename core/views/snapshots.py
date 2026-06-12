"""Saved EXPLAIN plan snapshots and their A/B diffing."""
import difflib
import json

from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from ..models import Connection, PlanSnapshot
from ..plan_diff import diff_plans, node_from_dict


def snapshot_save(request, pk):
    """Persist an EXPLAIN plan as a named snapshot, then confirm inline."""
    connection = get_object_or_404(Connection, pk=pk)
    label = (request.POST.get("label") or "").strip()
    if not label:
        label = timezone.now().strftime("plan %Y-%m-%d %H:%M:%S")
    snapshot = PlanSnapshot.objects.create(
        connection=connection,
        label=label,
        sql=request.POST.get("sql", ""),
        plan_text=request.POST.get("plan_text", ""),
        plan_json=request.POST.get("plan_json", ""),
        analyzed=request.POST.get("analyzed") == "1",
    )
    return render(request, "partials/snapshot_saved.html",
                  {"connection": connection, "snapshot": snapshot})


def snapshots(request, pk):
    """List saved plan snapshots with an A/B compare bar (htmx partial)."""
    connection = get_object_or_404(Connection, pk=pk)
    return render(
        request,
        "partials/snapshots.html",
        {"connection": connection, "snapshots": connection.snapshots.all()},
    )


def snapshot_plan(request, pk):
    """Show the exact plan text that was saved (not a re-run)."""
    connection = get_object_or_404(Connection, pk=pk)
    snap = connection.snapshots.filter(pk=request.GET.get("id")).first()
    if not snap:
        return HttpResponse("")
    return render(request, "partials/snapshot_plan.html", {"snapshot": snap})


def snapshot_delete(request, pk):
    """Delete one snapshot, then re-render the snapshots panel."""
    connection = get_object_or_404(Connection, pk=pk)
    connection.snapshots.filter(pk=request.POST.get("id")).delete()
    return render(
        request,
        "partials/snapshots.html",
        {"connection": connection, "snapshots": connection.snapshots.all()},
    )


def snapshot_diff(request, pk):
    """Diff two saved plans (A → B). Structured node-level diff when both plans
    have the JSON form; falls back to a text diff for older snapshots."""
    connection = get_object_or_404(Connection, pk=pk)
    a = connection.snapshots.filter(pk=request.GET.get("a")).first()
    b = connection.snapshots.filter(pk=request.GET.get("b")).first()
    if not a or not b:
        return render(request, "partials/snapshot_diff.html", {"missing": True})

    if a.plan_json and b.plan_json:
        diff = diff_plans(node_from_dict(json.loads(a.plan_json)),
                          node_from_dict(json.loads(b.plan_json)))
        return render(
            request,
            "partials/snapshot_diff.html",
            {"connection": connection, "a": a, "b": b,
             "structured": diff, "identical": diff.identical},
        )

    lines = []
    for line in difflib.unified_diff(
        a.plan_text.splitlines(), b.plan_text.splitlines(),
        lineterm="", n=3,
    ):
        if line.startswith(("---", "+++")):
            continue  # drop file headers; we show labels ourselves
        if line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "del"
        else:
            kind = "ctx"
        lines.append({"kind": kind, "text": line})

    return render(
        request,
        "partials/snapshot_diff.html",
        {"connection": connection, "a": a, "b": b, "lines": lines,
         "identical": not lines},
    )
