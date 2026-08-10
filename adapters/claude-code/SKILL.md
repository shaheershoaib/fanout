---
name: fanout
description: Use whenever 2+ work-items are on the table in one session - a board with several Open tickets, a multi-finding fix wave, several asks in one or successive user messages - BEFORE starting any of them, and AGAIN when new items arrive mid-session (re-batch; do not queue new asks behind the current item). Not just for when a fan-out is already decided - this tool is HOW you decide: it computes parallel vs serialize clusters, the wave schedule (honoring declared `after` dependencies), and per-item risk tier from the items' edited files (+ an optional graphify graph for coupling hints). Consumed by ship (step 0) and proto-port (plan step). The project supplies the risk-marker taxonomy and any graph path; this tool bakes in no project paths.
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
`python3 ~/.claude/skills/fanout/fanout.py --items <items.json> [--graph <graph.json>] --risk-markers <a,b,c> [--serial-verify-markers <d,e>] [--trajectories <store.jsonl> | --no-trajectories]`

Standalone by design: pure stdlib, no network, no runtime assumptions - JSON in,
JSON out. Every project-specific input (risk markers, verify markers, graph,
history store) is passed IN; nothing about any project or stack is baked in.
`FANOUT_TRAJECTORY_STORE` overrides the default history path.

## Running the plan (`--exec`)

Without `--exec` this only PRINTS a plan, and a plan is advice an agent can
quietly ignore - usually by running a parallel plan serially, which looks
identical in a transcript and loses the whole speed win. `--exec` dispatches it:

`... --exec 'claude -p {prompt}' --concurrency 4 [--dry-run] [--run-log run.jsonl]`

It walks `ready_after`: every item whose predecessors SUCCEEDED is dispatched, up
to `--concurrency` at once, unlocking dependents as each finishes. Order follows
the plan, so long poles go first. Because same-wave items are file-disjoint by
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
- `clusters`: lists of item names. Items in one cluster share an edited file OR a
  declared `contract_group` -> MUST serialize. Distinct clusters are disjoint ->
  safe to run in parallel.
- `waves`: ONE GLOBAL execution schedule over ALL items (the batch's ordered
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
  become MSPs/PRs and who ships them: the `ship` skill's "MSPs"
  section is canonical - a serialize-together cluster ships as ONE PR; `after`
  orders separate MSPs without merging them.
- Map `tier` to the model per leaf; the orchestrator + every review/gate/ship step
  stays top-tier.
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
