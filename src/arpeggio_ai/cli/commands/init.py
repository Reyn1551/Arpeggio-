"""`arpeggio init`: create the home, runtime subdirectories, a starter config and the database."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.text import Text

from arpeggio_ai.cli.output import JSON_HELP, console, emit_json, json_mode, run_command
from arpeggio_ai.config.loader import DEFAULT_TEMPLATE, template_bytes
from arpeggio_ai.core.errors import ArpeggioError
from arpeggio_ai.core.logs import configure_logging
from arpeggio_ai.paths import RUNTIME_SUBDIRS, arpeggio_home, db_path, global_config_path, logs_dir
from arpeggio_ai.store.db import open_db

DIR_MODE = 0o700

log = logging.getLogger(__name__)


@dataclass
class InitResult:
    home: Path
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    backup: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "home": str(self.home),
            "created": self.created,
            "skipped": self.skipped,
            "backup": self.backup,
        }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _ensure_dir(path: Path, result: InitResult) -> None:
    if path.is_dir():
        result.skipped.append(str(path))
        return
    path.mkdir(mode=DIR_MODE, parents=True)
    path.chmod(DIR_MODE)  # mkdir's mode is reduced by the umask; set it explicitly
    result.created.append(str(path))


def initialize(home: Path, force: bool) -> InitResult:
    """Create what is missing and skip what exists. Never deletes or overwrites a user file."""
    result = InitResult(home=home)
    _ensure_dir(home, result)
    for name in RUNTIME_SUBDIRS:
        _ensure_dir(home / name, result)

    config_path = global_config_path(home)
    if force and config_path.exists():
        backup = config_path.with_name(f"{config_path.name}.bak.{_utc_now():%Y%m%dT%H%M%SZ}")
        if backup.exists():
            raise ArpeggioError(f"backup {backup} already exists. Wait a second and retry.")
        config_path.rename(backup)
        result.backup = str(backup)

    if config_path.exists():
        result.skipped.append(str(config_path))
    else:
        with config_path.open("xb") as handle:
            handle.write(template_bytes(DEFAULT_TEMPLATE))
        result.created.append(str(config_path))

    # Opening the database creates it if needed and applies pending migrations. An existing
    # database keeps its data: --force only ever touches config.toml.
    configure_logging(logs_dir(home))
    database = db_path(home)
    existed = database.exists()
    open_db(database).close()
    (result.skipped if existed else result.created).append(str(database))

    log.info(
        "init.completed",
        extra={"created_count": len(result.created), "skipped_count": len(result.skipped)},
    )
    return result


def _print_human(result: InitResult) -> None:
    def short(path: str) -> str:
        relative = Path(path).relative_to(result.home)
        return "." if relative == Path() else relative.as_posix()

    out = console()
    out.print(Text.assemble(("Arpeggio home: ", "bold"), str(result.home)))
    rows = [("created", path, "green") for path in result.created]
    rows += [("skipped", path, "dim") for path in result.skipped]
    if result.backup:
        rows.append(("backup", result.backup, "yellow"))
    for label, path, style in rows:
        out.print(Text.assemble(("  " + label.ljust(8), style), short(path)))
    out.print("Next: fill in config.toml, then run `arpeggio config validate`.", markup=False)


def init_command(
    ctx: typer.Context,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Rename an existing config.toml to config.toml.bak.<UTC time> and write a "
            "fresh one.",
        ),
    ] = False,
    json_flag: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Create the Arpeggio home, a starter config.toml and the database. Safe to run again."""
    as_json = json_mode(ctx, json_flag)
    result = run_command(as_json, lambda: initialize(arpeggio_home(), force))
    if as_json:
        emit_json(result.to_dict())
    else:
        _print_human(result)
