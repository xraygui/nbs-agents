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

`sst-assistant` (Claude agents and the beamline assistant) and
`nbs-agents-gp` (Gaussian-process agents) are built this way.

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

Everything lives under `$NBS_AGENTS_HOME` (default `~/.nbs-agents`):

```
config/   nbs-cli.toml (settings)                                mode 700
state/    agent-log/ (the agent log's Redis snapshot), tiled/ (your Tiled
          login, mode 700), tiled-agent/ (the agents' Tiled key, mode 700)
data/     shared data such as attachments and knowledge-base notes
work/     agent working folders and logs
```

```bash
pixi run nbs-agents init --tiled-uri https://... --tiled-path BEAMLINE/raw
$EDITOR ~/.nbs-agents/config/nbs-cli.toml     # beamline addresses
pixi run nbs-agents check                     # files, permissions, services
pixi run agent-runner
```

`nbs-agents paths` prints every location, and `nbs-agents tiled-login` logs in
to Tiled ahead of time. Settings come from `$NBS_CLI_CONFIG` or
`config/nbs-cli.toml` (table `[nbs_cli]`), overridden by `NBS_<NAME>`
environment variables; see `nbs_agents.config.AgentsConfig`.

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
environment), keeps a snapshot in `state/agent-log`, and publishes the address
in the beamline Redis so nbs-gui finds it without this config. Without
`port`, the agent log shares the beamline Redis.

### Tiled login

Each install names its Tiled server and run catalog (`tiled_uri`,
`tiled_path`). When the server needs a login, the runner starts one: Tiled
prints a link and code and opens a browser, and the runner publishes the link
in the agent log for web pages to show. The login is cached in `state/tiled`.
From it the runner makes agents a read-only (`read:metadata`, `read:data`) API
key that expires after `tiled_key_lifetime_s`, renews it, and revokes it on
exit (`state/tiled-agent/api-key.json`). A fixed `TILED_API_KEY` skips this.

## Tests

```bash
pixi run -e test test
```

The Qt tests need nbs-gui and are skipped without it.

## License

Public domain.
