"""`arpeggio secrets scan`: a dry run of the secret scanner over files (SAF-02).

It measures false positives before you rely on redaction in patch mode. The report says
where findings are (file, line, type) and never what they are: no matched text, no hash.
"""

import asyncio
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from arpeggio_ai.cli.output import JSON_HELP, console, emit_json, json_mode, run_command
from arpeggio_ai.config.loader import load_config_if_present, read_toml
from arpeggio_ai.config.models import RepoSettings
from arpeggio_ai.paths import arpeggio_home, repo_config_path
from arpeggio_ai.safety.fileset import FileSet, ScanReport, collect, scan_files
from arpeggio_ai.safety.process import scrubbed_env
from arpeggio_ai.safety.secret_scan import SecretScanner, scanner_from_config
from arpeggio_ai.safety.worktree import git

app = typer.Typer(help="Secret scanner tools.")

GlobOption = Annotated[list[str] | None, typer.Option(help="Glob on paths relative to PATH.")]


@app.callback(invoke_without_command=True)
def secrets_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


async def _repo_root(path: Path, env: dict[str, str]) -> Path | None:
    top = await git(["rev-parse", "--show-toplevel"], cwd=path, env=env, home=arpeggio_home())
    return Path(top.text().strip()).resolve() if top.exit_code == 0 else None


def _scanner(repo: Path | None) -> SecretScanner:
    """Configured provider keys and the repo's secret_scan_allow, when they exist."""
    config = load_config_if_present(repo)
    if config is not None:
        return scanner_from_config(config, os.environ)
    if repo is not None and repo_config_path(repo).exists():
        raw = read_toml(repo_config_path(repo)).get("repo", {})
        allow = RepoSettings.model_validate(raw).secret_scan_allow
        return SecretScanner(allow=allow)
    return SecretScanner()


@app.command("scan")
def scan_command(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="File or directory to scan.")],
    include: GlobOption = None,
    exclude: GlobOption = None,
    json_flag: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Dry-run the secret scanner to measure false positives before relying on redaction.

    Inside a git repository only files git does not ignore are read. Binary files and
    files over 5 MB are skipped. Output names file, line and type of each finding, never
    the matched text.
    """
    as_json = json_mode(ctx, json_flag)

    def action() -> ScanReport:
        target = path.expanduser().resolve()
        if not target.exists():
            raise FileNotFoundError(f"path not found: {target}")
        env = scrubbed_env()
        folder = target if target.is_dir() else target.parent
        repo = asyncio.run(_repo_root(folder, env))
        scanner = _scanner(repo)
        if target.is_file():
            files = FileSet(folder, [target.name])
        else:
            files = asyncio.run(
                collect(
                    target,
                    home=arpeggio_home(),
                    env=env,
                    include=include or (),
                    exclude=exclude or (),
                )
            )
        return scan_files(files, scanner)

    report = run_command(as_json, action)
    if as_json:
        emit_json({"ok": True, **report.to_dict()})
        return
    out = console()
    skipped = ", ".join(f"{k} {v}" for k, v in sorted(report.skipped.items())) or "none"
    out.print(f"{report.files_scanned} files scanned, skipped: {skipped}", markup=False)
    totals = report.totals()
    if not totals:
        out.print("No findings.", style="bold green")
        return
    by_type = Table("type", "findings")
    for kind, count in totals.items():
        by_type.add_row(kind, str(count))
    out.print(by_type)
    top = Table("file", "findings", title="Top files")
    for name, count in report.top_files():
        top.add_row(name, str(count))
    out.print(top)
    lines = Table("file", "line", "type")
    for name, line, kind in report.findings:
        lines.add_row(name, str(line), kind)
    out.print(lines)
