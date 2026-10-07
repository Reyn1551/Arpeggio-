"""Documentation consistency checks (requirement IDs, config reference, links, templates)."""

import re
import tomllib
from collections import Counter
from pathlib import Path

import pytest
from pydantic import BaseModel

from arpeggio_ai.config.loader import TEMPLATES, template_bytes
from arpeggio_ai.config.models import (
    Budget,
    ModelSpec,
    PricingWindows,
    PrivacySettings,
    Provider,
    RepoSettings,
)

ROOT = Path(__file__).parents[2]
DOCS = ROOT / "docs"
REQUIREMENTS = (DOCS / "02-REQUIREMENTS.md").read_text(encoding="utf-8")
ROADMAP = (DOCS / "08-ROADMAP.md").read_text(encoding="utf-8")
ROUTING = (DOCS / "05-ROUTING-AND-COST.md").read_text(encoding="utf-8")

NEW_IDS = [
    *(f"BUD-0{n}" for n in range(1, 6)),
    *(f"QTA-0{n}" for n in range(1, 6)),
    *(f"CFG-{n:02d}" for n in range(5, 11)),
    "RTE-11",
    "RTE-12",
    "CST-10",
    "CST-11",
    "SAF-07",
    "SAF-08",
    "CLI-05",
    "CLI-06",
    "EVL-06",
    "NFR-11",
    "NFR-12",
]

MARKDOWN = sorted(
    [*ROOT.glob("*.md"), *DOCS.glob("*.md"), *(DOCS / "adr").glob("*.md")],
)


def defined_ids() -> list[str]:
    return re.findall(r"^\| ([A-Z]{3}-\d{2}) \|", REQUIREMENTS, flags=re.MULTILINE)


def referenced_ids(text: str) -> set[str]:
    """IDs used in text, with ranges such as CFG-05..10 expanded."""
    found: set[str] = set()
    for area, first, last in re.findall(r"\b([A-Z]{3})-(\d{2})(?:\.\.(\d{2}))?\b", text):
        end = int(last) if last else int(first)
        found.update(f"{area}-{n:02d}" for n in range(int(first), end + 1))
    return found


# Requirement IDs (A.5.1, A.5.2)


def test_requirement_ids_are_unique() -> None:
    duplicates = [rid for rid, count in Counter(defined_ids()).items() if count > 1]
    assert duplicates == []


def test_new_ids_are_defined_and_on_the_roadmap() -> None:
    defined = set(defined_ids())
    on_roadmap = referenced_ids(ROADMAP)
    assert [rid for rid in NEW_IDS if rid not in defined] == []
    assert [rid for rid in NEW_IDS if rid not in on_roadmap] == []


def test_roadmap_only_references_defined_ids() -> None:
    assert sorted(referenced_ids(ROADMAP) - set(defined_ids())) == []


def test_range_expansion() -> None:
    assert referenced_ids("CFG-05..10 and RTE-12") == {f"CFG-{n:02d}" for n in range(5, 11)} | {
        "RTE-12"
    }


# Config field names (A.5.3)


def reference_fields(heading: str) -> set[str]:
    """Field names in the first column of a docs/05 config reference table."""
    section = ROUTING.split("## Config reference", 1)[1].split(f"### `{heading}`", 1)[1]
    section = re.split(r"\n#{2,3} ", section, maxsplit=1)[0]
    names: set[str] = set()
    for line in section.splitlines():
        if line.startswith("| `"):
            first_cell = line.split("|")[1]
            names.update(re.findall(r"`([a-z_.<>]+)`", first_cell))
    return names


@pytest.mark.parametrize(
    ("heading", "model"),
    [
        ("[budget]", Budget),
        ("[providers.<name>]", Provider),
        ('[models."tier<N>.<name>"]', ModelSpec),
        ("[repo]", RepoSettings),
        ("[privacy]", PrivacySettings),
    ],
)
def test_config_reference_matches_the_models(heading: str, model: type[BaseModel]) -> None:
    documented = reference_fields(heading)
    top_level = {name.split(".", 1)[0] for name in documented}
    assert top_level == set(model.model_fields)
    if model is Provider:
        nested = {name.split(".", 1)[1] for name in documented if "." in name}
        assert nested == set(PricingWindows.model_fields)


@pytest.mark.parametrize(
    "field",
    [
        "price_cache_hit_in_per_m",
        "data_use",
        "effort_params",
        "last_verified",
        "response_model_aliases",
        "prepaid_balance_usd",
        "max_quota_wait_s",
        "allow_training_providers",
    ],
)
def test_field_names_in_requirements_and_routing_docs_exist(field: str) -> None:
    all_fields = (
        set(Budget.model_fields)
        | set(Provider.model_fields)
        | set(ModelSpec.model_fields)
        | set(RepoSettings.model_fields)
    )
    assert field in all_fields
    assert re.search(rf"`{field}[`.]", ROUTING), field


