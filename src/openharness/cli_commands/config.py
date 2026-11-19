"""Config subcommands."""

from __future__ import annotations

import sys

import typer

from openharness.cli_app import config_app


def _config_resolve_target(settings: object, key: str) -> tuple[object, str]:
    target = settings
    parts = key.split(".")
    for part in parts[:-1]:
        if not hasattr(target, part):
            raise KeyError(key)
        target = getattr(target, part)
    leaf = parts[-1]
    if not hasattr(target, leaf):
        raise KeyError(key)
    return target, leaf

def _config_field_annotation(target: object, leaf: str) -> object | None:
    """Return the declared type of a pydantic field, if *target* has one."""
    fields = getattr(type(target), "model_fields", None)
    if not isinstance(fields, dict):
        return None
    field = fields.get(leaf)
    return None if field is None else field.annotation


def _config_coerce_value(current: object, raw: str, *, annotation: object | None = None, key: str = "") -> object:
    if annotation is not None:
        from openharness.commands.helpers import _coerce_annotation_value

        return _coerce_annotation_value(annotation, key or "value", raw)
    # Non-pydantic targets (e.g. plain dataclasses): fall back to the current value.
    if isinstance(current, bool):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"Invalid boolean value: {raw}")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return [entry.strip() for entry in raw.split(",") if entry.strip()]
    return raw

@config_app.command("show")
def config_show() -> None:
    """Print the resolved settings JSON."""
    from openharness.commands.registry import _settings_json_for_display
    from openharness.config.settings import load_settings

    print(_settings_json_for_display(load_settings()), flush=True)

@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Setting key, including dotted nested keys"),
    value: str = typer.Argument(..., help="Value to store"),
) -> None:
    """Persist one setting in ~/.openharness/settings.json."""
    from openharness.config.settings import load_settings, save_settings

    settings = load_settings()
    try:
        target, leaf = _config_resolve_target(settings, key)
    except KeyError:
        print(f"Unknown config key: {key}", file=sys.stderr)
        raise typer.Exit(1)
    try:
        coerced = _config_coerce_value(
            getattr(target, leaf),
            value,
            annotation=_config_field_annotation(target, leaf),
            key=key,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(1)
    setattr(target, leaf, coerced)
    save_settings(settings)
    print(f"Updated {key}", flush=True)
