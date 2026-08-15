# fanout

Decide how a batch of coding tasks should run — what goes in parallel, what must
serialize, in what order, at what cost — and then actually run it.

JSON in, JSON out. Pure Python standard library: no dependencies, no network, no
assumption about which agent or runtime you use.

```bash
python3 fanout.py --items items.json --risk-markers "auth/,billing/"
```

## The problem

Hand an agent five tasks and it will usually do them one at a time, because
deciding what is safe to run concurrently is genuinely hard: two tasks that edit
the same file must not run together, a task that consumes another's output must
wait for it, and two tasks that look independent can still collide (both adding a
sequentially-numbered migration to the same app, for instance).

Guessing is slow when you are too cautious and corrupting when you are not.
fanout computes the answer instead, deterministically, in milliseconds.

## What it computes

Give it the items and the files each one will edit:

```json
[
  {"name": "api",    "files": ["app/api/orders.py"], "cost": 3},
  {"name": "ui",     "files": ["src/pages/Orders.tsx"], "after": ["api"]},
  {"name": "typo-a", "files": ["src/copy/a.ts"]},
  {"name": "typo-b", "files": ["src/copy/b.ts"]}
]
```

It returns:

| field | what it answers |
|---|---|
| `clusters` | which items MUST serialize (shared file or declared contract group) |
| `waves` | a barrier schedule: everything in a wave runs concurrently |
| `ready_after` | the real dependency DAG — pipeline off this, not the waves |
| `tier` | per item, `top` or `cheap`, so you can route models by risk |
| `context_packs` | the files each task should read, so an agent is not left hunting |
| `coupling_review` | file-disjoint pairs that still carry a coupling signal |
| `verdict_groups` | those pairs bucketed, so one ruling covers many |
| `coalesce` | small tasks cheaper merged into one worker than spawned separately |
| `shared_context` | files several tasks would each read — read once, share the digest |
| `verify_mode` | whether a task's check needs a live session or can be offloaded |
| `plan_id` | a hash, so a run log can be tied to the plan it claims to have run |

**Merge safety keys on the edited FILE.** Not on symbols, not on transitive
imports. Graph-driven impact analysis was tried and rejected: transitive coupling
collapses a real codebase into one serial blob and destroys the parallelism you
came for. The graph stays advisory.

## Running the plan

A plan is advice a caller can quietly ignore — usually by running a parallel plan
serially, which looks identical afterwards and loses the entire win. `--exec`
removes the choice:

```bash
python3 fanout.py --items items.json \
  --exec 'your-agent-cli --prompt {prompt}' --concurrency 4
```

It walks the dependency DAG: every item whose predecessors succeeded is
dispatched, up to the concurrency cap, unlocking dependents as each finishes.
Longest chains go first, so the critical path is not left until last.

- Same-wave items are file-disjoint by construction, so **one shared working tree
  is safe** — no worktree ceremony unless you want per-item commits.
- The command template is split into argv **before** substitution, so a prompt
  containing quotes, newlines or `;` can never be reinterpreted as shell syntax.
- A failure marks its dependents `blocked` and exits non-zero. A green exit is
  never a silent skip.
- `--dry-run` shows the dispatch order and the exact argv, spawning nothing.

Because dispatch is a subprocess concern, this works with an agent runtime that
has **no subagent primitive of its own** — the thing that otherwise forces a
capable planner back into single-file execution.

## Optional inputs

- `--graph` — a [graphify](https://github.com/shaheershoaib) `graph.json`. Adds
  import-adjacency coupling signals and fills in `context_packs` neighbours.
  Without it everything else still computes.
- `--risk-markers` — path substrings that force the `top` tier. **You** supply
  these; the tool bakes in no paths and knows nothing about your domain.
- `--serial-verify-markers` — surfaces whose verification needs a real session.
- `--trajectories` — a JSONL history store, if you keep one. Strictly additive:
  history only ever raises caution, never relaxes it. Override the default path
  with `FANOUT_TRAJECTORY_STORE`.

## Testing

```bash
python3 -m unittest discover -s tests
```

42 tests, covering the scheduling algebra, the safety invariants, and the runner
(including that a prompt full of shell metacharacters stays one argument).

## Adapters

`adapters/claude-code/SKILL.md` wires it into Claude Code as a skill. The script
itself is the product; adapters are thin.

## License

[Apache 2.0](LICENSE) — free to use, modify, and share, including commercially.
Keep the [`NOTICE`](NOTICE) file with any redistribution (§4(d)), and don't market
a fork under the `fanout` name (§6). The patent grant in §3 means adopting this
doesn't expose you to a patent claim over it.

Required Notice: Copyright Shaheer Shoaib (https://github.com/shaheershoaib)
