# Glossary

- **Project root** — The current directory initialized by `ws init`; it contains `ws.toml`, the source clone root, and feature workspaces.
- **Source clone root** — The project root's `repos/` directory, created only by `ws init`; it contains the configured source repository clones.
- **Feature workspace** — A named, worktree-based workspace at `workspaces/<name>/` whose repositories are detached worktrees of the source clones.
- **Repository definition** — One `[[repos]]` entry in `ws.toml`, identified by its remote URL and optionally supplying a configured default ref.
- **Configured default ref** — A default ref supplied in configuration.
- **Effective default ref** — The default ref selected for a repository.
- **Auto-discovered default ref** — A default ref selected automatically when no configured default ref applies.
- **Locked default selector** — The selector retained as the repository's chosen default.
