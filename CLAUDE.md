@AGENTS.md

## Claude Code で作業するとき

- リポジトリ直下の `.mcp.json`（git 管理外。worktree の準備で置くもの）は、Claude Code もプロジェクトの MCP サーバーとして読む。
  承認すると gliss のツールが使えるが、`load_project()` は利用者が画面で開いている曲を開く。開発の確かめには合成の WAV か一時のプロジェクトを使う。
- Windows では Bash tool は Git Bash。NSIS の `/S` のようにスラッシュで始まる引数や、`\` を含むパスを渡すコマンドは PowerShell tool で打つ。
