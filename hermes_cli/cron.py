"""
Cron subcommand for hermes CLI.

Handles standalone cron management commands like list, create, edit,
pause/resume/run/remove, status, and tick.
"""

import json
import sys
from pathlib import Path
from typing import Iterable, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli.colors import Colors, color


def _normalize_skills(single_skill=None, skills: Optional[Iterable[str]] = None) -> Optional[List[str]]:
    if skills is None:
        if single_skill is None:
            return None
        raw_items = [single_skill]
    else:
        raw_items = list(skills)

    normalized: List[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _cron_api(**kwargs):
    from tools.cronjob_tools import cronjob as cronjob_tool

    return json.loads(cronjob_tool(**kwargs))


def cron_list(show_all: bool = False):
    """List all scheduled jobs."""
    from cron.jobs import list_jobs

    jobs = list_jobs(include_disabled=show_all)

    if not jobs:
        print(color("No scheduled jobs.", Colors.DIM))
        print(color("Create one with 'hermes cron create ...' or the /cron command in chat.", Colors.DIM))
        return

    print()
    print(color("┌─────────────────────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                         Scheduled Jobs                                  │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────────────────────┘", Colors.CYAN))
    print()

    for job in jobs:
        job_id = job.get("id", "?")
        name = job.get("name", "(unnamed)")
        schedule = job.get("schedule_display", job.get("schedule", {}).get("value", "?"))
        state = job.get("state", "scheduled" if job.get("enabled", True) else "paused")
        next_run = job.get("next_run_at", "?")

        repeat_info = job.get("repeat", {})
        repeat_times = repeat_info.get("times")
        repeat_completed = repeat_info.get("completed", 0)
        repeat_str = f"{repeat_completed}/{repeat_times}" if repeat_times else "∞"

        deliver = job.get("deliver", ["local"])
        if isinstance(deliver, str):
            deliver = [deliver]
        deliver_str = ", ".join(deliver)

        skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
        if state == "paused":
            status = color("[paused]", Colors.YELLOW)
        elif state == "completed":
            status = color("[completed]", Colors.BLUE)
        elif job.get("enabled", True):
            status = color("[active]", Colors.GREEN)
        else:
            status = color("[disabled]", Colors.RED)

        print(f"  {color(job_id, Colors.YELLOW)} {status}")
        print(f"    Name:      {name}")
        print(f"    Schedule:  {schedule}")
        print(f"    Repeat:    {repeat_str}")
        print(f"    Next run:  {next_run}")
        print(f"    Deliver:   {deliver_str}")
        if skills:
            print(f"    Skills:    {', '.join(skills)}")
        script = job.get("script")
        if script:
            print(f"    Script:    {script}")
        if job.get("no_agent"):
            print(f"    Mode:      {color('no-agent', Colors.DIM)} (script stdout delivered directly)")
        workdir = job.get("workdir")
        if workdir:
            print(f"    Workdir:   {workdir}")

        # Execution history
        last_status = job.get("last_status")
        if last_status:
            last_run = job.get("last_run_at", "?")
            if last_status == "ok":
                status_display = color("ok", Colors.GREEN)
            else:
                status_display = color(f"{last_status}: {job.get('last_error', '?')}", Colors.RED)
            print(f"    Last run:  {last_run}  {status_display}")

        delivery_err = job.get("last_delivery_error")
        if delivery_err:
            print(f"    {color('⚠ Delivery failed:', Colors.YELLOW)} {delivery_err}")

        print()

    from hermes_cli.gateway import find_gateway_pids
    if not find_gateway_pids():
        print(color("  ⚠  Gateway is not running — jobs won't fire automatically.", Colors.YELLOW))
        print(color("     Start it with: hermes gateway install", Colors.DIM))
        print(color("                    sudo hermes gateway install --system  # Linux servers", Colors.DIM))
        print()


def cron_tick():
    """Run due jobs once and exit."""
    from cron.scheduler import tick
    tick(verbose=True)


def _is_failure_status(status: str) -> bool:
    return status not in {"ok", "silent"}


def _build_cron_status_snapshot(jobs, rows, pids):
    """Build a compact cron reliability snapshot from job metadata + run history."""
    active_jobs = [job for job in jobs if job.get("state") not in {"paused", "completed"} and job.get("enabled", True)]
    next_runs = [job.get("next_run_at") for job in active_jobs if job.get("next_run_at")]

    status_counts = {}
    rows_by_job = {}
    for row in sorted(rows, key=lambda r: str(r.get("started_at") or ""), reverse=True):
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        job_id = row.get("job_id")
        if job_id:
            rows_by_job.setdefault(job_id, []).append(row)

    job_summaries = []
    for job in sorted(jobs, key=lambda j: (str(j.get("name") or ""), str(j.get("id") or ""))):
        job_rows = rows_by_job.get(job.get("id"), [])
        last_row = job_rows[0] if job_rows else None
        last_success = next((row for row in job_rows if not _is_failure_status(str(row.get("status") or ""))), None)
        last_failure = next((row for row in job_rows if _is_failure_status(str(row.get("status") or ""))), None)
        consecutive_failures = 0
        for row in job_rows:
            if _is_failure_status(str(row.get("status") or "")):
                consecutive_failures += 1
            else:
                break
        durations = [int(row.get("duration_ms") or 0) for row in job_rows if row.get("duration_ms") is not None]
        job_summaries.append(
            {
                "job_id": job.get("id"),
                "name": job.get("name"),
                "state": job.get("state", "scheduled" if job.get("enabled", True) else "paused"),
                "next_run_at": job.get("next_run_at"),
                "history_count": len(job_rows),
                "last_run_at": last_row.get("started_at") if last_row else job.get("last_run_at"),
                "last_status": last_row.get("status") if last_row else job.get("last_status"),
                "last_success_at": last_success.get("started_at") if last_success else None,
                "last_failure_at": last_failure.get("started_at") if last_failure else None,
                "consecutive_failures": consecutive_failures,
                "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None,
                "last_duration_ms": last_row.get("duration_ms") if last_row else None,
                "last_output_bytes": last_row.get("output_bytes") if last_row else None,
                "delivery_failures": sum(1 for row in job_rows if row.get("status") == "delivery_error"),
                "silent_runs": sum(1 for row in job_rows if row.get("status") == "silent"),
                "empty_response_runs": sum(1 for row in job_rows if row.get("response_status") == "empty"),
            }
        )

    return {
        "gateway_running": bool(pids),
        "gateway_pids": list(pids),
        "active_jobs": len(active_jobs),
        "total_jobs": len(jobs),
        "next_run_at": min(next_runs) if next_runs else None,
        "history_count": len(rows),
        "status_counts": status_counts,
        "jobs": job_summaries,
    }


def _status_color(status: str):
    if status in {"ok", "silent"}:
        return Colors.GREEN if status == "ok" else Colors.DIM
    if status == "delivery_error":
        return Colors.YELLOW
    return Colors.RED


def cron_status(args=None):
    """Show cron scheduler and recent execution health."""
    from cron.jobs import list_jobs, list_run_history
    from hermes_cli.gateway import find_gateway_pids

    limit = int(getattr(args, "history_limit", 500) or 500)
    pids = find_gateway_pids()
    jobs = list_jobs(include_disabled=True)
    rows = list_run_history(limit=limit)
    snapshot = _build_cron_status_snapshot(jobs, rows, pids)

    if getattr(args, "json", False):
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0

    print()

    if pids:
        print(color("✓ Gateway is running — cron jobs will fire automatically", Colors.GREEN))
        print(f"  PID: {', '.join(map(str, pids))}")
    else:
        print(color("✗ Gateway is not running — cron jobs will NOT fire", Colors.RED))
        print()
        print("  To enable automatic execution:")
        print("    hermes gateway install    # Install as a user service")
        print("    sudo hermes gateway install --system  # Linux servers: boot-time system service")
        print("    hermes gateway            # Or run in foreground")

    print()
    print(f"  {snapshot['active_jobs']} active job(s), {snapshot['total_jobs']} total job(s)")
    if snapshot.get("next_run_at"):
        print(f"  Next run: {snapshot['next_run_at']}")
    print(f"  Recent run rows sampled: {snapshot['history_count']}")
    if snapshot["status_counts"]:
        counts = ", ".join(f"{status}={count}" for status, count in sorted(snapshot["status_counts"].items()))
        print(f"  Recent statuses: {counts}")

    if snapshot["jobs"]:
        print()
        print(color("  Job health", Colors.CYAN))
        for summary in snapshot["jobs"]:
            status = str(summary.get("last_status") or "never")
            status_display = color(status, _status_color(status)) if status != "never" else color(status, Colors.DIM)
            failures = summary.get("consecutive_failures") or 0
            failure_text = f", consecutive failures: {failures}" if failures else ""
            print(f"  - {summary.get('name') or summary.get('job_id')} ({summary.get('job_id')})")
            print(f"      state={summary.get('state')} last={status_display} at {summary.get('last_run_at') or '-'}{failure_text}")
            if summary.get("last_success_at"):
                print(f"      last success: {summary.get('last_success_at')}")
            if summary.get("last_failure_at"):
                print(f"      last failure: {summary.get('last_failure_at')}")
            if summary.get("avg_duration_ms") is not None:
                print(
                    f"      avg duration: {_fmt_duration_ms(summary.get('avg_duration_ms'))}; "
                    f"last output: {summary.get('last_output_bytes') if summary.get('last_output_bytes') is not None else '-'} bytes"
                )
            if summary.get("delivery_failures") or summary.get("silent_runs") or summary.get("empty_response_runs"):
                print(
                    "      flags: "
                    f"delivery_failures={summary.get('delivery_failures')}, "
                    f"silent={summary.get('silent_runs')}, "
                    f"empty_response={summary.get('empty_response_runs')}"
                )
    else:
        print()
        print("  No configured jobs")

    print()
    return 0


def cron_create(args):
    result = _cron_api(
        action="create",
        schedule=args.schedule,
        prompt=args.prompt,
        name=getattr(args, "name", None),
        deliver=getattr(args, "deliver", None),
        repeat=getattr(args, "repeat", None),
        skill=getattr(args, "skill", None),
        skills=_normalize_skills(getattr(args, "skill", None), getattr(args, "skills", None)),
        script=getattr(args, "script", None),
        workdir=getattr(args, "workdir", None),
        no_agent=getattr(args, "no_agent", False) or None,
    )
    if not result.get("success"):
        print(color(f"Failed to create job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1
    print(color(f"Created job: {result['job_id']}", Colors.GREEN))
    print(f"  Name: {result['name']}")
    print(f"  Schedule: {result['schedule']}")
    if result.get("skills"):
        print(f"  Skills: {', '.join(result['skills'])}")
    job_data = result.get("job", {})
    if job_data.get("script"):
        print(f"  Script: {job_data['script']}")
    if job_data.get("no_agent"):
        print("  Mode: no-agent (script stdout delivered directly)")
    if job_data.get("workdir"):
        print(f"  Workdir: {job_data['workdir']}")
    print(f"  Next run: {result['next_run_at']}")
    return 0


def cron_edit(args):
    from cron.jobs import get_job

    job = get_job(args.job_id)
    if not job:
        print(color(f"Job not found: {args.job_id}", Colors.RED))
        return 1

    existing_skills = list(job.get("skills") or ([] if not job.get("skill") else [job.get("skill")]))
    replacement_skills = _normalize_skills(getattr(args, "skill", None), getattr(args, "skills", None))
    add_skills = _normalize_skills(None, getattr(args, "add_skills", None)) or []
    remove_skills = set(_normalize_skills(None, getattr(args, "remove_skills", None)) or [])

    final_skills = None
    if getattr(args, "clear_skills", False):
        final_skills = []
    elif replacement_skills is not None:
        final_skills = replacement_skills
    elif add_skills or remove_skills:
        final_skills = [skill for skill in existing_skills if skill not in remove_skills]
        for skill in add_skills:
            if skill not in final_skills:
                final_skills.append(skill)

    result = _cron_api(
        action="update",
        job_id=args.job_id,
        schedule=getattr(args, "schedule", None),
        prompt=getattr(args, "prompt", None),
        name=getattr(args, "name", None),
        deliver=getattr(args, "deliver", None),
        repeat=getattr(args, "repeat", None),
        skills=final_skills,
        script=getattr(args, "script", None),
        workdir=getattr(args, "workdir", None),
        no_agent=getattr(args, "no_agent", None),
    )
    if not result.get("success"):
        print(color(f"Failed to update job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1

    updated = result["job"]
    print(color(f"Updated job: {updated['job_id']}", Colors.GREEN))
    print(f"  Name: {updated['name']}")
    print(f"  Schedule: {updated['schedule']}")
    if updated.get("skills"):
        print(f"  Skills: {', '.join(updated['skills'])}")
    else:
        print("  Skills: none")
    if updated.get("script"):
        print(f"  Script: {updated['script']}")
    if updated.get("no_agent"):
        print("  Mode: no-agent (script stdout delivered directly)")
    if updated.get("workdir"):
        print(f"  Workdir: {updated['workdir']}")
    return 0


def _job_action(action: str, job_id: str, success_verb: str) -> int:
    result = _cron_api(action=action, job_id=job_id)
    if not result.get("success"):
        print(color(f"Failed to {action} job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1
    job = result.get("job") or result.get("removed_job") or {}
    print(color(f"{success_verb} job: {job.get('name', job_id)} ({job_id})", Colors.GREEN))
    if action in {"resume", "run"} and result.get("job", {}).get("next_run_at"):
        print(f"  Next run: {result['job']['next_run_at']}")
    if action == "run":
        print("  It will run on the next scheduler tick.")
    return 0


def _fmt_duration_ms(ms) -> str:
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        return "-"
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def cron_history(args):
    """Show recent cron run history."""
    from datetime import timedelta
    from hermes_time import now as _hermes_now
    from cron.jobs import list_run_history

    limit = getattr(args, "limit", 20) or 20
    job_id = getattr(args, "job_id", None)
    status = getattr(args, "status", None)
    since = getattr(args, "since", None)
    days = getattr(args, "days", None)
    if days and not since:
        since = (_hermes_now() - timedelta(days=int(days))).isoformat()

    rows = list_run_history(job_id=job_id, limit=limit, since=since, status=status)

    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print()
    print(color("┌─────────────────────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                         Cron Run History                                │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────────────────────┘", Colors.CYAN))
    print()

    if not rows:
        print(color("No cron run history yet.", Colors.DIM))
        print()
        return 0

    for row in rows:
        status_value = str(row.get("status") or "?")
        if status_value == "ok":
            status_display = color(status_value, Colors.GREEN)
        elif status_value == "silent":
            status_display = color(status_value, Colors.DIM)
        elif status_value == "delivery_error":
            status_display = color(status_value, Colors.YELLOW)
        else:
            status_display = color(status_value, Colors.RED)
        print(f"  {row.get('started_at', '-')}  {status_display}  {row.get('job_name') or row.get('job_id', '?')}")
        print(f"    Job:        {row.get('job_id', '-')}")
        if row.get("scheduled_for"):
            print(f"    Scheduled:  {row.get('scheduled_for')}")
        if row.get("due_lag_ms") is not None:
            print(f"    Due lag:    {_fmt_duration_ms(row.get('due_lag_ms'))}")
        print(f"    Duration:   {_fmt_duration_ms(row.get('duration_ms'))}")
        print(f"    Execution:  {row.get('execution_status', '-')}")
        print(f"    Response:   {row.get('response_status', '-')}")
        print(f"    Delivery:   {row.get('delivery_status', '-')}")
        print(f"    Output:     {row.get('output_bytes', 0)} bytes  {row.get('output_path') or '-'}")
        if row.get("error"):
            print(f"    Error:      {row.get('error')}")
        if row.get("delivery_error"):
            print(f"    Delivery error: {row.get('delivery_error')}")
        print()
    return 0


def cron_command(args):
    """Handle cron subcommands."""
    subcmd = getattr(args, 'cron_command', None)

    if subcmd is None or subcmd == "list":
        show_all = getattr(args, 'all', False)
        cron_list(show_all)
        return 0

    if subcmd == "status":
        return cron_status(args)

    if subcmd == "tick":
        cron_tick()
        return 0

    if subcmd == "history":
        return cron_history(args)

    if subcmd in {"create", "add"}:
        return cron_create(args)

    if subcmd == "edit":
        return cron_edit(args)

    if subcmd == "pause":
        return _job_action("pause", args.job_id, "Paused")

    if subcmd == "resume":
        return _job_action("resume", args.job_id, "Resumed")

    if subcmd == "run":
        return _job_action("run", args.job_id, "Triggered")

    if subcmd in {"remove", "rm", "delete"}:
        return _job_action("remove", args.job_id, "Removed")

    print(f"Unknown cron command: {subcmd}")
    print("Usage: hermes cron [list|create|edit|pause|resume|run|remove|status|tick|history]")
    sys.exit(1)
