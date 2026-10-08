"""`arpeggio eval`: build, self-check and run the personal eval suite, and report on it.

The suite lives outside the Arpeggio repo (``[evals] dir``, ``ARPEGGIO_EVALS_DIR`` or
``<home>/evals``) because its tasks come from private repositories. Only ``eval run``
without ``--dry-run`` calls models, and it spends money within an explicit cap (M0.6).
"""

import asyncio
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import httpx
import typer
from rich.table import Table
from rich.text import Text

from arpeggio_ai.adapters.base import AdapterContext
from arpeggio_ai.cli.output import (
    EXIT_CONFIG,
    JSON_HELP,
    console,
    emit_json,
    fail,
    json_mode,
    run_command,
)
from arpeggio_ai.config.loader import load_config, load_config_if_present
from arpeggio_ai.config.models import Config
from arpeggio_ai.core.logs import configure_logging
from arpeggio_ai.evals.budget import (
    BudgetError,
    SpendCap,
    check_estimate,
    peak_message,
    peak_notices,
    run_limit,
)
from arpeggio_ai.evals.report import (
    DEFAULT_SEED,
    FAILURE_KINDS,
    Report,
    build_report,
    render_markdown,
)
from arpeggio_ai.evals.runner import EvalRunner, RunPlan, build_plan, config_hash, harness_sha
from arpeggio_ai.evals.scaffold import DEFAULT_TEST_GLOBS, NewTask, scaffold
from arpeggio_ai.evals.selfcheck import CheckRun, TaskCheck
from arpeggio_ai.evals.strategies import REFERENCE, STRATEGIES, Strategy
from arpeggio_ai.evals.suite import REPORTS_DIR, discover, latest_statuses, select, summarize
from arpeggio_ai.evals.task import Category, EvalTask, Risk
from arpeggio_ai.evals.textcheck import TextCheck
from arpeggio_ai.orchestrator.attempts import overhead_history
from arpeggio_ai.paths import arpeggio_home, artifacts_dir, db_path, evals_dir, logs_dir
from arpeggio_ai.routing.policy import load_policy
from arpeggio_ai.safety.process import provider_key_names, scrubbed_env
from arpeggio_ai.safety.secret_scan import SecretScanner, scanner_from_config
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.store.db import open_db
from arpeggio_ai.store.repositories import list_eval_results, list_eval_runs, month_eval_spend_usd

app = typer.Typer(help="Build, self-check and run the personal eval suite, and report on it.")

JsonOption = Annotated[bool, typer.Option("--json", help=JSON_HELP)]
SplitOption = Annotated[
    Literal["tuning", "holdout", "all"],
    typer.Option("--split", help="Which split to use."),
]


@app.callback(invoke_without_command=True)
def eval_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


def suite_root() -> Path:
    config = load_config_if_present()
    return evals_dir(config.evals.dir if config is not None else None)


def _env(config: Config | None) -> dict[str, str]:
    if config is None:
        return scrubbed_env()
    return scrubbed_env(extra=config.repo.check_env, deny=provider_key_names(config))


def _scanner(config: Config | None) -> SecretScanner:
    return SecretScanner() if config is None else scanner_from_config(config, os.environ)


