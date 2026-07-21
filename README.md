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
