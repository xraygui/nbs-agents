# NBS Agent Framework

Agentic automation for NSLS-II beamline operation, built on Bluesky, QueueServer, and Tiled.

## Features

- ZMQ document catalog for live Bluesky runs
- Persistent agent runner with queue sentinel control
- QueueServer integration for plan submission
- Extensible agent callbacks on run completion

## Installation

```bash
git clone https://github.com/xraygui/nbs-agents.git
cd nbs-agents
pip install -e .
```

Or with pixi (which also brings `redis-server` for the agent log):

```bash
pixi install
```

Install from a checkout (editable): the runner uses this repo's `scripts/`,
`skills/` and `templates/` in place.

### Setting up a beamline account

Everything nbs-agents keeps lives under one folder, `$NBS_AGENTS_HOME`
(default `~/.nbs-agents`):

```
config/   nbs-cli.toml, token (web login), optional gateway.env        mode 700
state/    agent-log/ (the agent log's Redis snapshot), tiled/ (your Tiled
          login, mode 700), tiled-agent/ (the agents' Tiled key, mode 700)
data/     attachments/ (figures), kb/ (agents' knowledge-base notes)
work/     sessions/<uid8>/ (driver workdirs), assistant/, logs/
```

```bash
export NBS_AGENTS_HOME=...          # optional; e.g. a staff account's area
pixi run nbs-agents init --tiled-uri https://... --tiled-path BEAMLINE/raw
                                    # make the layout and a config for this beamline
$EDITOR $NBS_AGENTS_HOME/config/nbs-cli.toml    # beamline addresses, kb_dir, models
# load AIFAPIM_HOST and AIFAPIM_API_KEY (e.g. from your gpg-encrypted .env) for --sandbox bwclaude
pixi run nbs-agents tiled-login     # optional: log in to Tiled now (the runner asks when needed)
pixi run nbs-agents check           # files, permissions, bwclaude, redis-server, services
NBS_CLAUDE_SANDBOX=bwclaude pixi run agent-runner
pixi run agent-web                  # open the printed ?token= link
```

`nbs-agents paths` prints every location. `nbs-agents init` never overwrites a
file; `--refresh-assistant` updates the assistant's workdir (`CLAUDE.md`,
`.claude/settings.json`) from `templates/assistant/`, keeping old copies as
`*.bak`. Settings and variables that name a path (`NBS_CLI_CONFIG`,
`attachments_dir`, `NBS_GATEWAY_ENV`, ...) still win.

### Tiled login

Each install names its Tiled server and run catalog: `tiled_uri` and
`tiled_path` in `[nbs_cli]`, which `nbs-agents init --tiled-uri ... --tiled-path
...` writes; `nbs-agents check` warns while `tiled_path` is unset. When the server needs a login (Entra ID at NSLS-II),
`nbs-agents-runner` starts one as it starts: Tiled prints a link and a code and
opens a browser, and the web page shows the same link until you finish. The
login is cached in `state/tiled` and refreshed, so later starts are usually
silent; `nbs-agents tiled-login` logs in ahead of time, and `nbs-cli` in a
terminal asks the same way.

Agents never use your login. The runner makes them a read-only
(`read:metadata`, `read:data`) Tiled API key that expires after
`tiled_key_lifetime_s` (a day), replaces it after two thirds of that, and
revokes it on exit. It lives in `state/tiled-agent/api-key.json`, whose folder
`claude-beamline --sandbox bwclaude` mounts read-only. An agent without a valid
key is told to ask the operator. A fixed `TILED_API_KEY` (or `tiled_api_key`)
skips all of this.

nbs-gui needs nbs-agents in its own environment for the Agents Log and
Assistant tabs, but no nbs-agents config: it finds the agent log through the
beamline Redis.

For GUI queue widgets:

```bash
pip install -e ".[gui]"
```

## Beamline Startup

Import the control plans in the beamline IPython startup sequence:

```python
from nbs_agents.load import load_plans
load_plans(globals())
```

Or import directly:

```python
from nbs_agents.plans import open_agent, close_agent
```

## Usage

Start the agent runner:

```bash
nbs-agents-runner
```

Or via pixi:

```bash
pixi run agent-runner
```

Submit an agent block to the queue:

```text
open_agent(agent_name, agent_parameters)
queue_stop
close_agent()
```

The GUI widget `nbs-agents-block` inserts all three items together when `nbs-agents[gui]` is installed and enabled in `gui_config.toml`.

