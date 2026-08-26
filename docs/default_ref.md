I want to change the default-ref configuration semantics before implementation.

Append/change the docs/workspace-plan.md with the changes regarding the default-ref as a plan for implementation.

Add support for a global `default_ref` in the top-level configuration, while
retaining an optional per-repository `default_ref`.

The effective default ref for a repository must be resolved in this order:

1. The repository-specific `default_ref`, if configured.
2. The global `default_ref`, if configured.
3. Existing automatic Git default-ref discovery, only if neither is configured.

Example:

    default_ref = "main"

    [[repos]]
    name = "repo1"
    ...
    default_ref = "development"

    [[repos]]
    name = "repo2"
    ...
    default_ref = "develop"

    [[repos]]
    name = "repo3"
    ...

Effective defaults:

    repo1 -> development
    repo2 -> develop
    repo3 -> main

Please revise §4c and any other affected sections of doc/workspace-plan.md.

Also explicitly specify:

- precedence between repository-specific, global, and auto-discovered defaults;
- whether configured `default_ref` values must resolve to refs or may
  resolve to arbitrary Git refs;
- validation/error behavior when an explicitly configured default cannot be
  resolved in a repository;
- behavior of read-only commands such as `status` when the configured default
  is unavailable;
- whether automatic multi-remote discovery is used only when neither config
  level supplies a default;
- backward compatibility for configurations without a global
  `default_ref`;
- TOML examples for global-only, per-repo override, and fully automatic cases.

Add corresponding required tests, but do not implement the change yet.

Do not reopen decisions unrelated to this change unless the new precedence
semantics directly conflict with them.
