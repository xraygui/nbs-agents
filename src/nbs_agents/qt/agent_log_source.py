"""Find the agent log for the Qt tabs: the beamline Redis, or the runner's own."""

import logging

from ..agentlog import AGENT_LOG_TOPIC
from ..agentlog_store import read_location

LOGGER = logging.getLogger(__name__)


def agent_log_dict(user_status, parent=None):
    """
    ``QtRedisJSONDict`` of agent-log records.

    nbs-gui usually runs without an nbs-cli config, so this asks the beamline
    Redis whether nbs-agents-runner published a separate agent-log Redis
    (``[agent_log]`` in the runner's nbs-cli config) and connects there, or
    else uses the beamline Redis as before.

    Parameters
    ----------
    user_status : nbs_gui.models.UserStatus
        The GUI's beamline status model.
    parent : QObject, optional
        Parent of a newly made dict.

    Returns
    -------
    QtRedisJSONDict or None
        The records, or None without a beamline Redis.

    Raises
    ------
    Exception
        If the published agent-log Redis cannot be reached.
    """
    records = user_status.get_redis_dict(AGENT_LOG_TOPIC)
    if records is None:
        return None
    try:
        location = read_location(records._redis, records._prefix)
    except Exception:
        LOGGER.warning("Could not look up the agent log location; using the beamline Redis", exc_info=True)
        location = None
    if location is None:
        return records
    from nbs_gui.models.QtRedisJSONDict import QtRedisJSONDict

    LOGGER.info("Agent log in Redis %s:%s", location["host"], location["port"])
    return QtRedisJSONDict.from_settings(location, topic=AGENT_LOG_TOPIC, parent=parent)
