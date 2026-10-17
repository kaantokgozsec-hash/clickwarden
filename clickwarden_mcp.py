"""clickwarden MCP launcher — works from any working directory.

MCP clients launch this file directly:
    python <path-to-this-repo>/clickwarden_mcp.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clickwarden.server import main  # noqa: E402

if __name__ == "__main__":
    main()
