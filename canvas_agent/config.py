"""Load and validate local settings before any Canvas requests are made."""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TEMPLATE = (
    "- [${checkbox}] **${name}** — ${course}\n"
    "  - ${due} · ${status}\n"
    "  - [Open assignment](${url})\n"
)
TEMPLATE_FIELDS = frozenset(
    {
        "checkbox",
        "id",
        "course_id",
        "name",
        "course",
        "due_at",
        "due",
        "status",
        "completed",
        "group",
        "points",
        "url",
        "description",
    }
)


class ConfigError(ValueError):
    """A configuration problem that can be shown without exposing credentials."""


@dataclass(frozen=True)
class StyleSettings:
    """Validated Markdown presentation settings."""

    title: str = "My study plan"
    item_template: str = DEFAULT_TEMPLATE

    def __post_init__(self) -> None:
        """Reject invalid placeholders even when the assignment list is empty."""
        if not isinstance(self.title, str) or not isinstance(self.item_template, str):
            raise ConfigError("style.title and style.item_template must be strings.")
        template = Template(self.item_template)
        if not template.is_valid() or set(template.get_identifiers()) - TEMPLATE_FIELDS:
            raise ConfigError(
                "Invalid style.item_template. Check the fields in config.example.toml."
            )


@dataclass(frozen=True)
class FileSettings:
    """Limits and local storage for optional PDF and PowerPoint extraction."""

    download_dir: Path = Path("output/files")
    max_file_mb: int = 25
    max_files_per_course: int = 100
    max_pages: int = 300


@dataclass(frozen=True)
class Settings:
    """Validated application settings with environment overrides applied."""

    base_url: str
    token: str = field(repr=False)
    course_ids: tuple[int, ...]
    zone: ZoneInfo
    days_ahead: int
    include_completed: bool
    style: StyleSettings
    files: FileSettings = field(default_factory=FileSettings)


def _section(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a TOML table.")
    return value


def load_settings(
    path: Path,
    *,
    demo: bool = False,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Read TOML and apply CANVAS_BASE_URL and CANVAS_API_TOKEN overrides.

    Demo runs may use defaults when the file is absent. Configuration errors
    raise ConfigError; filesystem errors are left for the CLI to report.
    The token is excluded from the returned settings' repr.
    """
    environment = os.environ if environ is None else environ
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except FileNotFoundError:
        if not demo:
            raise ConfigError(
                "Copy config.example.toml to config.local.toml "
                "and set your Canvas URL and token."
            ) from None
        document = {}
    except (tomllib.TOMLDecodeError, UnicodeError):
        # Some parser versions include source text, which may contain the token.
        raise ConfigError(
            "Invalid TOML configuration. Check its syntax and UTF-8 encoding."
        ) from None

    canvas = _section(document, "canvas")
    study = _section(document, "study")
    style = _section(document, "style")
    files = _section(document, "files")
    for key, default in (
        ("max_file_mb", 25),
        ("max_files_per_course", 100),
        ("max_pages", 300),
    ):
        value = files.get(key, default)
        if type(value) is not int or value <= 0:
            raise ConfigError(f"files.{key} must be a positive integer.")
    download_dir = files.get("download_dir", "output/files")
    if not isinstance(download_dir, str) or not download_dir.strip():
        raise ConfigError("files.download_dir must be a nonempty path string.")

    base_url = environment.get("CANVAS_BASE_URL", canvas.get("base_url", ""))
    token = environment.get("CANVAS_API_TOKEN", canvas.get("token", ""))
    if not isinstance(base_url, str) or not isinstance(token, str):
        raise ConfigError("Canvas base_url and token must be strings.")

    course_ids = canvas.get("course_ids", [])
    # bool is an int subclass, but true is not a valid course ID or day count.
    if not isinstance(course_ids, list) or any(
        type(course_id) is not int or course_id <= 0 for course_id in course_ids
    ):
        raise ConfigError("canvas.course_ids must be a list of positive integers.")
    days_ahead = study.get("days_ahead", 14)
    if type(days_ahead) is not int or days_ahead < 0:
        raise ConfigError("study.days_ahead must be a nonnegative integer.")
    include_completed = study.get("include_completed", False)
    if not isinstance(include_completed, bool):
        raise ConfigError("study.include_completed must be true or false.")

    zone_name = study.get("timezone", "Europe/Stockholm")
    if not isinstance(zone_name, str):
        raise ConfigError("study.timezone must be an IANA timezone string.")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            "Unknown timezone or missing timezone data. Check study.timezone "
            "and install the project dependencies."
        ) from None

    return Settings(
        base_url=base_url,
        token=token,
        course_ids=tuple(course_ids),
        zone=zone,
        days_ahead=days_ahead,
        include_completed=include_completed,
        style=StyleSettings(
            title=style.get("title", "My study plan"),
            item_template=style.get("item_template", DEFAULT_TEMPLATE),
        ),
        files=FileSettings(
            download_dir=Path(download_dir),
            max_file_mb=files.get("max_file_mb", 25),
            max_files_per_course=files.get("max_files_per_course", 100),
            max_pages=files.get("max_pages", 300),
        ),
    )
