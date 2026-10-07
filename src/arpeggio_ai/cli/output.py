"""Output shared by every command: Rich text for people, one JSON object for scripts.

Exit codes: 0 success, 1 unexpected error, 2 config or validation error.
"""

import json
from collections.abc import Callable
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.text import Text

from arpeggio_ai.core.errors import ConfigError

EXIT_UNEXPECTED = 1
EXIT_CONFIG = 2

JSON_HELP = "Print one JSON object to stdout instead of human-readable text."

ErrorEntry = dict[str, str | None]


def console() -> Console:
    # Built per call so it writes to whatever sys.stdout is at that moment.
    return Console(soft_wrap=True)


def error_console() -> Console:
    return Console(stderr=True, soft_wrap=True)


def json_mode(ctx: typer.Context, flag: bool) -> bool:
    """True if --json was given on this command or on the root `arpeggio` command."""
    return flag or bool(ctx.find_root().params.get("json_output"))


def emit_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload))


def fail(errors: list[ErrorEntry], as_json: bool, exit_code: int) -> NoReturn:
    """Report errors (JSON on stdout, or text on stderr) and exit with ``exit_code``."""
    if as_json:
        emit_json({"ok": False, "errors": errors})
    else:
        err = error_console()
        for entry in errors:
            where = ": ".join(part for part in (entry["file"], entry["field"]) if part)
            line = Text("error: ", style="bold red")
            line.append(f"{where}: {entry['message']}" if where else str(entry["message"]))
            err.print(line)
    raise typer.Exit(exit_code)


def run_command[T](as_json: bool, action: Callable[[], T]) -> T:
    """Run a command body and turn errors into the documented output and exit code."""
    try:
        return action()
    except (typer.Exit, typer.Abort):
        raise
    except ConfigError as error:
        fail([issue.to_dict() for issue in error.issues], as_json, EXIT_CONFIG)
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        fail([{"file": None, "field": None, "message": message}], as_json, EXIT_UNEXPECTED)
