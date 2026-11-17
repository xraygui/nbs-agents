"""
Run summaries for nbs-cli. The reducers moved to ``nbs_agents.reduce.metrics``,
which nbs-reduce shares; this module keeps the old import path working.
"""

from nbs_agents.reduce.metrics import (  # noqa: F401
    REDUCERS_ENTRY_POINT,
    SummaryContext,
    _round,
    dimension_keys,
    generic_summary,
    is_xas,
    noise_estimate,
    plugin_reducers,
    summarize,
    xas_summary,
)
