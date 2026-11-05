"""
``nbs-agents-runner``: the persistent agent runner.

It watches the Bluesky document stream, opens and closes agents as their
``open_agent`` / ``close_agent`` blocks run (``AgentSupervisor``), keeps the
agent log (starting its own Redis with ``[agent_log] serve = true``), logs in
to Tiled when needed and keeps agents a read-only Tiled key.

Installed packages add to it through the ``nbs_agents.runner`` entry-point
group: each entry is a callable ``plugin(runtime)`` returning supervisor
observers (see ``AgentSupervisor``). An observer with a ``shutdown()``
method has it called when the runner stops.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from bluesky.callbacks.zmq import RemoteDispatcher
from bluesky_queueserver_api.zmq import REManagerAPI

from .agentlog import AgentLog
from .agentlog_store import AgentLogServer
from .catalog import ZMQCatalog
from .config import NO_AGENT_LOG, AgentsConfig
from .supervisor import AgentSupervisor
from .tiled_auth import AgentKeyKeeper

LOGGER = logging.getLogger(__name__)

RUNNER_ENTRY_POINT = "nbs_agents.runner"


@dataclass
class Runtime:
    """
    What runner plugins get to build on.

    Attributes
    ----------
    config : AgentsConfig
        Shared connection settings.
    manager : REManagerAPI
        QueueServer client.
    catalog : ZMQCatalog
        Document catalog.
    agent_log : AgentLog or None
        The agent log, if Redis was reachable.
    """

    config: AgentsConfig
    manager: Any
    catalog: ZMQCatalog
    agent_log: Optional[AgentLog]


def load_plugins(runtime: Runtime) -> list:
    """
    Observers from every installed ``nbs_agents.runner`` plugin; failures are logged.

    Parameters
    ----------
    runtime : Runtime
        Passed to each plugin.

    Returns
    -------
    list
        Supervisor observers.
    """
    from importlib.metadata import entry_points

    observers: list = []
    for entry in entry_points(group=RUNNER_ENTRY_POINT):
        try:
            made = entry.load()(runtime) or []
        except Exception:
            LOGGER.exception("Runner plugin %s (%s) failed to start", entry.name, entry.value)
            continue
        LOGGER.info("Runner plugin %s: %d observer(s)", entry.name, len(made))
        observers.extend(made)
    return observers


def _start_heartbeat(agent_log: AgentLog, period_s: float = 5.0) -> None:
    """Write a heartbeat to the agent log so nbs-cli open can tell the runner is up."""

    def beat():
        while True:
            try:
                agent_log.heartbeat()
            except Exception:
                LOGGER.exception("Could not write the runner heartbeat")
            time.sleep(period_s)

    threading.Thread(target=beat, name="runner-heartbeat", daemon=True).start()


def main(argv=None) -> int:
    """
    Start the persistent agent runner.

    Connection settings come from the same sources as nbs-cli
    (``NBS_QS_ADDR``, ``NBS_ZMQ_ADDR`` or a TOML file) for the beamline named
    by ``--beamline`` (see ``nbs_agents.paths``). The agent log needs its own
    Redis (``[agent_log] port``); with ``serve = true`` the runner starts it.
    Plugins (``nbs_agents.runner`` entry points) add their observers.
    """
    import argparse
    import sys

    from .paths import BeamlineError, add_beamline_argument, use_beamline

    parser = argparse.ArgumentParser(prog="nbs-agents-runner", description="Run beamline agents.")
    add_beamline_argument(parser)
    args = parser.parse_args(argv)
    try:
        # Also sets $NBS_BEAMLINE, so the agents it launches use the same beamline
        beamline = use_beamline(args.beamline)
    except BeamlineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = AgentsConfig.load()
    if not config.agent_log.dedicated:
        print(f"error: {NO_AGENT_LOG}", file=sys.stderr)
        return 2
    LOGGER.info("Beamline %s", beamline)
    manager = REManagerAPI(zmq_control_addr=config.qs_addr)
    dispatcher = RemoteDispatcher(config.zmq_addr)
    catalog = ZMQCatalog(dispatcher)
    server = None
    if config.agent_log.serve:
        server = AgentLogServer(config.agent_log)
        try:
            server.start()
        except Exception:
            LOGGER.exception("Could not start the agent log Redis")
    agent_log = None
    settings = config.agent_log_settings
    try:
        agent_log = AgentLog.from_settings(settings)
        agent_log.redis.ping()
        LOGGER.info("Writing agent log to Redis %s:%s key %s*", settings["host"], settings["port"], agent_log.key_prefix)
    except Exception:
        LOGGER.exception("Agent log disabled: could not reach Redis at %s:%s", settings["host"], settings["port"])
        agent_log = None
    # Log in to Tiled (a link is printed and shown on the web page if needed) and
    # keep a read-only key for agents
    keeper = AgentKeyKeeper(config, on_link=agent_log.set_tiled_login if agent_log is not None else None)
    keeper.start()
    observers = load_plugins(Runtime(config, manager, catalog, agent_log))
    AgentSupervisor(manager, catalog, agent_log=agent_log, observers=observers)
    if agent_log is not None:
        _start_heartbeat(agent_log)
    LOGGER.info("Agent runner listening for Bluesky documents at %s", config.zmq_addr)

    try:
        dispatcher.start()
    except KeyboardInterrupt:
        LOGGER.info("Stopping agent runner")
    finally:
        for observer in observers:
            if hasattr(observer, "shutdown"):
                try:
                    observer.shutdown()
                except Exception:
                    LOGGER.exception("Could not shut down %s", type(observer).__name__)
        keeper.stop()
        if server is not None:
            server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
