# Prerequisites

## Python

Gitoma targets **Python 3.12 or later**. The mypy configuration enforces it and several features (PEP 695 type parameters in a few helpers, `Path.is_relative_to`) require it.

```bash
python --version   # must be >= 3.12
```

## A local OpenAI-compatible LLM endpoint

Gitoma plans and writes patches with a language model you run locally. Any endpoint that speaks the OpenAI Chat Completions protocol works — the two most common setups are:

### LM Studio (recommended for first-time users)

1. Install [LM Studio](https://lmstudio.ai).
2. Download a capable code model. The default Gitoma config points at `gemma-4-e2b-it`; bigger models produce better patches.
3. Start the server on port `1234` from the "Local Server" tab. Gitoma expects `http://localhost:1234/v1`.

### Ollama

1. `brew install ollama` (or see the [Ollama docs](https://ollama.com)).
2. Pull a model: `ollama pull qwen2.5-coder:14b` (or any other).
3. Start: `ollama serve`.
4. Tell Gitoma where it lives:

   ```bash
   gitoma config set LM_STUDIO_BASE_URL=http://localhost:11434/v1
   gitoma config set LM_STUDIO_MODEL=qwen2.5-coder:14b
   ```

::: tip
The env variable names begin with `LM_STUDIO_` for historical reasons — the *protocol* is the contract, not the vendor. Any OpenAI-compatible endpoint works.
:::

## A GitHub Personal Access Token

Gitoma writes to your repositories via the GitHub REST API and `git push`. It needs a token with enough scope to do both.

### Fine-grained PAT (recommended)

Create one at [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens):

- **Repository access**: select only the repos you want Gitoma to touch.
- **Repository permissions**:
  - Contents — **Read and write**
  - Pull requests — **Read and write**
  - Issues — **Read**
  - Metadata — Read (granted automatically)

### Classic PAT

If you must, a classic token with the `repo` scope works too. Less scope control; prefer fine-grained when you can.

### Verifying the token

Once configured, use `gitoma doctor --push <repo-url>` to walk the full push-permission chain — token identity, scopes, repo visibility, collaborator role, branch protection, plus an active write probe that creates and deletes a throwaway ref.

```bash
gitoma doctor --push https://github.com/owner/repo
```

## System

- **git** on the PATH (used by `GitPython` under the hood).
- **macOS or Linux.** Gitoma works on POSIX for the full feature set — the concurrent-run lock uses `fcntl.flock`, the subprocess cancel path uses `os.killpg`. It runs on Windows but without process-group isolation.

You are ready. Head to [Install](./install) next.
