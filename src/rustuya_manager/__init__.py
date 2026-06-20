"""rustuya-manager — sync layer between Tuya Cloud and rustuya-bridge.

The package exposes domain modules (models, diff, state, mqtt) plus a
`cli` entry point that preserves the original interactive workflow.
Topic and payload templating is delegated to `pyrustuyabridge` so the
manager's interpretation is byte-identical to the bridge's behavior.
"""

# The one place the version is defined. pyproject.toml resolves it at build
# time via [tool.setuptools.dynamic]; web.py uses it for the FastAPI title;
# check.sh reads it from the installed package. Bump here and nowhere else.
__version__ = "0.1.0rc56"
