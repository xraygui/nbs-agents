# nbs-agents

Run beamline agents alongside the Bluesky QueueServer.

nbs-agents is the base that agent packages build on:

- **Queue plans and helpers**: `open_agent` / `close_agent` blocks, with a
  `queue_stop` between them, so an agent owns a stretch of the queue.
- **`nbs-agents-runner`**: watches the Bluesky document stream, opens and
  closes agents as their blocks run, and records each agent's session in the
  agent log.
- **Agent log**: one JSON record per session in Redis, either the beamline
  Redis or a Redis of its own that the runner starts (`[agent_log]`).
- **Tiled login**: logs in to Tiled (Entra ID at NSLS-II) when needed, and keeps
  agents a read-only, expiring API key.
- **nbs-gui widgets**: the Agents Log tab and the agent block plan widget.
- **`nbs-agents init/check`**: one folder, `$NBS_AGENTS_HOME`, for every file.

Agents come from other packages, through entry points:

| Group | What it adds |
|---|---|
| `nbs_agents.agents` | an agent class, by the name `open_agent` uses |
| `nbs_agents.runner` | `plugin(runtime)` returning supervisor observers (`on_run_stop`, `on_agent_opened`, `on_agent_closed`, `shutdown`) |
| `nbs_agents.launchers` | `func(goal, budget)` returning `open_agent` parameters, offered by the Qt "New agent…" dialog |

`nbs-agents-gp` (Gaussian-process agents) is built this way. The Claude agents
and the beamline assistant ship with nbs-agents itself, behind the
`[assistant]` extra (see below); `sst-assistant` adds the SST specifics.

## Installation

```bash
git clone https://github.com/xraygui/nbs-agents.git
cd nbs-agents
pixi install
```

Usually nbs-agents is installed as a dependency of an agent package, whose
own `init` and `check` run nbs-agents' first.

## Beamline startup

Import the control plans in the beamline IPython startup:

```python
from nbs_agents.load import load_plans
load_plans(globals())
```

## Setting up an account

Everything lives under `$NBS_AGENTS_HOME` (default `~/.nbs-agents`), with one
folder per beamline:

```
<beamline>/
    config/   nbs-cli.toml (settings)                                mode 700
    state/    agent-log/ (the agent log's Redis snapshot),
              tiled-agent/ (the agents' Tiled key, mode 700)
    data/     shared data such as attachments and knowledge-base notes
    work/     agent working folders and logs
tiled/        your Tiled login, shared by every beamline (mode 700)
```

```bash
pixi run nbs-agents init --beamline sst1 --tiled-uri https://... --tiled-path BEAMLINE/raw
$EDITOR ~/.nbs-agents/sst1/config/nbs-cli.toml     # beamline addresses
pixi run nbs-agents check                          # files, permissions, services
pixi run agent-runner                              # or assistant-runner; see "Running it" below
```

Every command takes `--beamline NAME` (or `$NBS_BEAMLINE`). With only one
beamline set up it may be left out; with several, a command that isn't told
which one stops and lists them (`nbs-agents beamlines`). The runner passes its
beamline on to the agents it starts. `nbs-agents paths` prints every location,
and `nbs-agents tiled-login` logs in to Tiled ahead of time. Settings come from
`$NBS_CLI_CONFIG` or the beamline's `config/nbs-cli.toml` (table `[nbs_cli]`),
overridden by `NBS_<NAME>` environment variables; see
`nbs_agents.config.AgentsConfig`.

### Agent log

Each session is a JSON record under `{prefix}AGENT{agent_uid}`: `goal`,
`status`, `budget`, `opened`, and an append-only `log` of
`{t, kind, text, ...refs}` (see `nbs_agents.agentlog`). With

```toml
[agent_log]
port = 60740
serve = true
```

the runner starts `redis-server` on 127.0.0.1:60740 (from the pixi
environment), keeps a snapshot in the beamline's `state/agent-log`, and publishes the address
in the beamline Redis so nbs-gui finds it without this config. Without
`port`, the agent log shares the beamline Redis.