@app.command("new")
def new_command(
    ctx: typer.Context,
    repo: Annotated[Path, typer.Option("--repo", help="Root of the local git repository.")],
    base: Annotated[str, typer.Option("--base", help="Commit before the change (SHA).")],
    solution: Annotated[str, typer.Option("--solution", help="Commit with your solution.")],
    task_id: Annotated[str, typer.Option("--id", help="Task ID, also the file name.")],
    holdout: Annotated[
        bool, typer.Option("--holdout", help="Put it in the holdout split.")
    ] = False,
    tests_glob: Annotated[
        list[str] | None,
        typer.Option(
            "--tests-glob",
            help="Glob for test files that become hidden tests (repeatable). "
            f"Default: {', '.join(DEFAULT_TEST_GLOBS)}.",
        ),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing task.")] = False,
    request: Annotated[
        str | None, typer.Option("--request", help="The task as you would ask for it.")
    ] = None,
    check_file: Annotated[
        str | None,
        typer.Option("--check-file", help="Repo file a generated hidden test checks (POSIX path)."),
    ] = None,
    check_pattern: Annotated[
        str | None,
        typer.Option(
            "--check-pattern",
            help="Regex that must match --check-file at the solution and not at base"
            " (JavaScript RegExp at run time).",
        ),
    ] = None,
    check_name: Annotated[
        str | None, typer.Option("--check-name", help="Name of the generated test.")
    ] = None,
    category: Annotated[Category, typer.Option("--category", help="Task category.")] = "other",
    risk: Annotated[Risk, typer.Option("--risk", help="Expected risk.")] = "low",
    json_flag: JsonOption = False,
) -> None:
    """Scaffold a task from a base commit and a solution commit."""
    as_json = json_mode(ctx, json_flag)
    if (check_file is None) != (check_pattern is None):
        fail(
            [
                {
                    "file": None,
                    "field": None,
                    "message": "give --check-file and --check-pattern together",
                }
            ],
            as_json,
            EXIT_CONFIG,
        )
    text_check = (
        None
        if check_file is None or check_pattern is None
        else TextCheck(check_file.replace("\\", "/"), check_pattern, check_name)
    )

    def action() -> NewTask:
        config = load_config_if_present(repo)
        root = evals_dir(config.evals.dir if config is not None else None)
        return asyncio.run(
            scaffold(
                repo,
                base,
                solution,
                task_id,
                root,
                home=arpeggio_home(),
                env=_env(config),
                scanner=_scanner(config),
                holdout=holdout,
                test_globs=tuple(tests_glob) if tests_glob else DEFAULT_TEST_GLOBS,
                force=force,
                request=request,
                category=category,
                risk=risk,
                text_check=text_check,
            )
        )

    result = run_command(as_json, action)
    if as_json:
        emit_json({"ok": True, **result.to_dict()})
        return
    out = console()
    out.print(f"Created {result.split} task {result.id}", style="bold green")
    out.print(Text.assemble(("file:   ", "dim"), str(result.task_file)))
    out.print(f"hidden tests: {len(result.hidden_tests)}", markup=False)
    for path in result.hidden_tests:
        out.print(f"  {path}", markup=False)
    out.print(f"reference diff: {result.reference_diff or 'none'}", markup=False)
    findings = ", ".join(f"{k} {v}" for k, v in result.findings.items()) or "none"
    out.print(f"secret scan: {result.files_scanned} files, findings: {findings}", markup=False)
    if text_check is not None and request is not None:
        out.print(f"Next: run `arpeggio eval check --id {result.id}`.", markup=False)
    else:
        out.print("Next: fill in `request` and `done_criteria`, then run `arpeggio eval check`.")


@app.command("check")
def check_command(
    ctx: typer.Context,
    split: SplitOption = "all",
    task_ids: Annotated[
        list[str] | None, typer.Option("--id", help="Only these task IDs (repeatable).")
    ] = None,
    keep_worktrees: Annotated[
        bool, typer.Option("--keep-worktrees", help="Keep worktrees for debugging.")
    ] = False,
    json_flag: JsonOption = False,
) -> None:
    """Self-check tasks: criteria must fail at base and pass with the solution."""
    as_json = json_mode(ctx, json_flag)

    def action() -> tuple[list[TaskCheck], Path]:
        root = suite_root()
        entries = select(discover(root), split, task_ids)
        if task_ids:
            missing = sorted(set(task_ids) - {entry.id for entry in entries})
            if missing:
                raise ValueError(f"unknown task ids: {', '.join(missing)}")
        home = arpeggio_home()
        configure_logging(logs_dir(home))

        def config_for(task: EvalTask) -> Config | None:
            return load_config_if_present(Path(task.repo.path))

        run = CheckRun(
            evals_root=root,
            home=home,
            artifacts=ArtifactStore(artifacts_dir(home)),
            env_for=lambda task: _env(config_for(task)),
            scanner_for=lambda task: _scanner(config_for(task)),
            keep_worktrees=keep_worktrees,
        )

        async def check_all() -> list[TaskCheck]:
            return [await run.check(entry, index) for index, entry in enumerate(entries)]

        checks = asyncio.run(check_all())
        return checks, run.write_report(checks)

    checks, report = run_command(as_json, action)
    ok = all(check.status == "valid" for check in checks)
    if as_json:
        emit_json({"ok": ok, "report": str(report), "tasks": [check.to_dict() for check in checks]})
    else:
        out = console()
        table = Table("id", "split", "status", "base failing", "solution failing", "detail")
        for check in checks:
            table.add_row(
                check.id,
                check.split,
                Text(check.status, style="green" if check.status == "valid" else "red"),
                "-" if check.base is None else str(check.base.failing),
                "-" if check.solution is None else str(check.solution.failing),
                check.detail or "",
            )
        out.print(table)
        valid = sum(1 for check in checks if check.status == "valid")
        out.print(f"{valid}/{len(checks)} valid. Report: {report}", markup=False)
    if not ok:
        raise typer.Exit(1)


@app.command("list")
def list_command(
    ctx: typer.Context, split: SplitOption = "all", json_flag: JsonOption = False
) -> None:
    """List tasks with their latest self-check status and the split balance."""
    as_json = json_mode(ctx, json_flag)

    def action() -> tuple[Path, list[dict[str, object]], dict[str, object]]:
        root = suite_root()
        entries = discover(root)
        summary = summarize(entries)
        statuses = latest_statuses(root)
        rows: list[dict[str, object]] = []
        for entry in select(entries, split):
            task = entry.task
            rows.append(
                {
                    "id": entry.id,
                    "split": entry.split,
                    "category": None if task is None else task.category,
                    "expected_risk": None if task is None else task.expected_risk,
                    "tags": [] if task is None else task.tags,
                    "status": statuses.get(entry.id, "unchecked")
                    if task is not None
                    else "invalid_schema",
                }
            )
        footer: dict[str, object] = {
            "tuning": summary.tuning,
            "holdout": summary.holdout,
            "total": summary.total,
            "holdout_share": round(summary.holdout_share, 4),
            "warnings": summary.warnings,
        }
        return root, rows, footer

    root, rows, footer = run_command(as_json, action)
    if as_json:
        emit_json({"ok": True, "evals_dir": str(root), "tasks": rows, "summary": footer})
        return
    out = console()
    out.print(Text.assemble(("evals: ", "dim"), str(root)))
    table = Table("id", "split", "category", "risk", "tags", "status")
    for row in rows:
        tags = row["tags"]
        assert isinstance(tags, list)
        table.add_row(
            str(row["id"]),
            str(row["split"]),
            str(row["category"] or "-"),
            str(row["expected_risk"] or "-"),
            ", ".join(tags),
            str(row["status"]),
        )
    out.print(table)
    share = footer["holdout_share"]
    assert isinstance(share, float | int)
    out.print(
        f"tuning {footer['tuning']}, holdout {footer['holdout']}, total {footer['total']}, "
        f"holdout share {share:.0%}",
        markup=False,
    )
    warnings = footer["warnings"]
    assert isinstance(warnings, list)
    for warning in warnings:
        out.print(Text.assemble(("warning: ", "bold yellow"), str(warning)))


# Baseline experiment (M0.6): run, report, runs

StrategyOption = Annotated[
    Literal["junior", "middle", "senior", "arpeggio", "all"],
    typer.Option("--strategy", help="Strategy to run, or all four."),
]
# Tests replace these: the clock (peak windows) and the network transport.
TRANSPORT: httpx.AsyncBaseTransport | None = None


def now_utc() -> datetime:
    return datetime.now(UTC)


def _arpeggio_checkout() -> Path | None:
    root = Path(__file__).resolve().parents[4]
    return root if (root / "pyproject.toml").is_file() and (root / "src").is_dir() else None


def _strategies(choice: str) -> tuple[Strategy, ...]:
    return STRATEGIES if choice == "all" else (cast(Strategy, choice),)


def _plan_lines(plan: RunPlan) -> list[str]:
    lines = []
    for task_plan in plan.tasks:
        for strategy, strategy_plan in task_plan.strategies.items():
            if strategy_plan.skip:
                lines.append(f"  {task_plan.task.id} / {strategy}: skipped ({strategy_plan.skip})")
                continue
            routes = " -> ".join(
                f"{r.model}@{r.effort}" + (f" [{r.reason['rule']}]" if "rule" in r.reason else "")
                for r in strategy_plan.routes
            )
            lines.append(
                f"  {task_plan.task.id} / {strategy}: {routes}"
                f" (worst case ${strategy_plan.estimate_usd:.6f})"
            )
    return lines


@app.command("run")
def run_command_(
    ctx: typer.Context,
    strategy: StrategyOption = "all",
    split: SplitOption = "holdout",
    task_ids: Annotated[
        list[str] | None, typer.Option("--id", help="Only these task IDs (repeatable).")
    ] = None,
    repeats: Annotated[
        int, typer.Option("--repeats", min=1, help="Runs per task; 3 or more is recommended.")
    ] = 1,
    max_usd: Annotated[
        float | None, typer.Option("--max-usd", min=0, help="Spend cap for this run in USD.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan and estimate; call no model.")
    ] = False,
    allow_peak: Annotated[
        bool, typer.Option("--allow-peak", help="Run even when a provider is at peak prices.")
    ] = False,
    keep_worktrees: Annotated[
        bool, typer.Option("--keep-worktrees", help="Keep attempt worktrees for debugging.")
    ] = False,
    sleep_between: Annotated[
        float, typer.Option("--sleep-between", min=0, help="Seconds to wait between attempts.")
    ] = 0.0,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Use this file as the global config for this run (read only), for example"
            " to run another profile into the same database.",
        ),
    ] = None,
    json_flag: JsonOption = False,
) -> None:
    """Run the baseline strategies on the eval suite (spends money unless --dry-run)."""
    as_json = json_mode(ctx, json_flag)

    def action() -> dict[str, Any]:
        global_config = load_config(global_path=config_file)
        root = evals_dir(global_config.evals.dir)
        entries = select(discover(root), split, task_ids)
        if task_ids:
            missing = sorted(set(task_ids) - {entry.id for entry in entries})
            if missing:
                raise ValueError(f"unknown task ids: {', '.join(missing)}")
        home = arpeggio_home()
        configure_logging(logs_dir(home))
        policy, source = load_policy(home)
        at = now_utc()
        conn = open_db(db_path(home)) if (home.is_dir() or not dry_run) else None
        if conn is None:
            overhead: Callable[[str, str], int] = lambda provider, model: 0  # noqa: E731
        else:
            overhead = overhead_history(conn)
        try:
            plan = asyncio.run(
                build_plan(
                    entries,
                    evals_root=root,
                    strategies=_strategies(strategy),
                    split=split,
                    repeats=repeats,
                    config_for=lambda repo: load_config(repo, global_path=config_file),
                    policy=policy,
                    policy_source=source,
                    home=home,
                    at=at,
                    overhead=overhead,
                )
            )
            notices = peak_notices(plan.route_keys(), at)
            month = f"{at:%Y-%m}"
            spent = 0.0 if conn is None else month_eval_spend_usd(conn, month)
            monthly = global_config.budget.eval_per_month_usd
            limit: float | None = None
            if max_usd is not None or monthly is not None:
                limit = run_limit(max_usd, monthly, spent)
            summary: dict[str, Any] = {
                "profile": global_config.budget.profile,
                "evals_dir": str(root),
                "plan": plan.to_dict(),
                "estimate_usd": plan.estimate_usd,
                "limit_usd": limit,
                "spent_this_month_usd": round(spent, 8),
                "peak": [
                    {
                        "provider": n.provider,
                        "offpeak_at": None if n.offpeak_at is None else n.offpeak_at.isoformat(),
                    }
                    for n in notices
                ],
                "peak_message": peak_message(notices) if notices else None,
                "dry_run": dry_run,
                "plan_lines": _plan_lines(plan),
            }
            if dry_run:
                return summary
            if not plan.tasks:
                raise BudgetError("nothing to run: no selected task has a current valid self-check")
            limit = run_limit(max_usd, monthly, spent)
            check_estimate(plan.estimate_usd, limit)
            if notices and not allow_peak:
                raise BudgetError(peak_message(notices))
            assert conn is not None

            def context_for(config: Config) -> AdapterContext:
                return AdapterContext(
                    config=config, transport=TRANSPORT, prompt_overhead=overhead_history(conn)
                )

            env = scrubbed_env()
            runner = EvalRunner(
                conn,
                ArtifactStore(artifacts_dir(home)),
                home=home,
                evals_root=root,
                context_for=context_for,
                cap=SpendCap(limit),
                git_sha=asyncio.run(harness_sha(env)),
                config_hash=config_hash(global_config),
                profile=global_config.budget.profile,
                keep_worktrees=keep_worktrees,
                sleep_between_s=sleep_between,
            )
            outcome = asyncio.run(runner.run(plan))
            summary.update(
                status=outcome.status,
                reason=outcome.reason,
                spent_usd=round(outcome.spent_usd, 8),
                runs={s: run.id for s, run in outcome.runs.items()},
            )
            return summary
        finally:
            if conn is not None:
                conn.close()

    summary = run_command(as_json, action)
    if as_json:
        emit_json({"ok": True, **{k: v for k, v in summary.items() if k != "plan_lines"}})
        return
    out = console()
    plan = summary["plan"]
    out.print(
        f"profile {summary['profile']}, split {plan['split']}, strategies"
        f" {', '.join(plan['strategies'])}, repeats {plan['repeats']}:"
        f" {plan['planned_task_runs']} task runs per strategy",
        markup=False,
    )
    for line in summary["plan_lines"]:
        out.print(line, markup=False)
    for skipped in plan["skipped"]:
        out.print(f"  skipped {skipped['id']}: {skipped['message']}", markup=False, style="yellow")
    limit_text = "none" if summary["limit_usd"] is None else f"${summary['limit_usd']:.6f}"
    out.print(
        f"worst-case estimate ${summary['estimate_usd']:.6f}, limit {limit_text}"
        f" (spent on evals this month ${summary['spent_this_month_usd']:.6f})",
        markup=False,
    )
    if summary["peak_message"]:
        out.print(Text.assemble(("peak: ", "bold yellow"), summary["peak_message"]))
    if summary["dry_run"]:
        out.print("Dry run: no model was called.")
        return
    style = "green" if summary["status"] == "completed" else "bold yellow"
    out.print(
        f"run {summary['status']}: spent ${summary['spent_usd']:.6f}", style=style, markup=False
    )
    if summary["reason"]:
        out.print(f"PARTIAL: {summary['reason']}", style="bold yellow", markup=False)
    out.print("Next: `arpeggio eval report` for the numbers with confidence intervals.")


