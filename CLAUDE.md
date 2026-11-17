# Knowledger

Python and Docker conventions load automatically from the user-level path-scoped rules in
`~/.claude/rules/` when Claude touches a `.py` file or a `Dockerfile` — no import needed
here. See `claude-dotfiles/docs/guideline-system.md`.

`Dockerfile` follows the shared rule's direct-entrypoint guidance: the runtime stage puts
`/app/.venv/bin` on `PATH` and calls `python` directly rather than going through `uv run`.

Local Docker work (test builds, throwaway containers) goes to Docker Desktop (the
`desktop-linux` context, already selected on this laptop). Never pass `--context default`:
the context named `default` is the native daemon, which is only for pihole, and build cache
left there fills the root disk unseen. Production runs on the VPS via `deploy.sh`, so
nothing local needs the native daemon. Run throwaway containers with `--rm`.
