"""rustuya-manager — sync layer between Tuya Cloud and rustuya-bridge.

The package exposes domain modules (models, diff, state, mqtt) plus a
`cli` entry point that preserves the original interactive workflow.
Topic and payload templating is delegated to `pyrustuyabridge` so the
manager's interpretation is byte-identical to the bridge's behavior.

`Manager` (in `manager.py`) is the programmatic entry point for using the
manager as a library — no web/GUI dependency required:

    from rustuya_manager import Manager

    async with Manager(broker="mqtt://host:1883", root="rustuya") as m:
        await m.wait_ready()
        await m.add_device("bf1234...")
"""

from .manager import Manager

# The one place the version is defined. pyproject.toml resolves it at build
# time via [tool.setuptools.dynamic]; web.py uses it for the FastAPI title;
# check.sh reads it from the installed package. Bump here and nowhere else.
__version__ = "0.2.0.dev0"

__all__ = ["Manager", "__version__"]
