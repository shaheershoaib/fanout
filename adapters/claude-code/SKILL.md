---
name: fanout
description: Use whenever 2+ work-items are on the table in one session - a board with several Open tickets, a multi-finding fix wave, several asks in one or successive user messages - BEFORE starting any of them, and AGAIN when new items arrive mid-session (re-batch; do not queue new asks behind the current item). Not just for when a fan-out is already decided - this tool is HOW you decide: it computes the MSPs (one MSP = one branch = one PR), splits each into clusters (the unit one agent owns and walks sequentially), carries dependency edges at cluster granularity so one blocked leaf never gates its siblings, and tiers each cluster by blast radius x complexity. Consumed by ship (step 0) and proto-port (plan step). The project supplies the risk-marker taxonomy and any graph path; this tool bakes in no project paths.
---

# fanout

Turn "fan out only on disjoint files" from manual judgment into a computed plan -
and, with `--exec`, into the dispatch itself.

**Boundary: fanout schedules and (optionally) dispatches; `ship` owns the job.**
It plans in seconds, deterministically, and touches no git and ships nothing. With
`--exec` it will spawn one process per item along the dependency DAG, but it still
never commits, merges, gates or closes anything - the gated spine, verification and
close-outs stay with the CONSUMER (`ship` for work-sets, `proto-port` for ports).
Use `--exec` when you want the plan EXECUTED rather than followed by hand; leave it
off when the consumer is doing its own dispatching.

## Use it

**From plain asks (fanout does the recon):**

`python3 ~/.claude/skills/fanout/fanout.py --asks <asks.txt> --recon-exec 'claude -p {prompt}' [--graph <graph.json>] --emit-items items.json --risk-markers <a,b,c>`

One ask per line. Fanout runs one recon worker per ask, in parallel, and each
returns the leaves that ask decomposes into - with a `task`, the files it will
edit, `file_notes`, a `complexity` and an `acceptance` line. Recon resolves
files in this order: **the graphify graph if `--graph` names one** (it is told
the path and told to query it first), then grep for the named surface, then -
for net-new work with nothing to grep - the directory structure. `--emit-items`
writes the derived items so they can be read and corrected before anything is
built.

**From items you already have:**

`python3 ~/.claude/skills/fanout/fanout.py --items <items.json> [--graph <graph.json>] --risk-markers <a,b,c> [--serial-verify-markers <d,e>] [--trajectories <store.jsonl> | --no-trajectories]`

Each item may carry an optional `complexity` (`simple` | `complex`) and `type`
(`fix` | `feature` | `port` | `design` | `sweep` | `contract`). `complexity`
feeds the tier's second axis; omit it and tiering falls back to blast radius
alone, exactly as before. An item of `type: contract` inside a `contract_group`
PINS that interface: its consumers then run in parallel against a fixed shape
instead of serializing under one owner.

Standalone by design: pure stdlib, no network, no runtime assumptions - JSON in,
JSON out. Every project-specific input (risk markers, verify markers, graph,
history store) is passed IN; nothing about any project or stack is baked in.
`FANOUT_TRAJECTORY_STORE` overrides the default history path.

## Running the plan (`--exec`)

Without `--exec` this only PRINTS a plan, and a plan is advice an agent can
quietly ignore - usually by running a parallel plan serially, which looks
identical in a transcript and loses the whole speed win. `--exec` dispatches it:

`... --exec 'claude -p {prompt}' --concurrency 4 [--dry-run] [--run-log run.jsonl]`

**One branch per MSP is not a flag - it is what "one MSP = one PR" means.**
`--exec` cuts a branch and a git worktree for every MSP off `--base` (default:
the current branch), and dispatches that MSP's clusters inside it. The clusters
of one MSP share its tree, which is safe because they are file-disjoint by
construction. When an MSP's LAST cluster succeeds, its work is committed,
pushed, and handed to `--pr-exec`:

`--pr-exec 'gh pr create --draft --base {base} --head {branch} --title {title}'`

A failed cluster blocks its MSP, so a half-built MSP never commits and never
opens a PR. `--no-push` commits without pushing; `--no-branches` is an escape
hatch for a scratch repo or a non-git directory, and it is reported in the
result rather than silently producing no branches.

It dispatches **one agent per CLUSTER**, not per item - the cluster is the unit
one agent owns, and everything that must serialize is already inside it. A
cluster with no `cluster_after` edge starts immediately whatever MSP it belongs
to; one with an edge starts the moment ITS producer finishes, never when an
unrelated slow neighbour does. Priority goes to whatever unblocks the most work.

Each agent receives its cluster as a JSON brief - the leaves in dependency
order, and per leaf: `task`, `edit`, `read`, `acceptance`, and `file_notes` when
the item supplies them. It is JSON rather than prose so a non-LLM worker can
consume the same dispatch. A multi-leaf cluster is told to walk its leaves in
order and to emit one contract line per leaf; those land in `returns`, while
`returned` keeps its old single-line shape.

**`task` is what tells the agent what to DO, and it comes from the item.** An
item without one dispatches an agent whose entire brief is a name, so the plan
lists those under `underspecified` rather than letting the run look normal while
the work is guesswork. Because same-wave items are file-disjoint by
construction, one shared working tree is safe - worktrees only matter if you want
per-item commits.

- The command template is split into argv BEFORE substitution, so a prompt
  containing quotes, newlines or `;` can never be re-read as shell syntax. No shell.
- Each item gets a SELF-CONTAINED prompt built from its `context_packs` entry (a
  spawned process inherits no conversation) - override with `--prompt-template`.
- A non-zero exit marks the item `failed` and its dependents `blocked`;
  independent work continues. The process exits non-zero if anything failed or
  was blocked, so a green exit is never a silent skip.
- `--run-log` records what ACTUALLY ran against the plan's `plan_id`, which is
  what makes "was the parallel plan run in parallel?" checkable rather than assumed.
- **This is what makes fanout portable to an agent runtime with no subagent
  primitive** (a plain CLI agent): spawning becomes a subprocess concern, which
  every runtime has, instead of a capability the host must provide.

The runner guarantees DISPATCH - order, concurrency, dependencies. It says
nothing about whether the spawned agent did good work; review and the
verification gates still apply exactly as before.

### Keeping the ORCHESTRATOR's context small

An orchestrator's context grows from what its workers hand back, and every turn
re-sends that context - so unbounded worker reports are the dominant cost of a
long run, and the least visible one (each report looks reasonable on its own).
Two mechanisms bound it:

- **A return contract.** Each worker is asked for ONE line of JSON
  (`{item, status, files_changed, notes}`). The runner keeps that
  structured line in the record and leaves everything else in
  `<run-dir>/items/<name>.out`. Measured on a deliberately chatty worker: 343
  bytes in the record against 15 KB on disk. `--no-return-contract` opts out.
  **When you dispatch subagents yourself rather than via `--exec`, ask for the
  same shape** - the lever is the bounded return, not the runner.
- **Run state on disk.** `--run-dir` (default `.fanout/<plan_id>`) holds
  `plan.json`, `state.json` and per-item output, written after EVERY completion.
  So the run's state does not have to live in anyone's context, an interrupted
  run resumes with `--resume`, and a handoff to a fresh orchestrator is a
  directory rather than a summary. `--resume` refuses to run against a different
  `plan_id`, because prior results describe a different work-set.

Workers are given `FANOUT_ITEM`, `FANOUT_PLAN_ID`, `FANOUT_TIER` and
`FANOUT_FILES` in their environment, so a script worker can satisfy the contract
without parsing its prompt.

**Do not hand a run off mid-item.** Reproduce-then-verify evidence has to sit in
ONE transcript for a verification gate to see both halves; hand over at item
boundaries only.

