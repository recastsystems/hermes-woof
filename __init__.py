"""WOOF for Hermes Agent: plugin entry point."""
try:
    from .hermes_woof import register  # noqa: F401  (how Hermes imports the plugin)
except ImportError:  # imported as a plain module, for example by a test collector
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    from hermes_woof import register  # noqa: F401
