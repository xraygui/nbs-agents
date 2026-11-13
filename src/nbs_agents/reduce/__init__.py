"""
nbs-reduce: reduction and plotting of runs read from Tiled, with explicit keys.

A library and a command (``nbs-reduce``, the ``[reduce]`` extra) for people,
agents and the assistant's roles. It reads only Tiled, never the QueueServer,
the document stream or EPICS, and never shows a PV name. It never guesses
which keys to reduce: see ``keys`` for how they are settled.

- ``metrics``: the per-run reducers nbs-cli's summaries and the assistant use.
- ``runs``: a run as read from Tiled.
- ``keys``: what each data key is, and settling the signal and normalization.
- ``analysis``: summaries with surprise flags, normalized spectra, comparisons.
- ``plot``: overlay plots with a caption naming the keys.
"""