## nbs-cli

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

### A steerable Claude Code agent

`skills/beamline-steering/SKILL.md` is a Claude Code skill that teaches the
driving loop with no beamline-specific task: open a session, wait for the
operator, plan measurements with them through the log, queue plans with
reasons, and finalize when told to stop. Link it into your skills once:

```bash
ln -sfn "$PWD/skills/beamline-steering" ~/.claude/skills/beamline-steering
```

Then start an agent from any folder with `scripts/claude-beamline`
(interactive) or `scripts/claude-beamline --headless` (`claude -p`,
stream-json). It puts this repo's pixi `nbs-cli` on PATH and allows only
`nbs-cli` commands. Steer it from the Agents Log tab or with `nbs-cli tell`.

To run through the NSLS-II API gateway, add `--sandbox bwclaude` (or set
`NBS_CLAUDE_SANDBOX=bwclaude`, e.g. when starting `nbs-agents-runner`). Claude
then runs inside `bwclaude` from n2snscripts, with this repo, the nbs-cli config
and the attachment store mounted. The gateway host and key come from
`AIFAPIM_HOST` and `AIFAPIM_API_KEY` in the environment, normally loaded from a
gpg-encrypted `.env` in the shell that starts `nbs-agents-runner` (launched
agents inherit them). Without them, `claude-beamline` sources
`config/gateway.env` under `$NBS_AGENTS_HOME` (override with `NBS_GATEWAY_ENV`),
which must be mode 600; keep it outside any repo.

`open` needs `nbs-agents-runner` running: its `ExternalAgent` (registered as
`external` and `claude`) holds the block open and records the session.

### Starting a Claude agent from nbs-gui

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
`claude_log_dir`. `scripts/claude-beamline` sees `NBS_AGENT_UID`, attaches to the
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
                                              # on PATH is taken from this repo's scripts/
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
signal = "sc"              # summary signal and normalization keys
norm = "i0"
# allowed_plans = ["c_xas", "n_xas", "o_xas", "nbs_count", "move_sample"]
```

### Agent log

Every agent writes one JSON record per session to Redis under
`{prefix}AGENT{agent_uid}` (readable with
`QtRedisJSONDict.from_settings(settings, topic="AGENT")`): `goal`, `status`,
`budget`, `opened`, and an append-only `log` of `{t, kind, text, ...refs}`.
`BaseAgent.log()` and `BaseAgent.set_status()` write it, the supervisor records
each agent's open, runs and close, and nbs-cli writes `add`, `remove`, `note`,
`question` and `finalize` entries.

By default that Redis is the beamline one. To keep the agent log in a Redis of
its own, give it a port in the nbs-cli config:

```toml
[agent_log]
port = 60740      # host = "localhost", db = 0, prefix = "" by default
serve = true      # nbs-agents-runner starts redis-server on 127.0.0.1:60740
# data_dir = "$NBS_AGENTS_HOME/state/agent-log"   # snapshot, agent-log.rdb
# redis_server = "redis-server"
```

With `serve = true` the runner starts `redis-server` unless one already
answers on the port, and stops it on exit. The snapshot in `data_dir` keeps
the log across restarts for a beamtime; stop the runner and delete
`agent-log.rdb` to start afresh. Without `serve`, point `host`/`port` at a
Redis run elsewhere, for example beside an agent container. nbs-cli and
nbs-agents-web read `[agent_log]` too. nbs-gui often runs under another account
without this config, so the runner publishes the address in the beamline Redis
(`{prefix}NBS_AGENT_LOG_LOCATION`) and the Qt tabs connect there. Beam status
still comes from the beamline Redis.

### Web UI

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
`fastapi` and `uvicorn` (the `web` extra).

Message text is Markdown with `$...$` and `$$...$$` math, written so it still
reads well as plain text. A figure is an image link to an attachment,
`![alt](att:ID)`, stored by `nbs_agents.attachments.AttachmentStore` (a
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

## Manual Validation

1. Start QueueServer and the agent runner.
2. Submit an agent block for the `repeat` agent.
3. Confirm the runner activates after `open_agent` completes.
4. Confirm repeated plans are inserted before `queue_stop` with shared `agent_uid` metadata.
5. Confirm `close_agent` runs only after the repeat agent finalizes the queue.

## Dependencies

- Python 3.9+
- Bluesky and Bluesky QueueServer
- Tiled
- NumPy
- IPython

## License

This project is in the public domain.
