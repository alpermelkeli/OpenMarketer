"""Command-line entry point for the model configuration.

    python config/llm_config.py show

The implementation lives in ``openmarketer_core.llm_config``; this file keeps
the documented path working.
"""

from openmarketer_core.llm_config import (
    Config,
    ConfigError,
    ModelRouter,
    Resolved,
    main,
)

__all__ = ["Config", "ConfigError", "ModelRouter", "Resolved", "main"]

if __name__ == "__main__":
    main()
