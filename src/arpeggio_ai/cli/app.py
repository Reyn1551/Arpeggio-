"""The `arpeggio` command."""

from typing import Annotated

import typer

from arpeggio_ai import __version__
from arpeggio_ai.cli.commands.config import app as config_app
from arpeggio_ai.cli.commands.init import init_command
from arpeggio_ai.cli.output import JSON_HELP, emit_json

app = typer.Typer(
    name="arpeggio",
    help="Route coding tasks across agents and models, verify the results, learn from each task.",
    # Tracebacks must never print local variables: they can hold secrets.
    pretty_exceptions_show_locals=False,
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    # --version is handled here, not in an eager callback, so `--version --json` works in
    # any order. Bare `arpeggio` prints help with exit code 0 (no_args_is_help would exit 2,
    # which this CLI reserves for config errors).
    if version:
        if json_output:
            emit_json({"version": __version__})
        else:
            typer.echo(f"arpeggio {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


app.command("init")(init_command)
app.add_typer(config_app, name="config")