def _fmt_ci(ci: dict[str, Any], money: bool) -> str:
    if ci["low"] is None:
        return "n/a"
    low, high = float(ci["low"]), float(ci["high"])
    text = f"${low:.4f}-${high:.4f}" if money else f"{low:.0%}-{high:.0%}"
    return text + " UNRELIABLE" if ci["unreliable"] else text


@app.command("report")
def report_command(
    ctx: typer.Context,
    run_ids: Annotated[
        list[str] | None, typer.Argument(help="Eval run IDs; default: latest per strategy.")
    ] = None,
    markdown: Annotated[
        Path | None,
        typer.Option(
            "--markdown", help="Markdown file (default <evals>/.reports/baseline-<ts>.md)."
        ),
    ] = None,
    seed: Annotated[int, typer.Option("--seed", help="Bootstrap seed.")] = DEFAULT_SEED,
    json_flag: JsonOption = False,
) -> None:
    """Compare strategies per profile with 95% bootstrap intervals; writes Markdown."""
    as_json = json_mode(ctx, json_flag)

    def action() -> tuple[Report, Path]:
        home = arpeggio_home()
        if not db_path(home).exists():
            raise ValueError("no database yet: run `arpeggio eval run` first")
        root = suite_root()
        target = markdown or root / REPORTS_DIR / f"baseline-{now_utc():%Y%m%dT%H%M%SZ}.md"
        target = target.expanduser().absolute()
        checkout = _arpeggio_checkout()
        if checkout is not None and target.resolve().is_relative_to(checkout):
            raise ValueError(
                f"reports never go into the Arpeggio repo ({checkout}); write them under {root}"
            )
        conn = open_db(db_path(home))
        try:
            report = build_report(conn, run_ids or [], seed=seed)
        finally:
            conn.close()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_markdown(report), encoding="utf-8", newline="\n")
        return report, target

    report, target = run_command(as_json, action)
    if as_json:
        emit_json({"ok": True, "markdown": str(target), **report.to_dict()})
        return
    out = console()
    for section in report.sections:
        out.print(
            f"\nprofile {section.profile}, split {section.split}: {section.label},"
            f" {section.tasks} tasks",
            style="bold",
            markup=False,
        )
        for warning in section.warnings:
            out.print(Text.assemble(("warning: ", "bold yellow"), warning))
        table = Table(
            "strategy",
            "runs",
            "solved",
            "success",
            "95% CI",
            "cost",
            "cost/solved",
            "95% CI",
            "attempts",
            "esc.",
            "wall",
            "quota wait",
            "counterfactual",
        )
        for m in section.strategies:
            data = asdict(m)
            table.add_row(
                m.strategy + (" PARTIAL" if m.partial_reasons else ""),
                str(m.task_runs),
                str(m.solved),
                "n/a" if m.success_rate is None else f"{m.success_rate:.0%}",
                _fmt_ci(data["success_ci"], money=False),
                f"${m.total_cost_usd:.4f}",
                "n/a" if m.cost_per_solved_usd is None else f"${m.cost_per_solved_usd:.4f}",
                _fmt_ci(data["cost_per_solved_ci"], money=True),
                "n/a" if m.mean_attempts is None else f"{m.mean_attempts:.2f}",
                str(m.escalations),
                f"{m.wall_clock_s:.0f}s",
                f"{m.quota_wait_s:.0f}s",
                f"${m.counterfactual_usd:.4f}",
            )
        out.print(table)
        outcomes = Table(
            "strategy", *FAILURE_KINDS, "reasoning share", "redactions", title="attempt outcomes"
        )
        for m in section.strategies:
            share = "n/a" if m.reasoning_share is None else f"{m.reasoning_share:.0%}"
            counts = (str(m.failures[k]) for k in FAILURE_KINDS)
            outcomes.add_row(m.strategy, *counts, share, str(m.redactions))
        out.print(outcomes)
        for p in section.paired:
            data = asdict(p)
            success = "n/a" if p.success_diff is None else f"{p.success_diff * 100:+.0f} pp"
            cps = (
                "n/a"
                if p.cost_per_solved_diff_usd is None
                else f"{p.cost_per_solved_diff_usd:+.4f} USD"
            )
            out.print(
                f"  {p.strategy} vs {REFERENCE} on {p.tasks} tasks: success {success}"
                f" (CI {_fmt_ci(data['success_ci'], money=False)}), cost/solved {cps}"
                f" (CI {_fmt_ci(data['cost_per_solved_ci'], money=True)})",
                markup=False,
            )
    out.print(f"\nMarkdown: {target}", markup=False)