### Tiled login

Each install names its Tiled server and run catalog (`tiled_uri`,
`tiled_path`). When the server needs a login, the runner starts one: Tiled
prints a link and code and opens a browser, and the runner publishes the link
in the agent log for web pages to show. The login is cached in `$NBS_AGENTS_HOME/tiled`.
From it the runner makes agents a read-only (`read:metadata`, `read:data`) API
key that expires after `tiled_key_lifetime_s`, renews it, and revokes it on
exit (`state/tiled-agent/api-key.json`). A fixed `TILED_API_KEY` skips this.

## Claude agents and the beamline assistant

The `[assistant]` extra (`pip install nbs-agents[assistant]`: fastapi, uvicorn,
matplotlib, pyyaml, pyepics) adds Claude Code agents:

- **Steerable Claude agents**: an operator opens a session with a goal (nbs-gui
  "New agent…", the web page, or `nbs-cli open`); `nbs-agents-runner` starts
  Claude Code (`claude-beamline`), which plans and queues measurements through
  `nbs-cli`.
- **The beamline assistant**: a read-only Claude that triages every run, posts
  summaries, advice and alerts, and answers operator questions.
- **`nbs-cli`**: the command-line interface agents use for the QueueServer,
  runs, devices, the agent log and the knowledge base.
- **`nbs-agents-web`**: a web page for watching and steering agents.
- **nbs-gui Assistant tab** (`nbs-agents-assistant`).
- **`nbs-agents-assistant init/check/runner/timing`**: nbs-agents' own
  `init`/`check` plus the assistant's settings, token, workdir and checks.

nbs-agents registers them through its own entry points (the `claude` agent,
runner plugin and launcher). A beamline package adds its specifics through these
groups:

| Group | What it adds |
|---|---|
| `nbs_agents.config_defaults` | a dict of tables (`{"nbs_cli": {...}, "assistant": {...}}`) of default settings, under the config file and `NBS_*` variables |
| `nbs_agents.reducers` | `reducer(start, data, ctx, summary)` returning extra run-summary values |
| `nbs_agents.sample_layout` | `layout(samples)` for `nbs-cli samples --layout` |
| `nbs_agents.skill_addenda` | a folder of `<skill-name>.md` files appended to the bundled skills |

sst-assistant uses all four, and wraps `nbs_agents.assistant.admin.main` as
`sst-assistant` with its own config presets.

#### Running it

The pixi tasks are the same in nbs-agents (its default environment has the
`[assistant]` extra) and in sst-assistant, which runs `sst-assistant` where
this table says `nbs-agents-assistant`:

| Task | Command | What runs |
|---|---|---|
| `pixi run init` | `nbs-agents-assistant init` | nbs-agents' init, then the assistant's settings, token and workdir |
| `pixi run check` | `nbs-agents-assistant check` | nbs-agents' checks, then the assistant's |
| `pixi run timing` | `nbs-agents-assistant timing` | where the newest Claude call's time went |
| `pixi run agent-runner` | `nbs-agents-assistant runner` | the runner with Claude drivers and the assistant |
| `pixi run agent-web` | `nbs-agents-web` | the web page |
| `pixi run assistant-runner` | `nbs-agents-assistant runner --assistant-only` | the runner with the assistant only (`NBS_DRIVERS=false`) |
| `pixi run assistant-web` | `nbs-agents-web --assistant-only` | the web page, assistant only |

Each takes `--beamline NAME`. `nbs-agents-assistant runner` is `nbs-agents-runner` plus the
`--assistant-only` switch; run the runner through it (or these tasks) rather
than `nbs-agents-runner` directly.

### nbs-cli

`nbs-cli` lets something outside the supervisor (Claude in a terminal, a script,
or a person) read the beamline and drive its own agent block. Every command
prints one JSON document; errors print `{"error": ...}` and exit 1.

