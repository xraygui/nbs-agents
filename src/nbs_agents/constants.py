OPEN_AGENT_PLAN = "open_agent"
CLOSE_AGENT_PLAN = "close_agent"
QUEUE_STOP_INSTRUCTION = "queue_stop"

AGENT_UID_KEY = "agent_uid"
AGENT_NAME_KEY = "agent_name"
AGENT_PARAMETERS_KEY = "agent_parameters"
AGENT_SEQUENCE_KEY = "agent_sequence"
AGENT_CLOSE_KEY = "agent_close"
# The runner that drives an agent block, in its close_agent item's meta
AGENT_OWNER_KEY = "agent_owner"
# The owning runner's id, given to the Claude it launches; nbs-cli checks it
AGENT_OWNER_ENV = "NBS_AGENT_OWNER"
