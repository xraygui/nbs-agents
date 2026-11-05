# Beamline assistant workdir

This folder belongs to the read-only beamline assistant that nbs-agents-runner
calls after runs finish. Your instructions are in the system prompt.
`notebook/` is your memory between calls: each call's prompt includes it, so
keep it short and current.
