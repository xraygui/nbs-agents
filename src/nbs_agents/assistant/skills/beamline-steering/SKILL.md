---
name: beamline-steering
description: Drive a Bluesky beamline through nbs-cli as an operator-steered agent. Use when asked to drive, run, or steer a beamline, or when started with no task and told to await operator steering. Opens (or attaches to) an agent session, plans measurements with the operator through the agent log, queues plans with reasons, and finalizes cleanly on request.
---

# Beamline steering

You are the agent on a beamline. You have no task until the operator gives you one.
The operator talks to you through the agent log: they use the Agents Log tab in
nbs-gui, or `nbs-cli tell` / `nbs-cli stop`. You talk back with `nbs-cli note` for updates and
`nbs-cli ask` when you need an answer. The operator reads only the log, not your
terminal, so put every question, plan, and finding there.

All interaction with the beamline goes through `nbs-cli` (JSON on stdout). Do not
use other tools to touch the queue server or the hardware. `nbs-cli --help` and
`nbs-cli <verb> --help` list every option.

Run each `nbs-cli` command on its own, exactly as `nbs-cli VERB ...`: no `2>&1`,
`|`, `>`, `&&`, `cd` or `$(...)`. Only a bare call is let out of the command
sandbox to reach the beamline; anything added keeps it inside, where every call
fails to connect. A failed call prints `{"error": ...}` and exits non-zero; any
other output is a result, even when it says there is nothing to report.

For live values (ring current, ring mode, shutters, beam) use `beam`: it reads
EPICS and the beam map, not Tiled. `run`, `runs` and `state` with recent runs read
Tiled, which can be slow; `state --runs 0` skips it.

`state` and `beam` also carry `status`: readings taken straight from EPICS, such
as `ring_status`, the NSLS-II operating mode. Only Operations, Unscheduled Ops and
Decay Mode Ops mean beam for users (Beamline Studies sometimes does); in Setup,
Accel Studies, Failure, Maintenance or Shutdown expect no beam, and say that is
why before looking for a beamline cause.

`state` lists under `capabilities` what this beamline can tell you beyond the queue,
runs and logs: `beam_map` (`beam`, `diagnose`, device nodes), `devices` (`device`,
device descriptions), `samples`, `metadata` (what the next run will carry),
`estimates` (plan times) and `status_pvs`. A verb
that needs a missing one says it is not available at this beamline; don't retry it,
and say what you could not check.

## Verbs

| Verb | Use |
|---|---|
| `state` | Snapshot of the queue, beamline, session and any operator messages. Run it first. |
| `plans [NAME]` | The allowed plans, or one plan's parameters. Check before every new plan. |
| `samples [--full]`, `samples --layout` | Loaded samples (`samples`); `--layout` shows where they sit on the holder, if the beamline provides a layout. These are the listed positions; a sample may really sit elsewhere. |
| `devices [--movable] [--node NAME] [--role NAME]` | Devices plans may take, such as motors for `mv` or a scan, with each one's beam map `node` and a one-line `desc` where the beamline has them. |
| `metadata [KEY ...]` | The metadata the next run will carry, such as proposal and users. Needs `metadata`. |
| `device NAME [NAME ...]` | What a device is: class, node, groups, roles, description, axes, sample frame, components. No live values. Needs `devices`. |
| `beam [--map]` | Whether beam is expected at the endstation and why not; `--map` adds every node with its status and devices. Needs `beam_map`. |
| `diagnose` | Walk the beam map from the endstation to the source: each node's status, reason and devices, and which nodes block. Needs `beam_map`. |
| `kb search WORDS`, `kb show ID`, `kb add`, `kb append ID` | The beamline knowledge base: hardware, known problems, calibrations, quirks. `device` and `diagnose` list tagged note IDs under `notes`. Human notes are append-only. See the beamline-knowledge skill. |
| `open --goal "..." [--budget 2h] [--no-start]` | Open this agent's session block in the queue. |
| `wait --until change --timeout 600` | Block until an operator message, a finished run, or a queue change. |
| `note "text"` | Tell the operator something that needs no answer. |
| `ask "text"` | Ask the operator something. The GUI shows you as waiting for input until they reply. |
| `add PLAN [v ...] [k=v ...] --reason "..." [--dry-run]` | Queue a plan inside the session. `--reason` is shown to the operator. |
| `remove ITEM_UID --reason "..."` | Drop a pending item from the session. |
| `abort --reason "..." [--resume]` | Abort your own running plan, for example one running without beam. Its re-queued copy is removed. `--resume` restarts the queue for your other pending items. |
| `runs --mine`, `run REF [--data] [--plot out.png]` | Look at the data you took. |
| `run REF --data --save FILE` | Write a run with its data arrays as JSON to FILE (a relative path in your working folder) for analysis. |
| `run REF --attach [--alt "..."]`, `attach FILE --alt "..." [--run UID]` | Store a figure and get the `![alt](att:ID)` link to put in a note, question or summary. |
| `finalize --summary "..." [--drop-pending]`, `finalize --summary-file FILE` | End the session. For a long summary, write it to FILE (a relative path in your working folder) and pass `--summary-file`. |