| Command | What it does |
|---|---|
| `nbs-cli state [--full]` | Manager state, beam at the endstation, running item, the session's block and budget, other queue counts, samples, plans, recent run summaries |
| `nbs-cli beam [--map]` | Whether beam is expected at the endstation and why not, from nbs-bl's `BEAM_MAP`; `--map` adds every node's upstream, children and status |
| `nbs-cli plans [name]` | Allowed plans and XAS edges, or one plan's parameters, docstring and time-estimate rule |
| `nbs-cli run [ref] [--data] [--plot f.png]` | One run (uid, uid prefix, scan id, or `-1`) with a reducer summary |
| `nbs-cli runs [-n 10] [--mine]` | Recent run summaries |
| `nbs-cli wait [--until run\|idle\|change] [--timeout 600]` | Block on the document stream; returns finished-run summaries |
| `nbs-cli open --goal "..." [--budget 2h]` | Queue `open_agent("claude")`, `queue_stop`, `close_agent` and start the queue if it is at the head |
| `nbs-cli add <plan> [v ...] [k=v ...] --reason "..." [--dry-run]` | Insert a plan before the session's `queue_stop`, tagged with `agent_uid`, `agent_sequence`, `agent_reason`; checks the allowlist and budget; starts the queue if it is idle at this session |
| `nbs-cli devices [--movable]` | Devices the QueueServer user group may pass to plans (motors, detectors) |
| `nbs-cli remove <item_uid> --reason "..."` | Remove a pending item, only from the session's own block |
| `nbs-cli note "..."` | Append a note to the agent log |
| `nbs-cli ask "..."` | Ask the operator a question; the session shows as waiting for input until they reply |
| `nbs-cli finalize --summary "..." [--drop-pending]` | Log a summary, remove the session's `queue_stop` and let the queue run to `close_agent` |
| `nbs-cli log [--all]` | Agent-log records |
| `nbs-cli tell "..."` | Operator: leave a message for the running session |
| `nbs-cli stop [--reason "..."]` | Operator: ask the running session to wrap up |

Operators steer a running session through its agent log, from the nbs-gui
Agents Log tab or with `tell`/`stop`. `state`, `wait` and `add` return any
unseen operator messages under `operator`, `wait` returns early with reason
`operator_message` when one arrives, and once a stop is requested `add` is
refused and the session should `finalize`.

The Agents Log tab picks a session from a drop-down and shows its notes,
questions, operator messages and final summary as a conversation, above a table
of the rest of the log (plans added and removed, runs, status). After `ask`,
the record's `awaiting_input` field is set until the next operator message,
stop, `finalize` or `close`; meanwhile the tab shows an amber "waiting for your
input" banner and counts waiting agents in its title.

#### A steerable Claude Code agent

`src/nbs_agents/assistant/skills/beamline-steering/SKILL.md` is a Claude Code skill that teaches the
driving loop with no beamline-specific task: open a session, wait for the
operator, plan measurements with them through the log, queue plans with
reasons, and finalize when told to stop. `claude-beamline` writes it (and the
beamline-knowledge skill) into the agent's working folder, with any beamline
addenda appended; outside a sandbox you can also link it into your skills:

```bash
ln -sfn "$PWD/src/nbs_agents/assistant/skills/beamline-steering" ~/.claude/skills/beamline-steering
```

Then start an agent from any folder with `claude-beamline` (interactive) or
`claude-beamline --headless` (`claude -p`, stream-json); the console script
runs the bundled `src/nbs_agents/assistant/scripts/claude-beamline` with
`NBS_ENV_DIR` set to its python environment. It puts that environment's
`nbs-cli` on PATH and allows only `nbs-cli` commands. Steer it from the Agents
Log tab or with `nbs-cli tell`.

`claude_sandbox` (`[nbs_cli]`, `NBS_CLAUDE_SANDBOX` or `--sandbox`) picks the
wrapper: `none` (the default) runs Claude directly, `bwclaude` runs it inside
bwclaude from n2snscripts, through the NSLS-II API gateway, with the python
environment, its editable sources, the config and the agents' folders mounted.
`nbs-agents-assistant check` fails while bwclaude or the gateway key is
missing, when the sandbox is bwclaude.