@app.command("runs")
def runs_command(ctx: typer.Context, json_flag: JsonOption = False) -> None:
    """List recorded eval runs."""
    as_json = json_mode(ctx, json_flag)

    def action() -> list[dict[str, Any]]:
        path = db_path(arpeggio_home())
        if not path.exists():
            return []
        conn = open_db(path)
        try:
            rows = []
            for run in list_eval_runs(conn):
                results = list_eval_results(conn, run.id)
                done = [r for r in results if r.status != "skipped"]
                rows.append(
                    {
                        "id": run.id,
                        "strategy": run.strategy,
                        "profile": run.profile,
                        "split": run.split,
                        "status": run.status,
                        "status_reason": run.status_reason,
                        "started_at": run.started_at,
                        "task_runs": len(done),
                        "solved": sum(r.solved for r in done),
                        "skipped": len(results) - len(done),
                        "cost_usd": round(sum(r.cost_usd for r in done), 8),
                    }
                )
            return rows
        finally:
            conn.close()

    rows = run_command(as_json, action)
    if as_json:
        emit_json({"ok": True, "runs": rows})
        return
    table = Table(
        "id", "strategy", "profile", "split", "status", "started", "runs", "solved", "cost"
    )
    for row in rows:
        table.add_row(
            row["id"],
            row["strategy"],
            row["profile"],
            row["split"],
            row["status"],
            row["started_at"],
            str(row["task_runs"]),
            str(row["solved"]),
            f"${row['cost_usd']:.4f}",
        )
    console().print(table)
