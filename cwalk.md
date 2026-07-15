# ciwalk — Design Doc

**Project name:** `ciwalk`
**One-liner:** Run your CI pipeline locally, pause at any step, drop into a live shell to inspect/fix state, then resume — no more commit-push-pray.

---

## 1. Problem Statement

CI pipelines today force a slow, blind feedback loop: write YAML → commit → push → wait → read logs → guess what broke → repeat. Developers can't inspect the actual runtime state at the point of failure without SSH-ing into a real runner (which most people don't have access to) or manually recreating the environment.

This is a real, explicitly requested gap, not a made-up problem. From an "Ask HN: What developer tool do you wish existed in 2026?" thread:

> "I could use a sane CI system. I hate DevOps. I have to do multiple commits to implement something. I would love to be able to have access to the same env as the CI so that I could prototype the script/job on my own machine before committing to git... I hate that I need to write commands in Yaml files, commit (or use the browser) and then look at the result. Solve this and I would pay for it."
> — [Ask HN, Jan 2026](https://news.ycombinator.com/item?id=46345827)

A follow-up commenter specifically asked for a step-through debugging interface:

> "Perhaps the ability to stop at a specific point in the script and being able to modify any commands and execute the step and then continue the script until it fails again... a debugging interface would be a killer feature."

**Existing tools and why they fall short:**
| Tool | What it does | Gap |
|---|---|---|
| [`act`](https://github.com/nektos/act) | Runs GitHub Actions locally, start to finish | No pause/inspect/resume — still all-or-nothing |
| GitHub Codespaces / SSH debug actions | Lets you SSH into an actual failed CI run | Requires pushing first; not local; costs CI minutes |
| `gitlab-ci-local` | Similar to `act`, for GitLab CI | Same limitation — no interactive step debugging |

**Our differentiator:** persistent, inspectable container state + interactive pause/resume, entirely local.

---

## 2. Target User & Use Case

Any developer who has ever pushed 5 commits in a row just to fix a typo in `.github/workflows/ci.yml`. Primary use case:

1. Dev writes/edits a CI job.
2. Runs `ciwalk run .github/workflows/ci.yml --pause-on-fail`.
3. Pipeline executes step by step in a local container mirroring the CI image.
4. A step fails → tool drops them into a shell **inside that exact container**, with the exact filesystem/env state at time of failure.
5. Dev pokes around, fixes the command, tests it live.
6. Dev types `continue`, and either (a) the tool re-runs the fixed command and proceeds, or (b) resumes with the remaining steps.
7. Once everything passes locally, dev commits — with confidence, not hope.

---

## 3. MVP Scope (2 weeks)

Keep this tight. Do NOT try to support the full GitHub Actions / GitLab CI spec — pick one (GitHub Actions YAML) and support a useful subset.

**In scope:**
- Parse GitHub Actions workflow YAML (`jobs.<job>.steps`, `run`, `uses` for a small set of common actions like `actions/checkout`, `env`, `working-directory`).
- Spin up one Docker container per job using an image equivalent to the GitHub-hosted runner (reuse `catthehacker/ubuntu:act-latest`, same base `act` uses — battle-tested, no need to build our own).
- Execute steps sequentially via `docker exec`.
- On step failure OR when a `--breakpoint <step-name>` flag is set → drop into an interactive shell in the running container.
- `continue` command in that shell (implemented as a sentinel/wrapper, not a real shell builtin) resumes the pipeline.
- Container persists across pauses (don't destroy/recreate) so filesystem state carries over.
- Basic CLI output: step name, status (running/passed/failed/paused), duration.

**Explicitly out of scope for MVP** (mention as "future work" in README):
- GitLab CI / CircleCI / Jenkins support
- Matrix builds / parallel jobs
- Secrets management
- `uses:` for arbitrary marketplace actions (only support a small hardcoded allowlist)
- Caching between runs
- Firecracker/microVM isolation (Docker is enough for v1)

---

## 4. Architecture

```
ciwalk/
├── src/ciwalk/
│   ├── __init__.py
│   ├── cli.py               # CLI entrypoint (typer)
│   ├── parser.py            # YAML → internal Job/Step model
│   ├── container.py         # create/start/exec (docker-py)
│   ├── session.py           # pause/resume/breakpoint state machine
│   ├── shell.py             # interactive TTY bridge + resume menu
│   ├── model.py             # Job, Step, PauseAction dataclasses
│   └── errors.py
├── examples/
├── tests/
├── README.md
└── pyproject.toml
```

**Core flow:**
1. `parser` reads YAML → `list[Job]`, each with `steps: list[Step]` (`name`, `run`, `env`, `working_dir`).
2. `runner.container` creates a container per job (`docker-py`'s `client.containers.run(..., detach=True, tty=True)`), and execs each step's command inside it via `container.exec_run(..., stream=True)`.
3. `runner.session` tracks current step index + pause state. On failure/breakpoint, it opens an interactive exec session into the *same container* (`exec_create` + `exec_start` with `tty=True`, socket attached to the local terminal via `pty`) and hands control to the user's terminal.
4. On `continue`, session closes the interactive exec and resumes the step loop from the next step (or retries the failed one, dev's choice).

**Tech stack:**
- **Python 3.11+** — you're already fluent, so effort goes into the actual architecture (parsing, container orchestration, PTY handling) rather than learning syntax.
- **`docker` (docker-py)** — official Python Docker SDK, well-documented, handles container lifecycle + exec.
- **`click`** (or `typer`, which wraps click with type-hint-based commands) for the CLI.
- **`PyYAML`** for parsing.
- **`pty` / `os.exec*`** (stdlib) for bridging the interactive shell into the container's exec socket.
- **Packaging:** ship as a proper `pip install`-able package (`pyproject.toml`, published to PyPI eventually) — this is a completely normal distribution model for dev tools (`black`, `httpie`, `ansible` all ship this way), so it's not a compromise.

---

## 5. Milestones (2-week plan)

| Days | Deliverable |
|---|---|
| 1–2 | Repo scaffold, YAML parser for a minimal GH Actions subset, unit tests on parsing |
| 3–5 | Docker container lifecycle: create/start/exec a single step, capture stdout/stderr, report pass/fail |
| 6–8 | Sequential multi-step execution within one job/container; basic CLI progress output |
| 9–11 | Pause-on-fail + interactive PTY shell attach into the running container; `continue` to resume |
| 12–13 | `--breakpoint <step>` flag; polish CLI UX (colors, step timing, clear failure messages) |
| 14 | README, 2–3 example workflows, demo GIF/asciinema recording, publish to GitHub |

---

## 6. Demo Script (for README / resume link)

Have a sample `examples/broken-ci.yml` that intentionally fails on step 3 (e.g., a typo'd command or missing dependency). Recording the terminal session showing:
1. `ciwalk run examples/broken-ci.yml`
2. Steps 1–2 pass, step 3 fails
3. Tool drops into a live shell in the container
4. Dev runs `ls`, discovers the missing file, fixes it live
5. Types `continue`, pipeline finishes successfully

This is a much stronger portfolio artifact than a static README — an asciinema recording embedded at the top of the repo.

---

## 7. Resume-Ready Framing

> Built **ciwalk**, an open-source CLI (Python) that runs CI pipelines locally in Docker with interactive step-through debugging — pause execution on failure, inspect/modify container state live, and resume without re-committing. Addressed a recurring developer pain point (validated via community discussion) not solved by existing tools like `act`.

---

## 8. Decisions (locked for MVP)

### Identity
- **Name:** `ciwalk`
- **Language:** Python 3.11+
- **License:** MIT
- **Workflow syntax:** real GitHub Actions subset (not a custom format)
- **CLI:** `typer`
- **Layout:** `src/ciwalk/` (src layout), entry point `ciwalk`
- **Image:** `ubuntu-latest` / omitted `runs-on` → `catthehacker/ubuntu:act-latest`; any other label → clear error and exit

### Pause / resume (core UX)
- On step failure (with `--pause-on-fail`) or `--breakpoint <step>`: drop into interactive shell in the **same** container.
- Resume is **not** typed inside bash as a fake builtin. Flow:
  1. User exits the debug shell (`exit` / Ctrl+D).
  2. CLI prints a short menu: **`retry`** | **`continue`** | **`abort`**.
- **`retry`:** re-run the failed/breakpoint step using the original YAML `run:` (filesystem/env edits in the container are kept).
- **`continue`:** skip to the next step (do not re-run the failed one).
- **`abort`:** stop the run; container cleaned up (unless `--keep`); non-zero exit.
- Without `--pause-on-fail` / `--breakpoint`, failures end the run immediately (no shell). Success runs never pause.
- Host exit code: `0` only if **no** step ended as `FAILED` (retries that eventually pass are fine). `continue` after a failure still exits non-zero. Abort / unhandled failure → non-zero.
- Debug shell matches the step runner: `bash --noprofile --norc`, with the failed step’s merged job+step `env` and `working-directory`.
- `--keep` always prints the container id + `docker exec` hint (success, failure, or abort).
- CLI run status/step output goes to **stdout** (so `ciwalk run … > log` works); fatal errors may still go to stderr.

### Parser / GHA subset
- Supported: `jobs.<id>.steps` with `name`, `run`, `env`, `working-directory`, and `uses` for allowlisted actions only.
- **`uses` allowlist (MVP):** `actions/checkout@*` only → bind-mount host workspace (no real checkout clone required when already local).
- Ignored (parse OK): `on:`, `permissions:`, `concurrency:`, secrets.
- Step-level `if:` → warn once, step still runs (not evaluated).
- **`${{ }}` substitution (MVP):** expand known `${{ inputs.* }}` and `${{ env.* }}` keys only. CLI: `--input KEY=VALUE` (overrides workflow_dispatch / workflow_call defaults). Any other expression (or unknown input/env key) → **ParseError** / refuse to exec — never rewrite to empty string (avoids false-positive `Job passed`).
- **`ciwalk cleanup`:** remove leftover containers labeled/named `ciwalk-*` (recovery after `kill -9`).
- Steps stream `run:` output live (`exec_start(stream=True)`), not buffered until the step ends.
- **Loud SKIP (not silent pass):** job-level reusable workflow (`jobs.<id>.uses`), `needs:`, or job-level `if:` → print `SKIPPED` with reason and exit **non-zero**. No container started.
- Fail fast (unsupported): `shell:`, `continue-on-error:`, `defaults:`, `services:`, checkout `with:`, matrix/`strategy`, unsupported `runs-on` / step `uses:`.
- **Jobs:** run a single job — `--job <id>`, or the first job if only one exists; if multiple and no `--job`, list job ids and exit with an error.

### Container / runtime
- Workspace: bind-mount CWD (or `--workdir`) to `/github/workspace`; default `working-directory` for steps is that path.
- Steps: `docker exec` via `bash --noprofile --norc -eo pipefail -c` for `run:` (matches GHA default shell). Debug shell uses the same bash flags (interactive, no `-c`).
- Minimal fake context env: `CI=true`, `GITHUB_ACTIONS=true`, `GITHUB_WORKSPACE=/github/workspace`, `GITHUB_JOB=<id>`.
- Network: default Docker bridge (enough for `apt`/`npm`/`pip` in demos).
- User: container default (root on act image) — fine for local MVP.
- Lifecycle: one container per job run named `ciwalk-<job>-<id>`; remove on exit; `--keep` retains it and prints the id.
- Docker not running / image missing: auto-pull image once; if daemon unreachable, exit with a clear error.

### CLI surface (MVP)
```
ciwalk run <workflow.yml>
  [--job NAME]
  [--pause-on-fail]
  [--breakpoint STEP_NAME]
  [--image IMAGE]          # override default runner image
  [--workdir PATH]         # host path to mount as workspace
  [--keep]                 # do not remove container on exit
  [--input KEY=VALUE]      # repeatable; for ${{ inputs.* }}

ciwalk cleanup [--dry-run] # remove orphaned ciwalk containers
```

### Demo story (matches resume semantics)
`examples/broken-ci.yml`: steps 1–2 pass; step 3 fails because a required file is missing in the workspace. On pause, user creates the file in the container (or on the mounted host path), exits shell, chooses **`retry`**, step 3 passes, remaining steps finish.

### Explicitly still out of scope
GitLab/Circle/Jenkins; matrix/parallel jobs; secrets; arbitrary marketplace actions; caching; microVMs.