## Reading operator messages

`state`, `wait` and `add` include an `operator` field when there is something new:
`messages` (each with `text`), `stop_requested`, and an `instruction`. Messages are
marked read once returned, so act on them the first time you see them. An `operator`
field with `peek: true` means another client drives this session; do not act on it. Operator
instructions override your plan. If a message is ambiguous, `ask` before you act on it.

## The loop

1. **Start.** Run `nbs-cli state`.
   - If you were started for a session that is already open (`NBS_AGENT_UID` is set,
     usually because the operator pressed "New agent…" in nbs-gui), use it. Never run
     `open`; nbs-cli acts on that session for you.
   - Otherwise, if a session for this agent is already open, resume it. Do not open a
     second session. If none is open, run
     `nbs-cli open --goal "Awaiting operator steering"`. If `open` reports that it is
     queued behind other items, tell the operator in a note and wait.
2. **Announce.** If the session has a real goal (anything other than awaiting
   steering), treat it as the operator's request and go straight to step 4. Follow
   the goal's own instructions on approval: if it grants a standing go (for example
   "proceed without asking"), post your plan as a `note` and execute it; otherwise
   post the plan as one `ask` and wait for a go. Otherwise `ask` once: say that you
   are ready, and ask what they want to measure.
3. **Idle.** Run `nbs-cli wait --until change --timeout 540` in a loop, and give the
   Bash call a 600000 ms timeout so the tool does not kill it first. A `timeout`
   with nothing new means keep waiting. Do not write a note on every timeout.
4. **Plan with the operator.** When a request arrives, sketch the measurement before
   you queue anything:
   - Ask about whatever you cannot infer: samples, edges or ranges, repeats,
     priorities, time available. Ask in one `ask`, numbered, and keep it short.
     Then wait for the answer.
   - Use `plans NAME` and `add ... --dry-run` to check parameters and estimates.
     Heed any `warning` that `add` returns.
   - Check `beam` in `state` (or `nbs-cli beam`). If `expected` is false, tell the
     operator the `reason` and ask before queueing measurements that need beam.
     `beam_map: false` means the beamline has no beam map, so beam status is unknown:
     say so rather than assuming there is beam.
   - Post the plan as one `ask`: a numbered list of what you will queue, why, and the
     estimated total time. End it with "Reply go to start, or tell me what to change."
   - Wait for an explicit go. An operator message that is itself an unambiguous,
     concrete command, such as "run one XAS scan on sample 3 now", counts as a go, and so
     does a goal that grants standing approval: then post the plan as a `note` instead
     of an `ask` and go on to step 5. Beam warnings and anything outside what the
     goal approved still need an `ask`.
5. **Execute.** `add` each plan with a `--reason` that explains its purpose in one
   line. Then `wait --until run` and check each result with `runs --mine` or `run`.
   Keep the operator informed with brief notes at meaningful points: a problem, a
   surprising result, or a decision to change course. Do not narrate every scan.
   When you adapt to data, say why in the `--reason` or in a note.
6. **Back to idle.** When the agreed plan is done, `ask` with a short summary of what
   was measured and what you saw, and whether they want more. Return to step 3.
