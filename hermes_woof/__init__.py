"""hermes-woof: WOOF weather simulation tools for Hermes Agent."""
from __future__ import annotations

from pathlib import Path

__version__ = "0.1.0"

SYSTEM_LINE = ("WOOF weather simulation tools (woof_*) are available. "
               "Before planning or launching a run, read the skill woof:woof with skill_view.")

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def register(ctx) -> None:
    """Register the 12 tools, the bundled skill, the prompt line, the CLI
    subcommand and the watcher. No subprocess runs here."""
    from . import cli, schemas, settings, tools, watcher

    try:
        from hermes_constants import get_hermes_home
        data_dir = Path(get_hermes_home()) / "plugin-data" / "woof"
    except Exception:
        data_dir = Path.home() / ".woof-hermes"
    def get(key, default=None):
        value = ctx.get_config(key, None)
        if value is None:
            # also accept the key written directly under plugins.entries.woof (without "settings:")
            value = _legacy_entry(data_dir).get(key)
        return default if value is None else value

    settings.bind(get, lambda key, value: ctx.set_config(key, value), data_dir)

    for schema in schemas.for_model():
        ctx.register_tool(name=schema["name"], toolset="woof", schema=schema,
                          handler=tools.HANDLERS[schema["name"]], check_fn=lambda: True,
                          description=schema["description"])

    ctx.register_skill("woof", PLUGIN_ROOT / "skills" / "woof" / "SKILL.md",
                       description="Plan, launch, watch, compare and score WOOF weather simulations")
    ctx.register_system_prompt_section("woof.pointer", SYSTEM_LINE)
    ctx.register_cli_command(name="woof", help="Install and check WOOF for the woof plugin",
                             setup_fn=cli.setup, handler_fn=cli.main,
                             description="hermes woof install | doctor | runs | lint")

    # Pushes wait for the agent's turn to end, so a run event never cuts off the agent's answer.
    register_hook = getattr(ctx, "register_hook", None)
    if register_hook:
        register_hook("pre_llm_call", watcher.turn_started)
        register_hook("post_llm_call", watcher.turn_ended)
        register_hook("on_session_end", watcher.turn_ended)

    def inject(text: str) -> bool:
        return bool(ctx.inject_message(text, role="user"))

    spawn = getattr(ctx, "spawn_task", None)
    tools.WATCHER_START = lambda: watcher.start(inject, spawn)
    # Resume watching runs that were in flight when Hermes last stopped.
    if any(not ({"completed", "failed", "stopped"} & set(r.get("notified_events") or []))
           for r in _index_rows(data_dir)):
        tools.WATCHER_START()
    on_unload = getattr(ctx, "on_unload", None)
    if on_unload:
        on_unload(watcher.stop)


def _index_rows(data_dir: Path) -> list:
    import json
    try:
        return list(json.loads((data_dir / "index.json").read_text(encoding="utf-8")).values())
    except (OSError, ValueError):
        return []


def _legacy_entry(data_dir: Path) -> dict:
    """plugins.entries.woof.<key> written without the settings level (read only)."""
    try:
        import yaml
        cfg = yaml.safe_load((data_dir.parent.parent / "config.yaml").read_text(encoding="utf-8")) or {}
        entry = ((cfg.get("plugins") or {}).get("entries") or {}).get("woof") or {}
        return {k: v for k, v in entry.items() if k != "settings" and not isinstance(v, (dict, list))}
    except Exception:
        return {}
