---
name: beamline-reduce
description: Reduce and plot runs from Tiled with nbs-reduce - which data keys a run has and what each one is, edge jump, e0, noise and baseline shape with surprise flags, normalized spectra, comparison with another run or a reference scan, and overlay plots. Use before plotting or judging a spectrum, and whenever you are unsure which detector or normalization to use.
---

# Reducing runs with nbs-reduce

`nbs-reduce` reads runs from Tiled only. It cannot touch the beamline, so it is
always safe to run. Every command prints JSON. `RUN` is a uid, a scan id, or
`-1` for the latest run; `--catalog NAME` reads another catalog, as in
`nbs-cli run`. Call it bare (no pipes, redirects or `&&`), like nbs-cli.

## It never guesses keys

Detector names differ between beamlines and scans, and the main detector can
be blocked (a screen or foil in the beam). So nbs-reduce settles the signal and
normalization from, in order: your `--signal` / `--norm`; the beamline's
`[reduce.defaults]` rules for the plan (which can depend on the recorded beam
state); the detector roles the run recorded (`plot_role` in `keys`); the
beamline's default keys; the run's hints. If none settles them, it
exits with status 3 and `"ambiguous": true` plus the candidates. Then choose
from `keys` and pass them explicitly. Every output says which keys it used and
why (`keys.reasons`); repeat that when you report a result.

## Commands

| Command | Gives |
|---|---|
| `nbs-reduce keys RUN` | Each data key: `role` (detector or motor), `desc`, `hinted`, `device_roles`, and `check` (range and `problems`: `zero`, `flat`, `no_structure`, `clipped`, `all_non_finite`). Then `state` (recorded beam-map readings) and `defaults`: the keys the rules settle, or the candidates. |
| `nbs-reduce summary RUN [--signal K] [--norm K]` | Per signal: edge jump, e0, noise, jump to noise, pre/post-edge slopes and pre-edge curvature in edge jumps, `inverted`; and `flags`. |
| `nbs-reduce norm RUN [...] [-o FILE.csv]` | The signal over the normalization and an edge-step normalized column. |
| `nbs-reduce compare RUN --to RUN\|ref:ID [...]` | Energy `shift`, `scale`, `residual_rms` (normalized), `jump_ratio`. `ref:ID` is an `nbs-cli ref` entry. |
| `nbs-reduce plot RUN [RUN...] -o FILE.png [--kind ratio\|normalized\|raw]` | An overlay with a caption naming the keys. Attach it with `nbs-cli attach FILE.png --alt "..."`. |

`--signal` repeats (or takes `a,b`). `--norm none` means no normalization.
`-o` takes a file name inside your working folder.

## Reading the flags

A flag marks a run as surprising; it does not say why. `inverted_edge`
(pre-edge above post-edge), `steep_pre_edge` and `curved_pre_edge` can come from
charging, a wrong normalization, a detector behind something, or the sample
moving. Name at least three causes and check whatever a read verb can check
(`nbs-cli run` for the baseline and slits, `keys` for a blocked detector,
`compare` against the sample's earlier runs or its reference) before you
settle on one.

A key with `flat` or `no_structure` in its `check` usually means it saw no
beam: look at `state` and the other detectors before trusting a spectrum
made from it.
