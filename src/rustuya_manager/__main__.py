"""Allow `python -m rustuya_manager` invocation."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
