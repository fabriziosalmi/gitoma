# Deploy

Remote ops for the gitoma Mac Mini deployment. All the state lives on a
single Mac Mini reachable over Tailscale; `deploy/minimac` is the one
command surface for everything you'd otherwise SSH in to do by hand.

## Usage

```bash
deploy/minimac <command> [args]
```

| Command                    | What it does                                                   |
| -------------------------- | -------------------------------------------------------------- |
| `deploy`                   | rsync local working tree → install + (re)load launchd service  |
| `status`                   | Tailscale → SSH → launchd → HTTP → versions, one snapshot      |
| `start`                    | Load the launchd service                                       |
| `stop`                     | Unload the launchd service                                     |
| `restart`                  | Stop + start (returns when HTTP is green again)                |
| `logs [-f] [-n N]`         | Last N lines (default 60) of stdout + stderr. `-f` to follow.  |
| `token`                    | Print the current cockpit bearer token                         |
| `rotate-token`             | Delete runtime_token, reload service, print fresh token        |
| `env`                      | List keys in the remote `~/.gitoma/.env` (values masked)       |
| `config set KEY=VALUE`     | Safely upsert a single key in the remote `.env` (prompts restart) |
| `health`                   | End-to-end probe via `/api/v1/health` (LM Studio + GH token)   |
| `open`                     | Open the cockpit URL in the local browser                      |
| `ssh [args]`               | Interactive shell on the Mac Mini (or run a one-off command)   |
| `uninstall`                | Remove plist · venv · source · runtime_token · logs (prompts)  |

Shim: `deploy/deploy-to-minimac.sh` still works and calls `minimac deploy`.

## Env overrides (all optional)

```bash
MINIMAC_IP=100.98.112.23        # default
MINIMAC_USER=$USER              # default
MINIMAC_HOST=user@host          # bypasses USER/IP if set
INSTALL_DIR=gitoma              # default, relative to remote $HOME
SERVICE_PORT=8000               # default
```

## First-time setup on the Mac Mini

The deploy script needs Python ≥ 3.12 on the Mac Mini. One-time via
Homebrew:

```bash
ssh 100.98.112.23
# inside the Mac Mini:
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
brew install python@3.12
exit
```

After that: `deploy/minimac deploy` from your dev machine.

Paste `GITHUB_TOKEN` into `~/.gitoma/.env` on the Mini once (the deploy
script warns if missing and never ships secrets over the wire):

```bash
deploy/minimac ssh 'echo "GITHUB_TOKEN=ghp_..." >> ~/.gitoma/.env'
deploy/minimac restart
```

## Typical workflows

```bash
# I changed code — push to minimac and reload
deploy/minimac deploy

# The cockpit looks weird — is the service even up?
deploy/minimac status

# What's actually happening on the Mac Mini right now?
deploy/minimac logs -f

# I leaked the token somewhere — mint a new one
deploy/minimac rotate-token

# Blow it all away (keeps only ~/.gitoma/.env)
deploy/minimac uninstall
```

## Security posture

- Tailscale E2E encrypts everything on the wire; `100.98.112.23` is only
  reachable inside your tailnet.
- The launchd service binds to `0.0.0.0:8000` — any Tailscale peer can
  reach the cockpit, but nothing outside the tailnet can.
- Every `/api/v1/*` call requires the bearer token; the WS `/ws/state`
  does too if a token is configured (same token).
- The deploy script **never** ships `GITHUB_TOKEN` over rsync or SSH stdin;
  you paste it manually into `~/.gitoma/.env` on the Mini.
- The deploy script **never** clobbers existing `~/.gitoma/.env` values,
  only appends missing keys.
