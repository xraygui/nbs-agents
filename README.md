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

Or with pixi:

```bash
pixi install
```

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
| `nbs-cli state [--full]` | Manager state, running item, the session's block and budget, other queue counts, samples, plans, recent run summaries |
| `nbs-cli plans [name]` | Allowed plans and XAS edges, or one plan's parameters, docstring and time-estimate rule |
| `nbs-cli run [ref] [--data] [--plot f.png]` | One run (uid, uid prefix, scan id, or `-1`) with a reducer summary |
| `nbs-cli runs [-n 10] [--mine]` | Recent run summaries |
| `nbs-cli wait [--until run\|idle\|change] [--timeout 600]` | Block on the document stream; returns finished-run summaries |
| `nbs-cli open --goal "..." [--budget 2h]` | Queue `open_agent("claude")`, `queue_stop`, `close_agent` and start the queue if it is at the head |
| `nbs-cli add <plan> [v ...] [k=v ...] --reason "..." [--dry-run]` | Insert a plan before the session's `queue_stop`, tagged with `agent_uid`, `agent_sequence`, `agent_reason`; checks the allowlist and budget; starts the queue if it is idle at this session |
| `nbs-cli remove <item_uid> --reason "..."` | Remove a pending item, only from the session's own block |
| `nbs-cli note "..."` | Append to the agent log |
| `nbs-cli finalize --summary "..." [--drop-pending]` | Log a summary, remove the session's `queue_stop` and let the queue run to `close_agent` |
| `nbs-cli log [--all]` | Agent-log records |

`open` needs `nbs-agents-runner` running: its `ExternalAgent` (registered as
`external` and `claude`) holds the block open and records the session.

Settings come from `$NBS_CLI_CONFIG` or `~/.config/nbs-cli.toml` (table
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
each agent's open, runs and close, and nbs-cli writes `add`, `remove`, `note`
and `finalize` entries.

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
