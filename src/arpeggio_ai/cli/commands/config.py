"""`arpeggio config`: check and print the effective configuration."""

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.text import Text
from rich.tree import Tree

from arpeggio_ai.cli.output import JSON_HELP, console, emit_json, json_mode, run_command
from arpeggio_ai.config.loader import load_config
from arpeggio_ai.paths import global_config_path, repo_config_path

app = typer.Typer(help="Check and show the effective configuration.")

RepoOption = Annotated[
    Path | None,
    typer.Option(
        "--repo",
        help="Repo directory whose .arpeggio/config.toml is merged over the global config.",
    ),
]
JsonOption = Annotated[bool, typer.Option("--json", help=JSON_HELP)]


@app.callback(invoke_without_command=True)
def config_main(ctx: typer.Context) -> None:
    # Bare `arpeggio config` prints help with exit code 0, like the root command.
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


@app.command("validate")
def validate_command(
    ctx: typer.Context, repo: RepoOption = None, json_flag: JsonOption = False
) -> None:
    """Load and validate the config, merged with the repo config when --repo is given."""
    as_json = json_mode(ctx, json_flag)
    config = run_command(as_json, lambda: load_config(repo))
    tiers = sorted({spec.tier for spec in config.models.values()})
    if as_json:
        emit_json(
            {
                "ok": True,
                "providers": len(config.providers),
                "models": len(config.models),
                "tiers": tiers,
            }
        )
        return

    out = console()
    out.print("Config OK", style="bold green")
    out.print(
        f"{_count(len(config.providers), 'provider')}, {_count(len(config.models), 'model')}, "
        f"tiers {', '.join(str(tier) for tier in tiers)}",
        markup=False,
    )
    out.print(Text.assemble(("global: ", "dim"), str(global_config_path())))
    if repo is not None:
        repo_path = repo_config_path(repo)
        shown = str(repo_path) if repo_path.exists() else f"{repo_path} (not found, global only)"
        out.print(Text.assemble(("repo:   ", "dim"), shown))


def _add_branch(node: Tree, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if isinstance(value, dict):
            _add_branch(node.add(Text(str(key), style="bold")), value)
        else:
            node.add(Text.assemble((f"{key} = ", "cyan"), json.dumps(value)))


@app.command("show")
def show_command(
    ctx: typer.Context, repo: RepoOption = None, json_flag: JsonOption = False
) -> None:
    """Print the effective config after merging and validation."""
    as_json = json_mode(ctx, json_flag)
    config = run_command(as_json, lambda: load_config(repo))
    data = config.model_dump(mode="json", exclude_none=True)
    if as_json:
        emit_json({"ok": True, "config": data})
        return
    tree = Tree(Text("config", style="bold"))
    _add_branch(tree, data)
    console().print(tree)
