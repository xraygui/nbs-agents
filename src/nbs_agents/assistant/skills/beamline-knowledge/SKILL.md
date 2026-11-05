---
name: beamline-knowledge
description: Search, read and add to the beamline knowledge base through nbs-cli kb. Use when you need facts about the beamline's hardware, known problems, calibration routines or quirks that devices and diagnose don't give, before guessing about the hardware, when you learn something a later agent or operator should know, and when staff want to write knowledge down with your help (interview them, then save a draft for review). Human notes are append-only for agents.
---

# Beamline knowledge base

The knowledge base is a set of short Markdown notes about this beamline: hardware,
common problems, calibration routines and quirks. People write most of it. Agents
can add their own notes and append to any note. Every command prints JSON.

## Notes are background, not the current state

A note says how the beamline usually behaves and what a problem looks like. It
never says what is happening now. For a question about now ("why did scans
stop?", "is there beam?"), first check the live state with `nbs-cli state`,
`beam` and `diagnose`. Then use the notes to explain what you found. Never name a
cause from a note alone. Several problems look alike: a beam dump, a closed
shutter and a vacuum trip all take i0 to zero, and only `diagnose` and the run
summaries tell them apart.

## Who wrote what

Each note has `written_by`: `human` or `agent`. Human notes are the reference:
the beamline profile's notes, which staff write or approve in the web page
(approved ones name who approved them). Agent notes
and appended sections are observations by earlier agents. They may be wrong or
out of date, so weigh them as evidence and don't treat them as fact. Each
appended section starts `## Appended by <who> at <time>`.

You can edit only agent notes and drafts. You can never change a human note. If
one looks wrong or stale, use `kb append` to say what you saw and why. Staff
review every agent note and appended section in the web page's Knowledge tab,
and decide whether to keep, approve or remove it.

## Reading

| Command | Gives |
|---|---|
| `nbs-cli kb search WORDS [--device D] [--node N] [--topic T] [--by human\|agent] [-n 10]` | Notes matching the words, best first, each with a snippet. Notes with every word come first; a note that lacks some lists them under `missing`. Plurals, endings and prefixes match (`calibrate` finds `calibration`), and common words like "why" and "is" are ignored. |
| `nbs-cli kb index [same filters] [--only titles\|topics\|devices\|nodes\|tags ...] [--names]` | What the knowledge base covers: every note's title, and every topic, device, node and tag with the notes that use it. `--only` keeps just the parts you name, and `--names` shortens it (titles as `{id: title}`, the rest as plain lists). Run `kb index --names` once at the start to learn the words to search with. |
| `nbs-cli kb list [same filters]` | Every note that passes the filters, without text. |
| `nbs-cli kb show ID` | A note's front matter, body and appendix. |

`nbs-cli device NAME` and `nbs-cli diagnose` list the IDs of the notes tagged with
each device and node under `notes`. Read those before reasoning about the hardware.

Search before you guess. If you are unsure what a device does, why a signal looks
odd, or how a calibration works, search with a couple of plain words (`kb search
slit resolution`, `kb search --device mir3`). If the top hits all have `missing`
words, or nothing comes back, try another phrasing or two (the note may say "dump"
where you said "lost"). If still nothing fits, say so. Don't
fill the gap from general knowledge as if it were this beamline's fact.

## Writing

| Command | Does |
|---|---|
| `nbs-cli kb add --title T (--text "..." \| --file F) [--topic T] [--devices a,b] [--nodes n] [--tags t,u] [--draft]` | Writes a new agent note. Prints its ID (`agent/...`). With `--draft` it writes a draft (`draft/...`) instead, which is not searched or listed until staff approve it. |
| `nbs-cli kb list --drafts` | Drafts waiting for staff review. |
| `nbs-cli kb append ID (--text "..." \| --file F)` | Adds an attributed, dated section to any note. Only ever adds. |
| `nbs-cli kb edit ID [--title T] [--text "..." \| --file F] [--topic ...] [--devices ...]` | Changes an agent note. Human notes are refused. |

`--file` takes a relative path in your working folder. Write longer text there
first. Topics: `hardware`, `problem`, `calibration`, `quirk`, `procedure`. Tag
devices and nodes by their exact names from `devices` and `diagnose`, so the note
shows up there.

Before adding, search. If a note on the subject exists, append to it, so the
knowledge stays in one place. Add a new note only for a new subject.

Write something down when it would save a later agent or an operator real time.
Examples:
- a fault you found and how it showed itself (which node, which signal, what it looked like)
- a typical level, such as i0 at a given slit, or a reproducible quirk
- a correction to a note, as an append that gives the evidence

Each entry should be short and factual. Say what you saw, when, and on which runs
(scan IDs). Say how sure you are. Don't record routine results of one experiment,
which belong in the agent log, or anything the operator told you is private.
Never write PV names.

## Drafting notes for staff (interview)

Staff may ask you to draft a note ("draft a note on the beamline's purpose",
"help me write up the gate valve procedure", "interview me about the mono").
The result always goes into the knowledge base as a draft, which staff edit and
approve in the web page's Knowledge tab. It is not used until then.

1. Search first (`kb search`, `kb index --names`), so you add to what exists
   rather than repeat it. Say what you found.
2. Gather what you can check: `devices`, `device NAME`, `plans`, `diagnose`,
   `state`. Use exact device and node names.
3. Save the draft now with `kb add --draft --title ... --file notebook/kb-draft.md` (write the text there first; `notebook/` is the only folder you can write), one
   note per subject, short and factual, tagged with devices and nodes. Keep
   what staff told you apart from what you inferred: start the body with
   `Told by <name> on <date>.` when they gave you the content, and put anything
   you inferred (from device descriptions, plan names, general knowledge) under
   a heading `Unconfirmed` so the reviewer can check it. Never present general
   knowledge about similar beamlines as this beamline's fact.
4. Reply with the draft's ID, where to review it, and one or two short
   questions about what is missing or unconfirmed: what it is, when it matters,
   what it looks like when it goes wrong (which device, node or signal), what to
   do, typical values, and anything that must never be done.
5. When they answer (in a later message), fold the answers in with
   `kb edit draft/... --file notebook/kb-draft.md` and move confirmed points out of
   `Unconfirmed`. Find the draft with `kb list --drafts`.

Don't save staff knowledge as a live agent note, and don't append it to a human
note: it goes through review as a draft.

