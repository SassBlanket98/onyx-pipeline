# onyx-job

A CLI that runs Onyx's task pipeline: investigate, plan, build, verify, review, and summarize. Onyx starts a job and relays the five-line summary; the script owns the rest.

Onyx is my personal assistant: a small local model (9B) that I talk to over Telegram. A model that size cannot be trusted to plan, build and check code changes by itself, so it does one thing: start a job. This script then runs each stage with a suitable worker (Claude Code, Codex, or a cheaper CLI), works in a separate git worktree, stops for my approval after the plan, and sends me the result.

The defaults (paths under `~/.openclaw`, the Telegram session) are from my own setup and are all overridable through the environment variables below. The test suite runs without any of it: workers are replaced by stub binaries.

## Install

From this repo, with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install -e .
```

That puts `onyx-job` on your PATH (typically `~/.local/bin/onyx-job`).

For development:

```bash
uv sync --group dev
uv run pytest -q
uv run ruff check
uv run ruff format --check
```

## Commands

```text
onyx-job start --kind code|research|ops [--repo <path>] [--auto --auto-quote <quote>] [--deep | --quick] [--allow-duplicate] [--request-id <id>] "<task statement>"
onyx-job status [<id>]
onyx-job list
onyx-job summary <id>
onyx-job approve [<id>]
onyx-job answer [<id>] "<text>"
onyx-job revise [<id>] "<text>"
onyx-job cancel <id> --confirm
onyx-job rerun <id> --stage review
onyx-job rerun <id> --stage verify
onyx-job rerun <id> --stage fact-check
onyx-job rerun <id> --stage review --fix
onyx-job wait <id> [--poll <seconds>]
onyx-job resume
```

`rerun` re-runs a single stage of a finished job on its existing branch and never rebuilds.

`start` prints the job id and returns at once. A detached runner (`python -m onyx_pipeline.cli _run`) processes the queue under an exclusive lock on `<jobs>/.lock`. Further jobs wait in order (oldest first). One job runs at a time.

`--repo` is required for `--kind code`. `ops` jobs do not accept `--repo`, `--auto`, `--auto-quote`, `--deep`, or `--quick`. Research jobs write a report under `ONYX_RESEARCH_DIR`.

## Environment variables

| Variable | Default |
|---|---|
| `ONYX_CONFIG_FILE` | `~/.config/onyx-job/config.env` |
| `ONYX_JOBS_DIR` | `~/.openclaw/workspace-onyx/jobs` |
| `ONYX_RESEARCH_DIR` | `~/.openclaw/workspace-onyx/research` |
| `ONYX_ALLOWED_ROOTS` | `~/Projects:/mnt/more-extra/Repos` |
| `ONYX_CURSOR_BIN` | `cursor-agent` |
| `ONYX_CLAUDE_BIN` | `claude` |
| `ONYX_CODEX_BIN` | `codex` |
| `ONYX_OPENCLAW_BIN` | `openclaw` |
| `ONYX_NOTIFY_SESSION` | `agent:onyx:telegram:direct:<chat id>` (placeholder default `...:0`; set your own) |
| `ONYX_NOTIFY_ACCOUNT` | `onyx` |
| `ONYX_NOTIFY_MODEL` | empty |
| `ONYX_NOTIFY_TO` | empty |
| `ONYX_WORKER_TIMEOUT` | `2700` (seconds) |
| `ONYX_VERIFY_TIMEOUT` | `1200` (seconds) |
| `ONYX_QUIET_HOURS` | `23-7` (local time) |
| `ONYX_NO_NOTIFY` | empty |
| `ONYX_NO_DELIVER` | empty |
| `ONYX_DUP_WINDOW_MIN` | `30` (minutes) |
| `ONYX_PROGRESS_API` | `https://api.telegram.org` |
| `ONYX_PROGRESS_CHAT_ID` | `ONYX_NOTIFY_TO`, or the final component of `ONYX_NOTIFY_SESSION` |
| `ONYX_OPENCLAW_CONFIG` | `~/.openclaw/openclaw.json` |
| `ONYX_NO_PROGRESS` | empty |
| `ONYX_LIMIT_CURSOR` | `3` (how many plan parts may build at once) |
| `ONYX_MAX_PARTS` | `3` (maximum `## PARTS` entries before the plan is treated as one part) |
| `ONYX_AUTO_APPROVE` | empty |
| `ONYX_THINK_BACKEND` | `cursor` |
| `ONYX_THINK_MODEL` | `opencode-go/mimo-v2.6-pro` |
| `ONYX_OPENCODE_BIN` | `~/.opencode/bin/opencode` |
| `ONYX_BUILD_BACKEND` | `opencode` |
| `ONYX_BUILD_MODEL` | `opencode-go/deepseek-v4.1-flash` |
| `ONYX_IDLE_LIMIT` | `600` (seconds) |
| `ONYX_WATCH_POLL` | `20` (seconds) |
| `ONYX_RESEARCH_TIMEOUT` | `1200` (seconds) |
| `ONYX_DEEP_THRESHOLD` | `4` |
| `ONYX_PLAYBOOKS_DIR` | `~/.openclaw/workspace-onyx/playbooks` |
| `ONYX_AGENTS_MD` | `~/.openclaw/workspace-onyx/AGENTS.md` (the assistant's rules file, passed to the playbook writer) |
| `ONYX_THINK_TIMEOUT` | `360` (seconds) |
| `ONYX_THINK_DAILY_LIMIT` | `8` |
| `ONYX_THINK_MAX_REPAIRS` | `2` |

Repos must resolve under an allowed root. A directory that is not a git repo is initialised with `git init` and an "Initial commit (onyx-job)". The pipeline never checks out a branch in the user's working tree: each code job gets `git worktree add <job dir>/worktree -b onyx/<id> <current-branch>`. Investigate and planning stay read-only in the user's repo; build, verify, review, fix, and commits run in that worktree. Uncommitted files in the user's checkout are left alone. On `done` or `cancelled` the job worktree is removed (`git worktree remove --force`) and the `onyx/<id>` branch stays; on `failed` the worktree is kept for inspection and named in the summary.

## Plan parts

Claude may split a plan with a `## PARTS` section (at most `ONYX_MAX_PARTS` pieces that don't edit the same files). Independent parts build at the same time in separate worktrees, up to `ONYX_LIMIT_CURSOR` Cursor workers, then merge into the job branch. `AFTER` lists parts that must finish first. A merge conflict marks the part `clashed` and rebuilds it on the already-merged code. If `## PARTS` is missing, malformed, cyclic, over the limit, or has overlapping files on independent parts, the job builds as one part.

The summary adds `Built in N parts (M rebuilt after a merge clash)` when a split was used, or the `split_note` when the plan was treated as one part.

## Claude isolation

Planning and research-writing calls use Claude with read-only tools, the job directory added, project settings only, and an appended system prompt. Planning uses `claude-sonnet-5-5` at medium effort; research-writing uses `claude-sonnet-5-5` at high effort. The plan call is equivalent to:

```text
claude -p --model claude-sonnet-5-5 --effort medium --allowedTools Read,Grep,Glob --disallowedTools Edit,Write,Bash,NotebookEdit --output-format text --add-dir <job dir> --setting-sources project --append-system-prompt <worker instructions> <prompt>
```

`--setting-sources project` limits Claude to project settings. Planning and research-writing calls pass explicit model and effort values, allow `Read,Grep,Glob`, and disallow `Edit,Write,Bash,NotebookEdit`.

## Quiet hours

On `done`, `needs-answer`, and `needs-approval`, the notifier sleeps until the end of quiet hours before waking Onyx. `failed` notifies immediately. `ONYX_NO_NOTIFY` skips both the sleep and the `openclaw` binary.

## onyx-think

`onyx-think` asks Grok 4.6 (through `cursor-agent` in read-only ask mode; `ONYX_THINK_BACKEND=opencode` with `ONYX_THINK_MODEL` runs a cheap OpenCode Go model through `opencode run --agent plan` instead, and Grok is never sent through OpenCode) for a step-by-step playbook when Onyx has no written procedure for a task. Playbooks live as markdown files under `ONYX_PLAYBOOKS_DIR`. Worker output is appended to `.think.log`; each thinker invocation is recorded in `.usage.jsonl`. Failed parses are saved under `.history/`.

```text
onyx-think "<task>"
onyx-think worked <slug>
onyx-think failed <slug> "<what went wrong>"
onyx-think list
onyx-think show <slug>
onyx-think usage
```

Each playbook is `<slug>.md`. Before a repair, the previous version is copied to `.history/<slug>-<n>.md`.

## Tests

Stub worker binaries live in `tests/stubs/` and are selected through `ONYX_*_BIN`. Tests call the run loop directly; they do not spawn the detached runner.

## Licence

MIT. See [LICENSE](LICENSE).
