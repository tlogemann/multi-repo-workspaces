# Glossary

- **Workspace** — The current directory initialized by `ws init`; it contains `ws.toml` and, after initialization, the clone root.
- **Clone root** — The workspace's `repos/` directory, which is created only by `ws init` and contains the configured repository clones.
- **Repository definition** — One `[[repos]]` entry in `ws.toml`, identified by its remote URL and optionally supplying a configured default ref.
- **Configured default ref** — A default ref supplied in configuration.
- **Effective default ref** — The default ref selected for a repository.
- **Auto-discovered default ref** — A default ref selected automatically when no configured default ref applies.
- **Locked default selector** — The selector retained as the repository's chosen default.