def test_inline_micro_template_is_the_packaged_file() -> None:
    block = ROUTING.split("### Config templates", 1)[1].split("```toml\n", 1)[1]
    block = block.split("```", 1)[0]
    packaged = template_bytes("micro-deepseek").decode("utf-8").replace("\r\n", "\n")
    assert block == packaged
    assert tomllib.loads(block) == tomllib.loads(packaged)


# Links (A.5.4)


def slug(heading: str) -> str:
    """GitHub-style anchor for a heading."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def outside_code(text: str) -> list[str]:
    lines, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return lines


def anchors(path: Path) -> set[str]:
    seen: Counter[str] = Counter()
    result = set()
    for line in outside_code(path.read_text(encoding="utf-8")):
        match = re.match(r"#{1,6} (.+)", line)
        if match:
            base = slug(match[1])
            result.add(base if seen[base] == 0 else f"{base}-{seen[base]}")
            seen[base] += 1
    return result


def links(path: Path) -> list[str]:
    text = "\n".join(outside_code(path.read_text(encoding="utf-8")))
    text = re.sub(r"`[^`]*`", "", text)  # inline code is not a link
    return re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", text)


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: str(p.relative_to(ROOT)))
def test_internal_links_resolve(path: Path) -> None:
    broken = []
    for target in links(path):
        if re.match(r"[a-z]+:", target):
            continue
        file_part, _, anchor = target.partition("#")
        resolved = (path.parent / file_part).resolve() if file_part else path
        exists = resolved.exists()
        has_anchor = not anchor or resolved.suffix != ".md" or anchor in anchors(resolved)
        if not (exists and has_anchor):
            broken.append(target)
    assert broken == []


def test_link_checker_sees_links_and_anchors() -> None:
    assert "docs/02-REQUIREMENTS.md" in links(ROOT / "README.md")
    assert "05-ROUTING-AND-COST.md#risk-classification" in links(DOCS / "02-REQUIREMENTS.md")
    assert {"budget-profiles", "config-reference", "budget"} <= anchors(
        DOCS / "05-ROUTING-AND-COST.md"
    )
    assert "budget-profiles-1" not in anchors(DOCS / "05-ROUTING-AND-COST.md")


def test_slug_rules() -> None:
    assert slug("When cascade pays off") == "when-cascade-pays-off"
    assert slug("`[budget]`") == "budget"
    assert (
        slug("Single-provider mode (DeepSeek example)") == "single-provider-mode-deepseek-example"
    )


# Secrets and invented free-tier data (A.5.5)


KEY_SHAPES = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|gsk_[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}"
)


def published_text_files() -> list[Path]:
    templates = sorted((ROOT / "src" / "arpeggio_ai" / "config" / "templates").glob("*.toml"))
    return [*MARKDOWN, *templates]


def test_no_key_shaped_strings_in_docs_or_templates() -> None:
    hits = [
        f"{path.relative_to(ROOT)}: {match}"
        for path in published_text_files()
        for match in KEY_SHAPES.findall(path.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_free_template_uses_placeholders_only() -> None:
    free = tomllib.loads(template_bytes("free").decode("utf-8"))
    for key, spec in free["models"].items():
        assert spec["model"] == "<free-model-id>", key
        assert spec.get("limits", {}) == {}, key
        assert spec["free"] is True, key


@pytest.mark.parametrize("name", TEMPLATES)
def test_templates_state_behavioral_defaults_explicitly(name: str) -> None:
    budget = tomllib.loads(template_bytes(name).decode("utf-8"))["budget"]
    assert {"reserve_usd", "overhead_alert", "max_quota_wait_s"} <= set(budget)


def test_every_template_says_what_it_expects() -> None:
    for name in TEMPLATES:
        header = template_bytes(name).decode("utf-8").split("\n\n", 1)[0]
        assert "Expects:" in header, name
        assert "verif" in header or "check" in header, name


# Forbidden word (A.5.6)


def test_forbidden_word_is_absent() -> None:
    word = "me" + "ja"  # built from halves so this file does not contain it
    skip = {".venv", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache", "dist", "build"}
    hits = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or skip.intersection(path.relative_to(ROOT).parts):
            continue
        if path.suffix in {".pyc", ".db", ".whl", ".gz"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if word in text.lower():
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []
