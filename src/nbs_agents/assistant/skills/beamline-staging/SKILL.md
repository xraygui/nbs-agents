---
name: beamline-staging
description: Help a person build a plan queue in nbs-gui's shared staging queue without running anything - read what is staged, check plans as the QueueServer would, and propose additions, removals and reordering that the person accepts in the web UI. Use when someone asks you to set up, draft, plan or fix a list of scans they will run themselves (for example "stage XAS on all the carbon samples", "put these in order", "check my staged plans").
---

# Building plan queues in the staging queue

nbs-gui keeps a staging queue: plans people build up and check before they move
them to the QueueServer queue themselves. It lives in the beamline Redis, so every
nbs-gui, the web UI and `nbs-cli` see the same list. Nothing in it runs.

You never change the staging queue directly, and you never queue anything. You
**propose** changes; your proposal appears in the conversation in the web UI,
in the thread you made it from, where a person accepts it, rejects it (saying
why if they like), or modifies it (changes plans' parameters or drops some of
the changes) and accepts that. Accepted changes land in the staging queue, and the
person moves plans to the run queue in nbs-gui when they are ready.

Run each `nbs-cli` command bare, as everywhere else.

## Verbs

| Verb | Use |
|---|---|
| `staging show` | The staged plans in order (each with its `item_uid` and estimate), the list's `version`, and pending proposals (yours and others'), each marked `stale` or `changed`. |
| `staging validate PLAN [params...]` | Check one plan as the QueueServer would (allowed plan, parameter names and types, allowed devices) plus the beamline's warnings and time estimate. Nothing is stored. |
| `staging propose --reason R --add PLAN params... [--add ...] [--after UID\|top\|end]` | Propose adding plans, kept in the order given, after an item (default: the end). |
| `staging propose --reason R --remove UID`, `--move UID AFTER` | Propose removing or reordering staged items; combine with `--add` in one proposal. |
| `staging propose --reason R --file notebook/staging-ops.json` | Many or mixed changes: a JSON list of ops, `{"op": "add", "item": {"name": PLAN, "args": [...], "kwargs": {...}}, "after_uid": UID or null or "top"}`, `{"op": "update", "item_uid": UID, "item": {...}}`, `{"op": "remove", "item_uid": UID}`, `{"op": "move", "item_uid": UID, "after_uid": ...}`. Write it with the Write tool first. |
| `staging withdraw ID` | Take back your own pending proposal (for example when you replace it). |
| `plans [NAME]`, `samples [--layout]`, `state`, `devices` | What plans exist and their parameters, the loaded samples, the XAS plan table, devices. |

Params are `key=value` pairs (JSON or Python literals) and positional values,
as in `nbs-cli plans NAME` shows them.

## How to work

1. Read first: `staging show` (what is already staged), `samples` and `plans`
   (or `plans NAME`) for exact plan and parameter names. Use only sample ids,
   edges and plan names that exist; never guess one.
2. Ask before proposing when the request leaves a real choice open (which
   samples, how many repeats, which edge, dwell or slit settings). One short
   question beats a wrong list of twenty plans.
3. Check plans you are unsure of with `staging validate`; `propose` checks every
   added plan again and refuses the whole proposal if one fails, storing nothing.
4. Make **one proposal per coherent request**, with a `--reason` that says what
   it does and why in a sentence or two (the person reads it on the card). Put
   related scans in a sensible order: group by sample or by edge as the person
   prefers, and label repeats and series with `group_name` where the plan takes it.
5. Reply with what you proposed: the number of plans, the total estimated time
   from the proposal's `checks`, any warnings, and that it is waiting just below
   for them to accept, modify or reject. Do not say it is staged until they
   accept it. Their decision comes back to the thread as a note (accepted,
   accepted with changes, or rejected and why); `staging show` has the result.
6. If you replace a proposal, `staging withdraw` the old one so the person does
   not see two.

Proposals expire after 7 days. A proposal is `stale` when an item it refers to
has gone from the staging queue (someone removed it); it can no longer apply, so
make a new one from the current list. `changed` only means the list moved on
since you proposed; it still applies to the list as it is.

The staging queue is shared by everyone at the beamline: never propose removing or
reordering plans someone else staged unless the person asked you to.