Readings the assistant must always see, even with no Bluesky session
publishing them, are read straight from EPICS: name them in the `[status]`
table of the profile's `beam_map.toml`, and `beam` and `state` report them
under `status`: numbers with their units, enums as their state strings.

```toml
# beam_map.toml
[status.ring_status]
pv = "SR-OPS{}Mode-Sts"
desc = "Beam operation status"     # optional; reported with the value

# nbs-cli.toml
[nbs_cli]
beam_map_file = "/path/to/profile_collection/startup/beam_map.toml"
epics_ca_addr_list = "..."      # bwclaude drops EPICS_CA_* from the environment
```

Where managed settings turn on Claude Code's own Bash sandbox (as on the SST
workstations), every command it runs has no network. `claude-beamline` passes
`--settings` with `"excludedCommands": ["nbs-cli *"]` so that nbs-cli calls run
outside it, still inside bwclaude. Only a bare `nbs-cli ...` call matches: a
redirect, pipe or `&&` keeps the call sandboxed, and the skills say so.

`open` needs `nbs-agents-runner` running: its `ExternalAgent` (registered as
`external` and `claude`) holds the block open and records the session.

#### Where a call's time goes

`nbs-agents-assistant timing [--beamline NAME]` reads the newest
Claude call log, or the logs you name (`-n 5` for the five newest), and splits
its time into startup, model (waiting on the API with no tool running) and
tools, with the slowest tool calls:

```
claude-assistan-20261103-131605.log
  74.7 s total, 16 turns, API 70.1 s, $0.49
  startup (launch to first model output): 7.5 s
  model (no tool running):               63.4 s
  tools (a tool running):                5.8 s
  slowest tool calls:
       3.6 s  nbs-cli state
```

#### Assistant only

To run the read-only assistant without any Claude agent that drives the
beamline, set

```toml
[nbs_cli]
drivers = false        # or NBS_DRIVERS=false
```

or start the runner and web page with `--assistant-only`:

```bash
nbs-agents-assistant runner --assistant-only
nbs-agents-web --assistant-only
```

The runner then launches no driver and closes any `claude` session as soon as
it opens (releasing its `queue_stop`), "New agent…" is refused (the web page
hides it), and `nbs-cli` refuses every verb that queues or steers, as in the
assistant role. `nbs-agents-assistant check` shows "drivers off". For a hard
guarantee, also give the driver's QueueServer group (`qs_user_group`) no
allowed plans.

#### Starting a Claude agent from nbs-gui

The Agents Log tab has a **New agent…** button next to the Agent picker. It asks
for a goal (required), an optional budget such as `90m` or `2h`, and whether to
start the queue if it is idle. It queues
`open_agent("claude", {"goal": ..., "budget": {"time_s": ...}, "launch": true})`,
`queue_stop` and `close_agent` at the back of the queue (the same helper
`nbs-cli open` uses), and starts the queue only if the block is at its head.
The tab shows whether the runner's heartbeat is fresh; the button is disabled
while the runner is down. The Agent Block plan widget offers the same `claude`
block (goal and budget) without starting the queue.

When that `open_agent` runs, `nbs-agents-runner` starts Claude itself:
`claude_command --headless` in `claude_workdir`, with `NBS_AGENT_UID`,
`NBS_AGENT_GOAL` and `NBS_DRIVER` set, stdout and stderr in a per-session file in
`claude_log_dir`. `claude-beamline` sees `NBS_AGENT_UID`, attaches to the
open session instead of calling `open` (nbs-cli reads `NBS_AGENT_UID` as the
default `--agent-uid`), and plans the goal with the operator before queueing.
The session record gets `driver` and `launch` (`pid`, `command`, `workdir`,
`log_path`), and the log gets `launch` entries when Claude starts and exits, or
an `error` entry if it cannot start (the runner keeps going). When the session
closes, the runner sends Claude SIGTERM, then SIGKILL after
`claude_stop_timeout_s`. Stopping the runner leaves a launched Claude running.