- `--graph` (optional): a graphify `graph.json` (node-link). Per-repo, or the
  merged multi-repo graph (run graphify from the repos' common parent dir).
  Missing/absent graph = the plan loses ONLY the `import-adjacent` coupling
  signal; clustering, waves, tiers, and the marker/history signals all still
  compute - so a project with no graph runs the planner rather than skipping it
  (build the graph first when the stack supports it; it feeds better verdicts).
- `--items`: JSON `[{"name": "...", "files": ["path", ...], "contract_group": "tag", "after": ["producer", ...]}]` -
  one entry per work-item (leaf/ticket/ask) with the files it will edit. Two
  optional relationship fields, with OPPOSITE semantics:
  - `contract_group`: items sharing a tag are forced into one serialize-together
    cluster - declare it when two FILE-DISJOINT leaves are halves of ONE change
    (e.g. a backend split + the serializer that exposes it) so a single owner
    holds both. Deterministic "do not parallelize these; one MSP."
  - `after`: DIRECTIONAL producer->consumer ordering - "this item starts only
    after the named items INTEGRATE." It does NOT merge them into one owner:
    two consumers of one producer stay parallel with EACH OTHER (waves put the
    producer earlier, both consumers together later). Use it for cross-repo/API
    dependencies (BE endpoint -> the FE items that consume it) and design->impl
    chains; using contract_group there over-serializes independent consumers.
- `--risk-markers`: comma-separated path substrings that force the top model tier
  (the PROJECT supplies these - its own high-risk surfaces, e.g. financial, auth,
  migration, or contract paths).
- `--trajectories`: (optional) a trajectory-memory JSONL store, if the setup
  provides one. Defaults to the global store when present and is read
  automatically, so the plan is HISTORY-AWARE: a surface with a bad track record
  gets tiered up and surfaces known to break each other get a serialize hint. A missing/empty store - or `--no-trajectories` - yields the
  marker-only plan (identical output). History only ever ADDS caution; it never
  relaxes a tier or drops a signal.

Output JSON:
- `msps`: lists of item names - the **top unit**, one MSP = one branch = one PR.
  Items are fused into one MSP when they share an edited file OR a declared
  `contract_group`, so two open PRs never touch the same file. This does not
  DEFINE the boundary (that is a semantic call made upstream); it only FUSES two
  that turn out to collide.
- `clusters`: lists of item names - the **unit one agent owns** and walks
  sequentially. A cluster is a connected component of the serialization graph:
  a shared file or an UNPINNED `contract_group` fuses (undirected - must not run
  at once, either order fine), and a single-consumer `after` link fuses (nothing
  to parallelize). A BRANCHING `after` edge does not fuse, because splitting is
  the whole point of it; nor does any edge across MSPs, since a cluster
  converges into one branch and cannot span two. Independent items come out as
  clusters of one, which is the common case and the best one.
  **BREAKING: `clusters` used to mean what `msps` now means.**
- `cluster_briefs`: **what each subagent must be told.** One entry per cluster:
  its `leaves`, the `msp` it belongs to, its `tier`, the clusters it waits on,
  and a ready-to-send `prompt`. `--exec` sends exactly this, so an orchestrator
  spawning its OWN subagents sends the same thing rather than reconstructing it
  and telling them less. On by default; `--no-briefs` omits them when you only
  want the shape of the plan, since each carries a full prompt and they dominate
  the output on a wide batch.
- `cluster_after`: cluster index -> the clusters whose build must finish first.
  Carried by the CLUSTER that needs it, never by its MSP: if one of B's five
  leaves depends on A, that leaf's cluster waits and B's other four start at
  once. Every cluster NOT listed here starts immediately.
- `waves`: **deprecated** - a barrier schedule, kept only so existing consumers
  do not break. Dispatch off `clusters` + `cluster_after` instead: a barrier
  makes an item wait on unrelated slow neighbours, an edge makes it wait only on
  its actual predecessor. Historically: ONE GLOBAL execution schedule over ALL items (the batch's ordered
  plan). Items in one wave are mutually conflict-free (no shared file, no shared
  contract_group) AND have no `after` path between them -> run concurrently;
  each wave starts from the INTEGRATED result of the waves before it
  (merge/rebase between waves, or one owner stepping through). `after` consumers
  always land in a later wave than their producers; hubs go early (the shared
  spine, e.g. a models.py contract, integrates before its dependents fan out). A
  big cluster is NOT a serialize-everything verdict - a real 92-item set
  decomposes to 18 waves with wave 1 running 25 items concurrently; only a
  clique tail (N items all editing one file) is irreducibly one-at-a-time. A
  cluster's internal order = the global waves filtered to its members.
- `coupling_review`: for each file-disjoint, NOT-co-clustered PAIR that carries a soft
  coupling signal, an entry `{pair, signals, default}`. Signals: `import-adjacent`
  (their files are one hop apart in the graph), `shared-risk-marker:<M>` (the SAME
  risk-marker matches a file in both), `regression-history` (trajectory memory
  records a fix on one of these surfaces having BROKEN the other), and
  `same-migration-app:<A>` (both leaves add a migration to app `<A>`'s
  sequentially-numbered `migrations/` dir, so building them in parallel produces
  two migrations at the SAME number and one loses the rebase - a collision
  file-overlap clustering misses because the new files have different names).
  `default` is `serialize` when they share a risk-marker, have a regression
  history, OR would collide on a migration number (same high-risk subsystem /
  known to break each other / guaranteed rebase conflict -> likely must
  serialize), else `parallel`. Signal-free pairs are omitted, and so are pairs
  already ORDERED by an `after` path (their verdict is declared, not pending).
  This FEEDS the mandatory verdict below; it does NOT decide - the orchestrator does.
- `tier`: per item, `top` or `cheap` (map to your models, e.g. top=Opus, cheap=Sonnet).
  A path-marker match forces `top`; trajectory memory ALSO forces `top` for a surface
  with a bad track record (a recorded revert, a speculative ship, a caused-regression,
  or >=2 prior wrong-surface traps).
- `tier_notes`: present ONLY when history bumped something - per bumped item, the
  reason (e.g. `"history: 1 reverted, 2 prior traps"`). Absent otherwise.

Execution-cost outputs (additive - clustering and merge safety are unchanged):
- `ready_after`: per item, the items that must INTEGRATE before it starts (its
  declared `after` producers + any conflicting item scheduled earlier). This is
  the DAG the waves flatten. **Prefer it over `waves`**: waves are a barrier, so
  every item pays for the slowest member of the wave before it, while
  `ready_after` lets you PIPELINE - start each item the moment ITS predecessors
  land - so wall-clock tracks the critical path instead of the sum of per-wave
  maxima. Same safety: an item never starts before something it shares a file or
  a `contract_group` with. Map it to the Workflow tool's `pipeline()`; `waves`
  remains for consumers that want the simpler `parallel()` chain.
- `context_packs`: per item, `{edit: [...], read: [...]}` - the files to change
  plus their graph neighbours. **Hand the pack to the agent** instead of letting
  it hunt: locating the work (grep/glob/open) is where a subagent's tokens
  actually go. `read` is capped (`--max-context-files`) and reports
  `read_truncated` rather than silently dropping. Without a graph, `edit` only.
- `shared_context`: per wave, files that MORE THAN ONE item would read. Read them
  once at the orchestrator and pass a digest rather than paying N times.
- `coalesce`: per wave, groups of small `cheap` items ONE agent can take together
  (a subagent costs a fixed spawn + context load before any work). Never includes
  `top` tier, and members always share the same `ready_after`, so a group never
  waits on the union of its members' deps. Advisory - ignore it freely.
- `verdict_groups`: `coupling_review` pairs bucketed by identical signals, biggest
  first. Render ONE verdict per bucket rather than one per pair - the per-pair
  verdict is the only consumption cost that grows quadratically with batch size,
  and it is all top-tier. Every pair is still covered; nothing is dropped.
- `verify_mode`: per item, `serial` (needs the orchestrator's own session, e.g. an
  authenticated UI) or `offload` (a by-value check a session-less cheap agent can
  do). Driven by `--serial-verify-markers`, which the PROJECT supplies.

Scheduling is makespan-aware: `cost` (explicit, else edited-file count) feeds a
critical-path priority, so the longest remaining chain dispatches first - both
across waves and WITHIN one, since a wave wider than your concurrency cap queues.

## Consuming the plan
- **Render the coupling verdict FIRST (mandatory, every fan-out).** For EVERY
  `coupling_review` pair, make an explicit parallelize-vs-serialize call with a
  one-line rationale BEFORE dispatch. This is deterministic - always done, on any
  fan-out, regardless of leaf count - and the ORCHESTRATOR renders it inline (no
  extra judge agent). Skeptical default: a `default:"serialize"` pair STAYS
  serialized under one owner unless you can state why the two are genuinely
  independent. The signals are a HINT; the failure this prevents is parallelizing
  coupled-but-file-disjoint halves of one contract (what file-overlap clustering
  misses) - catch them here, or declare them up front with `contract_group`.
- **Execute `ready_after` as a pipeline (preferred), or `waves` as a barrier
  chain** - parallel background agents, or the Workflow tool's
  `pipeline()`/`parallel()` with worktree isolation (skill-directed use of the
  Workflow tool is authorized). `ready_after` maps to `pipeline()`: each item
  starts when ITS predecessors integrate, so nothing idles waiting on an
  unrelated slow neighbour. `waves` maps to a serial chain of `parallel()` steps
  - simpler, but every item pays for the slowest member of the preceding wave.
  Hand each agent its `context_packs` entry, and take the `coalesce` groups as
  single agents where you want fewer spawns. Do NOT run disjoint items one-at-a-time, and do NOT
  treat a big cluster as one serial lump - the waves ARE its internal
  parallelism. Sequential (e.g. subagent-driven) execution is for clique tails
  and coupled chains only. The speed win is real only if you actually fan out; a
  careful orchestrator defaults to serial and loses it otherwise. How clusters
  become MSPs/PRs: this planner computes `msps`, and that is the definition - a
  serialize-together cluster ships as ONE PR; `after` orders separate MSPs without
  merging them. Who ships them is the consumer's business.
- Risk markers match a WORD of the path, case-insensitively - `auth` fires on
  `types/AuthResponse.ts` and on `app/auth/route.ts`, but NOT inside
  `(unauthenticated)`. A marker containing a separator (`api/auth`, `.sql`) is
  read as a path fragment and keeps substring behaviour.
- Map `tier` to the model per leaf; the orchestrator + every review/gate/ship step
  stays top-tier. `tier` now reads TWO axes - blast radius (path markers, plus a
  history bump) and `complexity` when the item supplies it. `cheap` requires BOTH
  mechanical and low-blast-radius: path markers alone send the capable model to
  one-word copy changes and the cheap model to hard refactors.
- After a run, feed the returned `files_changed` back through `reconcile(items,
  clusters, actual)`. The whole plan rests on PREDICTED files; reconcile is what
  turns that from an assumption into something checked. A miss inside an item's
  own MSP is noise; a miss landing in another MSP's file set means the two were
  not disjoint and the plan parallelized a real collision.
- **VERIFY-MODE: fan out the verification the same way you fan out the build (the
  canonical rule, work-type-agnostic - fix, net-new feature, AND port all use it;
  it is the verify twin of the trust-boundary line above).** Read each leaf's
  verify-mode off the project's surface markers (same idea as the risk-markers): a
  leaf whose surface is authed deployed-UI verifies SERIALLY on the orchestrator
  (ONE authenticated session - parallel agents cannot each drive it; a
  standalone/automation browser is unauthed); a data / by-value leaf's verification
  OFFLOADS to a verifier-agent pool (session-less, cheap tier, one per leaf,
  returning `{leaf, observed_value, sha, pass, evidence}`). What NEVER fans out: the
  gate-read + judgment - the shipped "verified" claim (and any Stop verification
  hook) is keyed to the ORCHESTRATOR, whose transcript a subagent's read does not
  enter, so the orchestrator does the ONE cheap confirming read itself and cites the
  value. The project loop skill names the markers + the by-value channel.
  `verify_mode` is now a COMPUTED output - pass `--serial-verify-markers` and
  dispatch `offload` leaves to the checker pool, `serial` ones to the
  orchestrator.
- **Tier REVIEW depth by the same risk**, not just the model: `top` leaves get a
  full adversarial review, `cheap` leaves an orchestrator diff-glance. The
  orchestrator still reviews every diff and does all gating/shipping - parallelism
  never delegates the trust boundary.

## Caveats (it is a HINT, not a merge-safety oracle)
- **Clustering keys on edited-file overlap, NOT graph coupling - by design.**
  Graph-driven impact-set clustering was weighed and deliberately rejected:
  transitive coupling collapses a real codebase into one serial blob and kills
  parallelism. The graph stays advisory (the `coupling_review` signals feeding the
  mandatory verdict); the verdict + gate + orchestrator review catch logical breaks
  between file-disjoint items - and `contract_group` lets the scoper declare a known
  coupling outright (deterministic), without relying on the graph at all.
- **Producer->consumer dependency must be DECLARED - the graph cannot see it.**
  Two file-disjoint items where one needs the other's output first (a
  reasoning/design leaf that decides a contract, then impl leaves that build to
  it; a BE endpoint and its FE consumers) read as "parallel" unless you declare
  `after` on the consumers - recon/triage is where those edges are discovered,
  and declaring them is part of scaffolding the items JSON. With `after`
  declared, `waves` handles the rest (producer early, consumers together later -
  the Workflow serial `agent()` -> `parallel()` shape). Without it, conflict
  structure alone often approximates contract-first (hubs early) but is NOT
  semantic dependency - do not rely on the accident.
- File-level granularity: two agents editing different symbols in the SAME file still
  conflict - file is the safe parallel unit, not symbol.
- The graph can be stale (worktree commits may not rebuild it) - treat output as advisory.
- It is BLIND to the cross-repo API contract (per-repo graphs; a merged graph tags `repo`
  but does not model HTTP coupling) - cover that with the project's contract check.
- The orchestrator still reviews every diff before commit.
- **Trajectory matching is heuristic + strictly additive.** It joins history to items
  by edited-file overlap first, then surface/tag/name substring (for prose-only
  entries), so it can over-match - but the only effects are bumping a tier UP or
  adding a serialize HINT (the orchestrator still renders the verdict). It never
  relaxes anything, and an absent/empty store is a no-op. Populate `files` (and
  `regressed` when a fix breaks a twin) when appending trajectory entries, for
  precise joins.

## Related
- `ship` (step 0 + batching), `proto-port` (plan step) - the consumers.
- `graphify` - produces the `graph.json` this reads.
- A trajectory-memory store (whichever MCP/plugin the setup provides) - the optional history this reads for history-aware tiering + the `regression-history` coupling signal.
