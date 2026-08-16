# Knowledger

Python and Docker conventions load automatically from the user-level path-scoped rules in
`~/.claude/rules/` when Claude touches a `.py` file or a `Dockerfile` — no import needed
here. See `claude-dotfiles/docs/guideline-system.md`.

`Dockerfile` follows the shared rule's direct-entrypoint guidance: the runtime stage puts
`/app/.venv/bin` on `PATH` and calls `python` directly rather than going through `uv run`.
