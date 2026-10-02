# -*- coding: utf-8 -*-
"""`python -m vocal_engine.mcp` の入口。

実体は `vocal_engine.mcp_server`。ここは薄いランチャだけ。
（このモジュール名は `mcp` だが、Python 3 は絶対 import なので
  `mcp_server.py` の中の `from mcp.server...` は PyPI の `mcp` パッケージを指す。）
"""
from .mcp_server import main

if __name__ == "__main__":
    main()