So the runner must run on a host where `claude` is installed and logged in, with
the beamline-steering skill linked into `~/.claude/skills`. Launch settings, in
`[nbs_cli]` or as `NBS_CLAUDE_*` variables:

```toml
claude_command = "claude-beamline"            # empty turns launching off; a bare name not
                                              # on PATH is taken from the bundled scripts
claude_workdir = "$NBS_AGENTS_HOME/work/sessions/{uid8}"   # default; {uid}, {uid8} filled in
claude_log_dir = "$NBS_AGENTS_HOME/work/logs"               # default
claude_stop_timeout_s = 10.0
```

Settings come from `$NBS_CLI_CONFIG` or `config/nbs-cli.toml` under `$NBS_AGENTS_HOME` (table
`[nbs_cli]`), overridden by `NBS_<NAME>` environment variables. The runner reads
the same settings.

```toml
[nbs_cli]
qs_addr = "tcp://localhost:60615"
zmq_addr = "localhost:5578"
tiled_uri = "http://localhost:8000"
tiled_api_key = "secret"
redis_host = "localhost"   # Redis with the nbs-bl status dicts (settings.redis.info)
redis_port = 60737
redis_db = 4
redis_prefix = "claude-"
qs_user_group = "primary"
signal = "sc"              # summary signal and normalization keys (no default)
norm = "i0"
# allowed_plans = ["nbs_count", "move_sample"]
```

#### Agent log

Sessions, operator messages and the assistant's posts live in the nbs-agents
agent log (see "Agent log" above for where it is kept). nbs-cli writes
`add`, `remove`, `note`, `question` and `finalize` entries.

#### Web UI

`nbs-agents-web` serves the nbs-gui "Agents Log" and "Assistant" tabs as a
page on localhost, for watching and steering agents without the Qt GUI:

```bash
nbs-agents-web            # http://localhost:8765; --port, --config as for nbs-cli
```

It reads the same nbs-cli settings, and shows agent sessions with their
conversation, activity and queued block, beam status from the beam map, the
queue state and the runner heartbeat. From the page you can message a running
agent, request a stop, queue a new Claude agent, and ask the assistant. It
binds to 127.0.0.1 and only accepts writes from its own page. It needs
`fastapi` and `uvicorn` (the `assistant` extra).

Message text is Markdown with `$...$` and `$$...$$` math, written so it still
reads well as plain text. A figure is an image link to an attachment,
`![alt](att:ID)`, stored by `nbs_agents.assistant.attachments.AttachmentStore` (a
folder, `$NBS_ATTACHMENTS_DIR` or `data/attachments` under `$NBS_AGENTS_HOME`).
The page renders Markdown, math and figures (markdown-it, KaTeX and DOMPurify
from jsDelivr, falling back to plain text offline) and lists the shown
sessions' figures in a side panel. Plain views use
`nbs_agents.richtext.plain_text`, which turns figures into `[figure: alt]`.

The page needs a login token, as Jupyter does, so another user on the machine
cannot open it and act as you. The token lives in `config/token` under `$NBS_AGENTS_HOME`
(`$NBS_AGENTS_TOKEN_FILE`), mode 600 in a mode 700 folder, and is made on first
start; a file that is not yours or that others can read is refused.
`nbs-agents-web` prints a `http://localhost:8765/?token=...` link at start:
opening it sets a cookie, otherwise the page asks for the token. Scripts can
send `Authorization: Bearer <token>`. To change the token, delete the file and
restart nbs-agents-web. Programs on the machine itself (the runner, nbs-cli,
nbs-gui) do not use it.

## Tests

```bash
pixi run -e test test
```

The Qt tests need nbs-gui and are skipped without it.

## License

Public domain.