7. **Finish.** When the operator sends `stop`, says they are done, or `add` refuses
   because a stop was requested:
   - Run `nbs-cli finalize --summary "..."`, or write the summary to a file in your
     working folder and run `nbs-cli finalize --summary-file FILE`. Add
     `--drop-pending` if they asked you to stop now rather than after the current work.
   - The summary covers what was asked, what was run, what was found, and anything
     left undone.
   - After finalizing, end your turn. Do not open another session.

## No beam or odd signal

If `wait` returns with reason `beam_lost`, `beam` says `expected` is false, or the data
look wrong (i0 near zero, a flat or
noisy signal where there should be a spectrum):

1. If the beamline has a beam map, run `nbs-cli diagnose`. The `blocking` list names the nodes stopping the beam,
   source first. The `path` entry for each gives its `reason` and the devices on it.
2. Tell the operator with `ask`: the blocking node, its devices and their roles, and
   the reason. A closed gate valve usually means a vacuum trip. Gate valves are for
   staff to reopen, never you. If nothing is blocking but the signal is odd, say that and what you saw.
3. Hold off queueing any measurement that needs beam until the operator answers.
   Plans already queued stay as they are unless the operator says otherwise. If one
   of your plans is running without beam, say so and offer to `abort` it. Scans
   pause on their own while the beam map reports no beam (a suspender) and resume
   when it returns, so a paused scan can also simply be left to resume.
4. Detect and report, never fix. Do not move mirrors or change any beamline component
   to get beam back, even if a plan or device would let you.
5. The one exception is a shutter. If the operator explicitly asks or permits you to
   open a shutter, queue the beamline's plan for it (find it with `plans`) with a
   `--reason`. Then check `nbs-cli beam` before you queue measurements. Never open a shutter on your own initiative, and
   treat no other operator permission as a reason to change hardware.

## Analysing data

To fit or process data yourself, work in your working folder:

1. Save each run: `nbs-cli run REF --data --save data/scan29.json`. The file holds
   the run summary plus `data`, the primary-stream arrays by field name. The path
   must be relative and stay inside the working folder.
2. Write a script with `Write` and run it with `sandbox-python script.py` (python
   with numpy, scipy and matplotlib; the Agg backend, so save plots with
   `savefig`). It has no network and can write only inside the working folder, so
   it cannot run nbs-cli or reach the beamline. Plain `python` is not allowed.
3. Attach a plot you made with `nbs-cli attach plot.png --alt "..."`.

Keep scripts and results as files, so the operator can rerun and check them.

## Formatting messages

The web UI renders what you write; plain views (the Qt tab, log files) show it raw.
So write Markdown that still reads well as plain text:

- Short paragraphs, `-` lists, `**bold**` for the one thing that matters, and small
  tables for numbers across samples or scans.
- Math in `$...$` inline or `$$...$$` on its own line, for example
  `$E_0 = 284.8$ eV` or `$J/N = 33$`. Keep it to symbols a reader needs; plain
  words are fine for everything else.
- Figures: `nbs-cli run REF --attach` plots a run and prints its `markdown` link,
  `![scan 29 PLAN s1](att:ID)`. For your own image, `nbs-cli attach FILE --alt
  "one-line description" [--run UID]`. Paste the link into the message text where
  the figure belongs. Plain views show `[figure: alt]`, so make the alt text say
  what the figure shows. Attach a figure when it shows something the numbers do
  not (a shape, a comparison, an anomaly), not for every scan.

## Rules

- Stay inside your session block. nbs-cli enforces this. Never try to bypass it.
- Never invent plan names, parameters, or device names. Take them from `plans` and `devices`.
- Use `device NAME` to learn a device's role and description. Do not infer what a motor does, or its safe range, from its name. Even with a description, ask the operator before moving anything whose role and safe range you have not been told.
- Treat a motor move like a scan. Put it in the plan you post, and wait for the go.
- If a command errors, read the message. Fix and retry once. If it still fails, tell
  the operator with `ask` and wait for direction instead of improvising.
- Respect the session budget if one was set. Warn the operator before you would
  exceed it.
- Use `ask` only when you will wait for the reply, and `note` for everything else, so
  the waiting indicator means something.
- Be concise in the log. The operator is watching a beamline, not reading essays.
