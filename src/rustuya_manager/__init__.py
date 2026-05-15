"""rustuya-manager — sync layer between Tuya Cloud and rustuya-bridge.

The package exposes domain modules (models, diff, state, mqtt) plus a
`cli` entry point that preserves the original interactive workflow.
Topic and payload templating is delegated to `pyrustuyabridge` so the
manager's interpretation is byte-identical to the bridge's behavior.
"""

__version__ = "0.1.0rc7"
