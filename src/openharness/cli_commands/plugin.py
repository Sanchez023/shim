"""Plugin subcommands."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from openharness.cli_app import plugin_app
from openharness.plugins.schemas import PluginVariable
from openharness.plugins.types import LoadedPlugin

config_app = typer.Typer(
    name="config",
    help="Show, set, or clear plugin variables and tokens",
)
plugin_app.add_typer(config_app, name="config")


def _load_installed_plugins() -> list[LoadedPlugin]:
    from openharness.config import load_settings
    from openharness.plugins.loader import load_plugins

    return list(load_plugins(load_settings(), str(Path.cwd())))


def _find_plugin(name: str) -> LoadedPlugin:
    for plugin in _load_installed_plugins():
        if plugin.name == name:
            return plugin
    print(f"Plugin not found: {name}", file=sys.stderr)
    raise typer.Exit(1)


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


def _variable_env_names(spec: PluginVariable) -> tuple[str, ...] | None:
    return (spec.env,) if spec.env else None


@plugin_app.command("list")
def plugin_list() -> None:
    """List installed plugins and missing required variables."""
    from openharness.plugins.config import get_plugin_variable

    plugins = _load_installed_plugins()
    if not plugins:
        print("No plugins installed.")
        return
    for plugin in plugins:
        status = "enabled" if plugin.enabled else "disabled"
        missing: list[str] = []
        for key, spec in plugin.manifest.variables.items():
            value = get_plugin_variable(
                plugin.name,
                key,
                env_names=_variable_env_names(spec),
                default=spec.default,
            )
            if spec.required and value is None:
                missing.append(key)
        suffix = f"  missing: {', '.join(missing)}" if missing else ""
        print(f"  {plugin.name} [{status}] - {plugin.description or ''}{suffix}")


@plugin_app.command("install")
def plugin_install(
    source: str = typer.Argument(..., help="Plugin source (path or URL)"),
) -> None:
    """Install a plugin from a source path."""
    from openharness.plugins.installer import install_plugin_from_path

    result = install_plugin_from_path(source)
    print(f"Installed plugin: {result}")


@plugin_app.command("uninstall")
def plugin_uninstall(
    name: str = typer.Argument(..., help="Plugin name to uninstall"),
) -> None:
    """Uninstall a plugin."""
    from openharness.plugins.installer import uninstall_plugin

    try:
        uninstall_plugin(name)
    except ValueError as exc:
        raise typer.BadParameter("invalid plugin name") from exc
    print(f"Uninstalled plugin: {name}")


@config_app.command("show")
def plugin_config_show(
    name: str = typer.Argument(..., help="Plugin name"),
) -> None:
    """Show declared variables and whether they are configured."""
    from openharness.plugins.config import get_plugin_variable, plugin_variable_source

    plugin = _find_plugin(name)
    variables = plugin.manifest.variables
    if not variables:
        print(f"Plugin {name} does not declare any variables.")
        return

    print(f"Variables for {name}:")
    for key, spec in variables.items():
        env_names = _variable_env_names(spec)
        source = plugin_variable_source(name, key, env_names=env_names)
        value = get_plugin_variable(name, key, env_names=env_names, default=spec.default)
        flags = [flag for flag, enabled in (("required", spec.required), ("secret", spec.secret)) if enabled]
        flag_text = f" [{'/'.join(flags)}]" if flags else ""
        if value is None:
            print(f"  {key}{flag_text}: missing")
        elif source is not None:
            display = _mask(value) if spec.secret else value
            print(f"  {key}{flag_text}: set ({source})" + (f" = {display}" if display else ""))
        elif spec.default is not None:
            display = _mask(spec.default) if spec.secret else spec.default
            print(f"  {key}{flag_text}: set (default)" + (f" = {display}" if display else ""))
        else:
            print(f"  {key}{flag_text}: set")
        if spec.description:
            print(f"    {spec.description}")


@config_app.command("set")
def plugin_config_set(
    name: str = typer.Argument(..., help="Plugin name"),
    key: str = typer.Argument(..., help="Variable name, e.g. TAVILY_API_KEY"),
    value: str | None = typer.Option(
        None,
        "--value",
        help="Value; omit to be prompted without echo",
    ),
) -> None:
    """Store a plugin variable or token in the OpenHarness credential store."""
    from openharness.plugins.config import set_plugin_variable

    plugin = _find_plugin(name)
    if key not in plugin.manifest.variables:
        print(
            f"Warning: plugin {name} does not declare {key}; storing it anyway.",
            file=sys.stderr,
        )
    resolved = value if value is not None else typer.prompt(f"{name} {key}", hide_input=True)
    if not resolved.strip():
        raise typer.BadParameter("value must not be empty")
    set_plugin_variable(name, key, resolved)
    print(f"Stored {key} for plugin {name}.")
    print("Plugins that read variables at execution time pick it up immediately.")


@config_app.command("unset")
def plugin_config_unset(
    name: str = typer.Argument(..., help="Plugin name"),
    key: str = typer.Argument(..., help="Variable name"),
) -> None:
    """Remove a stored plugin variable or token."""
    from openharness.plugins.config import clear_plugin_variable

    _find_plugin(name)
    removed = clear_plugin_variable(name, key)
    if removed:
        print(f"Removed {key} for plugin {name}.")
    else:
        print(f"{key} was not stored for plugin {name}.")
