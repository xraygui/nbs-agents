# NBS Agent Framework

Agentic automation for NSLS-II beamline operation, built on Bluesky, QueueServer, and Tiled.

## Features

- ZMQ document catalog for live Bluesky runs
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

## Usage

Start the agent runner (listens for Bluesky documents and runs configured agents):

```bash
nbs-agents-runner
```

Or via pixi:

```bash
pixi run agent-runner
```

## Dependencies

- Python 3.9+
- Bluesky and Bluesky QueueServer
- Tiled
- NumPy
- IPython

## License

This project is in the public domain.
