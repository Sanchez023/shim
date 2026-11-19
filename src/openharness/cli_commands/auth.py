"""Auth subcommands."""

from __future__ import annotations

import sys

import typer

from openharness.cli_app import auth_app
from openharness.cli_auth_helpers import (
    _AUTH_SOURCE_LABELS,
    _PROVIDER_LABELS,
    _bind_external_provider,
    _login_provider,
    _run_copilot_login,
)


@auth_app.command("login")
def auth_login(
    provider: str | None = typer.Argument(None, help="Provider name (anthropic, openai, copilot, …)"),
) -> None:
    """Interactively authenticate with a provider.

    Run without arguments to choose a provider from a menu.
    Supported providers: anthropic, anthropic_claude, openai, openai_codex, copilot, dashscope, bedrock, vertex, moonshot, minimax, modelscope.
    """
    if provider is None:
        print("Select a provider to authenticate:", flush=True)
        labels = list(_PROVIDER_LABELS.items())
        for i, (name, label) in enumerate(labels, 1):
            print(f"  {i}. {label} [{name}]", flush=True)
        raw = typer.prompt("Enter number or provider name", default="1")
        try:
            idx = int(raw.strip()) - 1
            if 0 <= idx < len(labels):
                provider = labels[idx][0]
            else:
                print("Invalid selection.", file=sys.stderr)
                raise typer.Exit(1)
        except ValueError:
            provider = raw.strip()

    provider = provider.lower()
    _login_provider(provider)

@auth_app.command("status")
def auth_status_cmd() -> None:
    """Show authentication source and provider profile status."""
    from openharness.auth.manager import AuthManager

    manager = AuthManager()
    auth_sources = manager.get_auth_source_statuses()
    profiles = manager.get_profile_statuses()

    print("Auth sources:")
    print(f"{'Source':<24} {'State':<14} {'Origin':<10} Active")
    print("-" * 60)
    for name, info in auth_sources.items():
        label = _AUTH_SOURCE_LABELS.get(name, name)
        active_str = "<-- active" if info["active"] else ""
        print(f"{label:<24} {info['state']:<14} {info['source']:<10} {active_str}")
        if info.get("detail"):
            print(f"  detail: {info['detail']}")

    print()
    print("Provider profiles:")
    print(f"{'Profile':<20} {'Provider':<18} {'Auth source':<22} {'State':<12} Active")
    print("-" * 92)
    for name, info in profiles.items():
        status_str = "ready" if info["configured"] else info.get("auth_state", "missing auth")
        active_str = "<-- active" if info["active"] else ""
        print(f"{name:<20} {info['provider']:<18} {info['auth_source']:<22} {status_str:<12} {active_str}")

@auth_app.command("logout")
def auth_logout(
    provider: str | None = typer.Argument(None, help="Provider to log out (default: active provider)"),
) -> None:
    """Clear stored authentication for a provider."""
    from openharness.auth.manager import AuthManager

    manager = AuthManager()
    if provider is None:
        target = manager.get_active_profile()
        manager.clear_profile_credential(target)
        print(f"Authentication cleared for profile: {target}", flush=True)
        return
    manager.clear_credential(provider)
    print(f"Authentication cleared for provider: {provider}", flush=True)

@auth_app.command("switch")
def auth_switch(
    provider: str = typer.Argument(..., help="Auth source or profile to activate"),
) -> None:
    """Switch the auth source for the active profile, or use a profile by name."""
    from openharness.auth.manager import AuthManager

    manager = AuthManager()
    try:
        manager.switch_provider(provider)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise typer.Exit(1)
    print(f"Switched auth/profile to: {provider}", flush=True)

@auth_app.command("copilot-login")
def auth_copilot_login() -> None:
    """Authenticate with GitHub Copilot via device flow (alias for 'oh auth login copilot')."""
    _run_copilot_login()

@auth_app.command("codex-login")
def auth_codex_login() -> None:
    """Bind OpenHarness to a local Codex CLI subscription session."""
    _bind_external_provider("openai_codex")

@auth_app.command("claude-login")
def auth_claude_login() -> None:
    """Bind OpenHarness to a local Claude CLI subscription session."""
    _bind_external_provider("anthropic_claude")

@auth_app.command("copilot-logout")
def auth_copilot_logout() -> None:
    """Remove stored GitHub Copilot authentication."""
    from openharness.api.copilot_auth import clear_github_token

    clear_github_token()
    print("Copilot authentication cleared.")
