# Install

## With pipx (recommended)

[pipx](https://pipx.pypa.io/) installs Gitoma into an isolated virtual environment and puts the `gitoma` command on your PATH. It's the cleanest option for a CLI you run across many projects.

```bash
pipx install gitoma
```

Upgrade at any time:

```bash
pipx upgrade gitoma
```

## From source

Clone and install in editable mode if you intend to hack on Gitoma itself:

```bash
git clone https://github.com/fabriziosalmi/gitoma
cd gitoma
pip install -e '.[dev]'
```

The `[dev]` extra pulls in pytest, ruff, and mypy — the three checks every change must pass.

## With pip

```bash
pip install gitoma
```

pip installs Gitoma into whatever Python environment is active. If you manage multiple Python projects on the same machine, prefer pipx.

## Verify

```bash
gitoma --help
gitoma doctor
```

`gitoma doctor` performs a full health check: config load, LM Studio reachability + target model presence, GitHub API authentication. It exits non-zero if anything is missing, so it's safe to use as a pre-run gate in scripts and CI.

## Uninstall

```bash
pipx uninstall gitoma       # if installed via pipx
pip uninstall gitoma        # if installed via pip
```

State under `~/.gitoma/` is not removed on uninstall. Delete the directory manually if you want a completely clean slate.

## What `~/.gitoma/` contains

After the first run, Gitoma creates:

| Path | Purpose |
|---|---|
| `~/.gitoma/config.toml` | Persistent configuration (`gitoma config set` writes here) |
| `~/.gitoma/.env` | Optional override file — takes precedence over `config.toml` |
| `~/.gitoma/state/<owner>__<repo>.json` | Per-repo run state (phase, task plan, PR URL, heartbeat, exit flag) |
| `~/.gitoma/state/<owner>__<repo>.lock` | Concurrent-run lock (held by kernel flock; auto-released on process death) |
| `~/.gitoma/logs/<owner>__<repo>/<ts>.jsonl` | Structured trace for every invocation |
| `~/.gitoma/runtime_token` | Auto-generated Bearer token for the REST API (mode `0600`) |

Every file is local to your machine. Gitoma ships no telemetry and phones nothing home.
