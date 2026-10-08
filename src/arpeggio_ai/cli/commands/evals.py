"""`arpeggio eval`: scaffold, self-check and list tasks of the personal eval suite.

The suite lives outside the Arpeggio repo (``[evals] dir``, ``ARPEGGIO_EVALS_DIR`` or
``<home>/evals``) because its tasks come from private repositories. No command here calls
a model.
"""

import asyncio
import os
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.table import Table
from rich.text import Text

from arpeggio_ai.cli.output import JSON_HELP, console, emit_json, json_mode, run_command
from arpeggio_ai.config.loader import load_config_if_present
from arpeggio_ai.config.models import Config
from arpeggio_ai.core.logs import configure_logging
from arpeggio_ai.evals.scaffold import DEFAULT_TEST_GLOBS, NewTask, scaffold
from arpeggio_ai.evals.selfcheck import CheckRun, TaskCheck
from arpeggio_ai.evals.suite import discover, latest_statuses, select, summarize
from arpeggio_ai.evals.task import EvalTask
from arpeggio_ai.paths import arpeggio_home, artifacts_dir, evals_dir, logs_dir
from arpeggio_ai.safety.process import provider_key_names, scrubbed_env
from arpeggio_ai.safety.secret_scan import SecretScanner, scanner_from_config
from arpeggio_ai.store.artifacts import ArtifactStore

app = typer.Typer(help="Build and self-check the personal eval suite (no model calls).")

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
    json_flag: JsonOption = False,
) -> None:
    """Scaffold a task from a base commit and a solution commit."""
    as_json = json_mode(ctx, json_flag)

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
