"""Plan a parallel subagent fan-out from a graphify graph. Project-agnostic.

cluster_items: items sharing an edited file OR a declared contract_group MUST
serialize (one owner); distinct clusters are file- and contract-disjoint -> safe
to run in parallel (merge-safety unit is the EDITED file, not symbols, not
transitive import-coupling).
coupling_review: for each file-disjoint, NOT-co-clustered PAIR, the soft coupling
signals (import-adjacency, shared risk-marker, a recorded regression between the
two surfaces from trajectory memory, and same-migration-app - both add a migration
to one app's sequentially-numbered migrations/ dir, so parallel builds collide on
the number) that warrant an explicit parallelize-vs-serialize verdict from the
ORCHESTRATOR before dispatch.
tier_for: RISK tier ('top'/'cheap') from caller-supplied path markers; a surface
with a bad track record in trajectory memory (a revert, a speculative ship, a
caused-regression, or repeated wrong-surface traps) is ALSO forced to 'top',
even with no path-marker match.

Trajectory memory is OPTIONAL and strictly ADDITIVE: with no store, an empty
store, or --no-trajectories, the output is identical to the marker-only plan.
History only ever ADDS caution - it bumps a tier UP and adds serialize hints; it
never relaxes a tier or removes a coupling signal.

The graph is a HINT, not a merge-safety oracle: file-level granularity; may be
stale; blind to the cross-repo API contract. The orchestrator still reviews
diffs AND renders the coupling_review verdicts.

Execution-cost outputs (all ADDITIVE - clustering and merge safety are unchanged):
ready_after: per item, the predecessors that must INTEGRATE before it starts, so
a consumer can PIPELINE (start each item when ITS deps land) instead of marching
whole-wave barriers - wall-clock tracks the critical path, not the sum of
per-wave slowest items. waves stays for consumers that want the simple shape.
context_packs: per item, the files to read (its own + graph neighbours), so an
agent does not spend its tokens LOCATING the work. shared_context: files >1 item
in a wave would each read separately. coalesce: small cheap-tier items one agent
can take together (a subagent costs a fixed spawn + context load before any
work). verdict_groups: coupling_review pairs bucketed by identical signals, so
the mandatory verdict is rendered once per bucket, not once per pair.
verify_mode: whether a leaf's check needs the orchestrator's own session or can
be offloaded. Scheduling is makespan-aware: longest remaining chain first.
"""
import argparse
import hashlib
import json
import os
import shlex
import subprocess
import time
from collections import defaultdict

# Optional history store. Overridable by env so the tool carries no assumption
# about which agent runtime (or which home layout) it is running under.
DEFAULT_TRAJECTORY_STORE = os.environ.get(
    "FANOUT_TRAJECTORY_STORE",
    os.path.expanduser("~/.claude/mcp-servers/trajectory-kb/data/trajectories.jsonl"),
)


def load_graph(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return {n["id"]: n for n in d["nodes"]}, d["links"]


def build_file_coupling(nodes_by_id, links):
    adj = defaultdict(set)
    for ln in links:
        s = nodes_by_id.get(ln.get("source"))
        t = nodes_by_id.get(ln.get("target"))
        if not s or not t:
            continue
        sf, tf = s.get("source_file"), t.get("source_file")
        if sf and tf and sf != tf:
            adj[sf].add(tf)
            adj[tf].add(sf)
    return dict(adj)


def _paths_match(a, b):
    """Same file across repo-relative vs prefixed roots: equal, or one is a
    /-boundary suffix of the other (so item 'backend/app/services/x.py' matches
    graph node 'app/services/x.py' but 'yapp/s.py' does NOT match 'app/s.py').
    Without this, item paths that carry a repo prefix never resolve to graph
    nodes and the coupling advisory silently never fires."""
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _item_graph_files(file_paths, adj_keys):
    """Graph node source_files that path-match an item's edited files."""
    return {k for f in file_paths for k in adj_keys if _paths_match(f, k)}


def cluster_items(items):
    """Union items that MUST serialize: they share an edited file OR a declared
    contract_group (two halves of one contract -> one owner, even when their
    edited files are disjoint). Distinct clusters are file- and contract-disjoint
    -> safe to run in parallel."""
    parent = {it["name"]: it["name"] for it in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    owner, group_owner = {}, {}
    for it in items:
        for f in it["files"]:
            if f in owner:
                union(it["name"], owner[f])
            else:
                owner[f] = it["name"]
        g = it.get("contract_group")
        if g:
            if g in group_owner:
                union(it["name"], group_owner[g])
            else:
                group_owner[g] = it["name"]
    groups = defaultdict(list)
    for it in items:
        groups[find(it["name"])].append(it["name"])
    return list(groups.values())


def _conflict_adjacency(items):
    """name -> set of names it MUST serialize with: shared edited file OR shared
    contract_group (the same keys cluster_items unions on, kept pairwise here so
    waves can see WHICH items inside a cluster actually conflict)."""
    names = [it["name"] for it in items]
    files = {it["name"]: set(it["files"]) for it in items}
    group = {it["name"]: it.get("contract_group") for it in items}
    adj = {n: set() for n in names}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if files[a] & files[b] or (group[a] and group[a] == group[b]):
                adj[a].add(b)
                adj[b].add(a)
    return adj


def _after_edges(items):
    """Validated producer->consumer edges from the optional per-item `after`
    list ("this item starts only after the named items INTEGRATE"). Directional
    on purpose: unlike contract_group it does NOT merge items into one owner -
    two consumers of one producer stay parallel with each other."""
    names = {it["name"] for it in items}
    after = {it["name"]: list(it.get("after") or []) for it in items}
    unknown = {d for deps in after.values() for d in deps if d not in names}
    if unknown:
        raise ValueError("unknown `after` reference(s): %s" % sorted(unknown))
    return after


def item_cost(item):
    """Relative size of an item, for makespan-aware scheduling. An explicit
    `cost` wins; otherwise the edited-file count - coarse, but it separates a
    one-line leaf from a forty-file one, which input order never does."""
    c = item.get("cost")
    if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0:
        return float(c)
    return float(len(item.get("files") or [])) or 1.0


def downstream_cost(items, cost):
    """Longest remaining-work path from each item through its `after` consumers
    (its critical path). Scheduling the largest first is what shortens the
    MAKESPAN: wave COUNT is not wall-clock - a wave costs its slowest member, so
    an item with a long chain behind it must not be the one that starts late.
    Iterative (a deep chain must not blow the stack); a cycle leaves the base
    cost in place, and global_waves raises on it separately."""
    after = _after_edges(items)
    names = list(after)
    consumers, outdeg = defaultdict(list), {n: 0 for n in names}
    for n, deps in after.items():
        for d in deps:
            consumers[d].append(n)
            outdeg[d] += 1
    dc = {n: cost.get(n, 1.0) for n in names}
    queue = [n for n in names if outdeg[n] == 0]  # sinks first
    qi = 0
    while qi < len(queue):
        n = queue[qi]
        qi += 1
        for p in after[n]:  # p produces for n -> p carries n's chain behind it
            dc[p] = max(dc[p], cost.get(p, 1.0) + dc[n])
            outdeg[p] -= 1
            if outdeg[p] == 0:
                queue.append(p)
    return dc


def global_waves(items, adj, order_index, priority=None):
    """ONE global execution schedule over all items. Items in one wave are
    mutually conflict-free (no shared file, no shared contract_group) and have
    no `after` path between them -> run concurrently; each wave starts from the
    INTEGRATED result of the waves before it (merge/rebase between waves, or
    one owner stepping through). `after` gives a topological floor (consumers
    never before producers); above the floor, greedy coloring hub-first: the
    most-conflicted item (the shared spine, e.g. the models.py contract) lands
    early so producers integrate before consumers fan out. Deterministic:
    dependency floor asc, `priority` desc (the critical path from
    downstream_cost, when supplied - longest remaining chain starts earliest),
    conflict degree desc, then input order. Raises ValueError on an unknown
    `after` name or a dependency cycle."""
    after = _after_edges(items)
    names = [it["name"] for it in items]
    consumers = defaultdict(list)
    indeg = {n: len(after[n]) for n in names}
    for n, deps in after.items():
        for d in deps:
            consumers[d].append(n)
    floor = {n: 0 for n in names}
    queue = [n for n in names if indeg[n] == 0]
    qi = 0
    while qi < len(queue):
        n = queue[qi]
        qi += 1
        for c in consumers[n]:
            floor[c] = max(floor[c], floor[n] + 1)
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    if qi != len(names):
        cyc = sorted(n for n in names if indeg[n] > 0)
        raise ValueError("`after` cycle involving: %s" % cyc)
    members = set(names)
    priority = priority or {}
    verts = sorted(names, key=lambda n: (floor[n],
                                         -priority.get(n, 0.0),
                                         -len(adj.get(n, set()) & members),
                                         order_index[n]))
    wave_of = {}
    for v in verts:
        used = {wave_of[u] for u in adj.get(v, set()) if u in wave_of}
        w = max([floor[v]] + [wave_of[p] + 1 for p in after[v] if p in wave_of])
        while w in used:
            w += 1
        wave_of[v] = w
    n_waves = max(wave_of.values()) + 1 if wave_of else 0
    waves = [[] for _ in range(n_waves)]
    for v in names:
        waves[wave_of[v]].append(v)
    # WITHIN a wave, dispatch order still matters: a consumer's concurrency is
    # capped, so a wave wider than the cap queues - hand out the long poles
    # first or they straggle at the end and set the wave's cost.
    for w in waves:
        w.sort(key=lambda n: (-priority.get(n, 0.0),
                              -len(adj.get(n, set()) & members),
                              order_index[n]))
    return [w for w in waves if w]


def _after_connected(items):
    """Frozenset pairs with an `after` PATH between them (either direction) -
    their order is DECLARED, so they need no coupling_review verdict."""
    after = _after_edges(items)
    names = list(after)
    reach = {}
    for n in names:
        seen, stack = set(), list(after[n])
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            stack.extend(after[p])
        reach[n] = seen
    return {frozenset((a, b)) for a in names for b in reach[a]}


# ── trajectory memory (optional, additive) ──────────────────────────────────

def load_trajectories(path):
    """Read the append-only JSONL store; exclude superseded entries. A missing or
    unreadable file -> [] (the plan degrades to the marker-only path, no error)."""
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return []
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # skip a corrupt line, don't fail the whole read
    superseded = {e.get("supersedes") for e in entries if e.get("supersedes")}
    return [e for e in entries if e.get("id") not in superseded]


def _basename(p):
    return p.rsplit("/", 1)[-1]


def _ref_matches_item(ref, item):
    """Does a free-text reference (a trajectory surface / file / regressed string)
    point at this item? Path-suffix match on an edited file first (precise), then
    a basename / filestem / item-name substring (catches surface-only entries)."""
    if not ref:
        return False
    r = str(ref).lower()
    for f in item["files"]:
        if _paths_match(str(ref), f):
            return True
        bn = _basename(f).lower()
        if bn and bn in r:
            return True
        stem = bn.rsplit(".", 1)[0]
        if len(stem) >= 4 and stem in r:
            return True
    nm = item["name"].lower()
    return len(nm) >= 4 and nm in r


def _entry_matches_item(entry, item):
    """Is this trajectory entry about this work-item? Edited-file overlap
    (precise) OR the entry's surface/tags reference one of the item's files or
    its name (fallback for entries whose surface is prose)."""
    for ef in (entry.get("files") or []):
        for itf in item["files"]:
            if _paths_match(str(ef), itf):
                return True
    hay = (entry.get("surface") or "") + " " + " ".join(entry.get("tags") or [])
    return _ref_matches_item(hay, item)


def _entry_regresses_item(entry, target_item):
    """Did fixing this entry's surface break the target item's surface? (the
    structured `regressed` list - a recorded 'fixing A broke B')."""
    return any(_ref_matches_item(r, target_item) for r in (entry.get("regressed") or []))


def history_tier_bump(item, trajectories):
    """A surface with a bad track record is high-risk regardless of path markers.
    Returns (bump?, reason). Bumps on a recorded revert, a speculative ship, a
    caused-regression, or >=2 prior wrong-surface traps on the matched surface."""
    matched = [e for e in trajectories if _entry_matches_item(e, item)]
    if not matched:
        return False, ""
    reverts = sum(1 for e in matched if e.get("outcome") == "reverted")
    spec = sum(1 for e in matched if e.get("outcome") == "speculative")
    regressed = sum(1 for e in matched if (e.get("regressed") or []))
    traps = sum(1 for e in matched if (e.get("what_failed") or []))
    parts = []
    if reverts:
        parts.append(f"{reverts} reverted")
    if spec:
        parts.append(f"{spec} speculative")
    if regressed:
        parts.append(f"{regressed} caused-regression")
    if traps >= 2:
        parts.append(f"{traps} prior traps")
    return bool(parts), ", ".join(parts)


# ── coupling + tier ─────────────────────────────────────────────────────────

def _migration_apps(file_paths):
    """App dirs whose migrations/ a set of files touches. Two file-DISJOINT items
    that each ADD a migration to the SAME app collide at the same migration number
    when built in parallel (two 0089_* leaves -> the renumber-on-rebase tax) - a
    serialize signal that file-overlap clustering MISSES, because the new migration
    files have different names and so never register as a shared file. Frameworks
    with a per-app sequentially-numbered migration dir (Django, Rails db/migrate,
    etc.) all share this hazard; the key is the path segment before `migrations/`."""
    apps = set()
    for f in file_paths:
        norm = f.replace("\\", "/")
        if "migrations/" in norm and norm.endswith(".py"):
            apps.add(norm.split("migrations/")[0].rstrip("/"))  # '' == repo-root migrations dir
    return apps


def coupling_review(items, adj, risk_markers, trajectories=None):
    """For each file-disjoint, NOT-co-clustered pair, surface soft coupling
    signals that warrant an explicit parallelize-vs-serialize verdict before
    dispatch (the ORCHESTRATOR renders the verdict; this flags + defaults only):
      - import-adjacent:        their files are one hop apart in the graph
      - shared-risk-marker:<M>: the SAME risk-marker matches a file in both
      - regression-history:     trajectory memory records a fix on one surface
                                having broken the other (default 'serialize')
      - same-migration-app:<A>: both add a migration to the same app's migrations/
                                dir -> parallel builds collide on the migration
                                number (default 'serialize')
    default 'serialize' when they share a risk-marker, have a recorded regression,
    or would collide on a migration number (likely halves of one contract / known
    to break each other / a guaranteed rebase conflict); else 'parallel'.
    Signal-free pairs are omitted (auto-parallel)."""
    names = [it["name"] for it in items]
    by_name = {it["name"]: it for it in items}
    files = {it["name"]: set(it["files"]) for it in items}
    adj_keys = list(adj)
    gfiles = {n: _item_graph_files(files[n], adj_keys) for n in names}
    matched = ({n: [e for e in trajectories if _entry_matches_item(e, by_name[n])] for n in names}
               if trajectories else {n: [] for n in names})
    co = set()
    for c in cluster_items(items):
        for i in range(len(c)):
            for j in range(i + 1, len(c)):
                co.add(frozenset((c[i], c[j])))
    out = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            if frozenset((a, b)) in co or files[a] & files[b]:
                continue
            signals = []
            if any(bf in adj.get(af, ()) for af in gfiles[a] for bf in gfiles[b]):
                signals.append("import-adjacent")
            shared = sorted({m for m in risk_markers
                             if any(m in f for f in files[a]) and any(m in f for f in files[b])})
            signals += ["shared-risk-marker:" + m for m in shared]
            regression = (any(_entry_regresses_item(e, by_name[b]) for e in matched[a])
                          or any(_entry_regresses_item(e, by_name[a]) for e in matched[b]))
            if regression:
                signals.append("regression-history")
            mig = sorted(_migration_apps(files[a]) & _migration_apps(files[b]))
            signals += ["same-migration-app:" + (m or "<root>") for m in mig]
            if signals:
                out.append({"pair": [a, b], "signals": signals,
                            "default": "serialize" if (shared or regression or mig) else "parallel"})
    return out


def tier_for(file_paths, risk_markers):
    for f in file_paths:
        if any(m in f for m in risk_markers):
            return "top"
    return "cheap"


def ready_after(items, waves):
    """Per item, the items that must INTEGRATE before it can start: its declared
    `after` producers, plus any item it CONFLICTS with that the schedule placed
    earlier. This is the dependency DAG the waves flatten. A consumer that honors
    it can pipeline - start each item the moment ITS predecessors land - instead
    of waiting on a whole-wave barrier, where every item pays for the slowest
    member of the wave before it. Same safety, less idling: an item never starts
    before anything it shares a file (or a contract_group) with."""
    after = _after_edges(items)
    adj = _conflict_adjacency(items)
    wave_of = {n: i for i, w in enumerate(waves) for n in w}
    out = {}
    for it in items:
        n = it["name"]
        deps = set(after[n])
        deps |= {m for m in adj.get(n, ())
                 if wave_of.get(m, -1) < wave_of.get(n, 0)}
        out[n] = sorted(deps)
    return out


def context_packs(items, adj, hops=1, cap=40):
    """Per item, the files an agent should READ to do the work: its own edited
    files, plus their graph neighbours out to `hops`. An agent spends most of its
    tokens LOCATING the work - grepping, globbing, opening files to find the edit
    site - not performing it; handing over the read-set removes that search. The
    neighbour list is capped (a hub file would otherwise pull in half the repo)
    and the overflow COUNT is reported rather than silently dropped."""
    packs = {}
    adj_keys = list(adj)
    for it in items:
        own = list(dict.fromkeys(it["files"]))
        anchors = _item_graph_files(set(own), adj_keys) if adj else set()
        seen = set(own) | anchors
        frontier = anchors
        for _ in range(max(0, hops)):
            nxt = set()
            for f in frontier:
                nxt |= adj.get(f, set())
            frontier = nxt - seen
            if not frontier:
                break
            seen |= frontier
        pack = {"edit": own}
        neighbours = sorted(seen - set(own) - anchors)
        if neighbours:
            pack["read"] = neighbours[:cap]
            if len(neighbours) > cap:
                pack["read_truncated"] = len(neighbours) - cap
        packs[it["name"]] = pack
    return packs


def shared_context(packs, waves):
    """Per wave, the files MORE THAN ONE of its items would read. Each agent that
    reads one pays for the same content separately, so the orchestrator can read
    a shared file once and pass a digest instead. Only files with >1 reader
    appear; a wave with no overlap is omitted."""
    out = {}
    for i, w in enumerate(waves):
        counts = defaultdict(int)
        for n in w:
            for f in packs.get(n, {}).get("read", []):
                counts[f] += 1
        shared = sorted(f for f, c in counts.items() if c > 1)
        if shared:
            out[str(i)] = shared
    return out


def coalesce_groups(items, waves, tier, cost, below=2.0, max_size=4, deps=None):
    """Within a wave, small CHEAP-tier items that ONE agent can take together
    instead of one agent each. A subagent costs a fixed spawn + context load
    before it does any work, so several trivial leaves are cheaper merged than
    fanned out - the deliberate inverse of fanning out, and where the tail of a
    long batch actually goes. Safe by construction: same-wave items are already
    mutually conflict-free. `top`-tier items are NEVER merged (they carry their
    own review depth), and this is advisory - the consumer may ignore it.

    Members must also share the SAME predecessors (`deps`): merging items with
    different ones would make the group wait on the UNION, re-introducing the
    barrier that ready_after exists to remove."""
    deps = deps or {}
    out = {}
    for i, w in enumerate(waves):
        small = [n for n in w
                 if tier.get(n) != "top" and cost.get(n, 1.0) <= below]
        by_deps = defaultdict(list)
        for n in small:
            by_deps[tuple(deps.get(n, ()))].append(n)
        groups = []
        for key in sorted(by_deps):
            bucket = by_deps[key]
            groups += [g for g in (bucket[k:k + max_size]
                                   for k in range(0, len(bucket), max_size))
                       if len(g) > 1]
        if groups:
            out[str(i)] = groups
    return out


def verdict_groups(review):
    """coupling_review pairs bucketed by IDENTICAL signal set + default, so the
    orchestrator renders ONE verdict per bucket rather than one per pair. The
    per-pair verdict is the only consumption cost that grows QUADRATICALLY with
    batch size, and it is all top-tier. Grouping keeps the discipline intact -
    every pair is still covered by a rendered verdict - at a fraction of the
    tokens. Ordered biggest bucket first."""
    buckets = {}
    for e in review:
        buckets.setdefault((tuple(e["signals"]), e["default"]), []).append(e["pair"])
    out = [{"signals": list(sig), "default": dflt, "count": len(pairs), "pairs": pairs}
           for (sig, dflt), pairs in buckets.items()]
    out.sort(key=lambda g: (-g["count"], g["signals"]))
    return out


def verify_modes(items, serial_markers):
    """Per item, HOW its verification runs. `serial` - the surface needs the
    orchestrator's own session (e.g. an authenticated UI that a session-less
    agent cannot reach), so those are checked one at a time; `offload` - a
    by-value check a session-less cheap agent can perform, so verification fans
    out the same way the build does. The PROJECT supplies the markers; nothing
    about any surface is baked in here."""
    return {it["name"]: ("serial" if any(m in f for m in serial_markers
                                         for f in it["files"]) else "offload")
            for it in items}


def plan(items, graph_path, risk_markers, trajectories=None,
         serial_verify_markers=None, context_hops=1, context_cap=40,
         coalesce_below=2.0, coalesce_max=4):
    """graph_path is OPTIONAL (None -> no import-adjacency signals; clustering,
    waves, tiers, and the marker/history coupling signals still compute)."""
    if graph_path:
        nodes, links = load_graph(graph_path)
        adj = build_file_coupling(nodes, links)
    else:
        adj = {}
    trajectories = trajectories or []
    tier, tier_notes = {}, {}
    for it in items:
        t = tier_for(it["files"], risk_markers)
        if t != "top" and trajectories:
            bump, reason = history_tier_bump(it, trajectories)
            if bump:
                t = "top"
                tier_notes[it["name"]] = "history: " + reason
        tier[it["name"]] = t
    conflict_adj = _conflict_adjacency(items)
    order_index = {it["name"]: i for i, it in enumerate(items)}
    decided = _after_connected(items)
    review = [p for p in coupling_review(items, adj, risk_markers, trajectories)
              if frozenset(p["pair"]) not in decided]
    cost = {it["name"]: item_cost(it) for it in items}
    waves = global_waves(items, conflict_adj, order_index,
                         downstream_cost(items, cost))
    packs = context_packs(items, adj, context_hops, context_cap)
    deps = ready_after(items, waves)
    result = {
        "clusters": cluster_items(items),
        "waves": waves,
        "ready_after": deps,
        "coupling_review": review,
        "tier": tier,
        "verify_mode": verify_modes(items, serial_verify_markers or []),
        "context_packs": packs,
    }
    if tier_notes:  # only present when history actually bumped something
        result["tier_notes"] = tier_notes
    for key, value in (("verdict_groups", verdict_groups(review)),
                       ("shared_context", shared_context(packs, waves)),
                       ("coalesce", coalesce_groups(items, waves, tier, cost,
                                                    coalesce_below, coalesce_max,
                                                    deps))):
        if value:  # omit empty sections rather than pad the plan
            result[key] = value
    result["plan_id"] = plan_id(result)
    return result


# ── runner (optional: EXECUTE the plan instead of only printing it) ─────────
#
# Without this, a plan is advice an agent may quietly ignore - and the usual way
# it is ignored is by running a parallel plan serially, which looks identical in
# the transcript and loses the entire speed win. Dispatching from the plan makes
# adherence true by construction rather than by hope, and it is what lets a
# runtime with no subagent primitive (a plain CLI agent) still fan out: spawning
# becomes a SUBPROCESS concern, which every runtime has.


def plan_id(plan_obj):
    """Stable short hash of a plan, so a run log can be tied to the plan it
    claims to have executed - 'was the parallel plan actually run in parallel?'
    becomes checkable instead of assumed."""
    body = {k: v for k, v in plan_obj.items() if k != "plan_id"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def render_prompt(item, pack, tier, template=None):
    """The self-contained prompt for ONE item. A spawned process is a FRESH
    session - it inherits no conversation - so everything needed must be in
    here. This is what context_packs buys: the agent is TOLD what to edit and
    what to read rather than spending its tokens finding out."""
    edit = ", ".join(pack.get("edit", []) or item.get("files", []))
    read = ", ".join(pack.get("read", []))
    if template:
        for key, val in (("{name}", item["name"]), ("{task}", item.get("task", "")),
                         ("{edit}", edit), ("{read}", read), ("{tier}", tier)):
            template = template.replace(key, val)
        return template
    lines = ["Task: " + (item.get("task") or item["name"])]
    if edit:
        lines.append("Edit ONLY these files: " + edit)
    if read:
        lines.append("Read for context, do not edit: " + read)
    lines.append("Other agents are working in this tree RIGHT NOW on disjoint "
                 "files. Touching anything outside the edit list will collide "
                 "with them.")
    return "\n".join(lines)


def build_argv(exec_template, prompt, item):
    """argv WITHOUT a shell: split the template FIRST, then substitute into the
    resulting tokens, so a prompt containing quotes, newlines, backticks or
    semicolons lands as ONE argument and can never be re-read as shell syntax."""
    return [tok.replace("{prompt}", prompt)
               .replace("{name}", item["name"])
               .replace("{files}", ",".join(item.get("files", [])))
            for tok in shlex.split(exec_template)]


def run_plan(plan_obj, items, exec_template, concurrency=4, dry_run=False,
             prompt_template=None, cwd=None, poll=0.2):
    """Dispatch every item whose predecessors have SUCCEEDED, up to `concurrency`
    at once, unlocking dependents as each finishes - the `ready_after` DAG driven
    directly, so nothing idles behind an unrelated slow neighbour. Dispatch order
    follows the plan (waves are already priority-sorted), so long poles go first.

    Same-wave items are file-DISJOINT by construction, so one shared working tree
    is safe - worktrees are only needed if you want per-item commits.

    A non-zero exit marks an item `failed` and its dependents `blocked`; work
    that does not depend on the failure keeps going. Nothing is ever recorded as
    done because it was skipped."""
    by_name = {it["name"]: it for it in items}
    deps = plan_obj.get("ready_after", {})
    packs, tier = plan_obj.get("context_packs", {}), plan_obj.get("tier", {})
    pending = [n for w in plan_obj.get("waves", []) for n in w]
    status, running, records = {}, {}, []

    def ok(n):
        return all(status.get(d) == "ok" for d in deps.get(n, []))

    def doomed(n):
        return any(status.get(d) in ("failed", "blocked") for d in deps.get(n, []))

    while pending or running:
        for name in [n for n, (p, _) in running.items() if p.poll() is not None]:
            proc, started = running.pop(name)
            status[name] = "ok" if proc.returncode == 0 else "failed"
            records.append({"item": name, "status": status[name],
                            "exit": proc.returncode,
                            "seconds": round(time.time() - started, 2)})
        for n in [n for n in pending if doomed(n)]:
            pending.remove(n)
            status[n] = "blocked"
            records.append({"item": n, "status": "blocked",
                            "reason": "a predecessor did not succeed"})
        while len(running) < concurrency:
            nxt = next((n for n in pending if ok(n)), None)
            if nxt is None:
                break
            pending.remove(nxt)
            item = by_name.get(nxt) or {"name": nxt, "files": []}
            prompt = render_prompt(item, packs.get(nxt, {}),
                                   tier.get(nxt, "cheap"), prompt_template)
            argv = build_argv(exec_template, prompt, item)
            if dry_run:
                status[nxt] = "ok"
                records.append({"item": nxt, "status": "dry-run", "argv": argv})
            else:
                running[nxt] = (subprocess.Popen(argv, cwd=cwd), time.time())
        if running:
            if not dry_run:
                time.sleep(poll)
        elif pending and not any(ok(n) for n in pending):
            for n in pending:  # unreachable deps: say so, never silently drop
                status[n] = "blocked"
                records.append({"item": n, "status": "blocked",
                                "reason": "dependencies can no longer be satisfied"})
            pending = []
    counts = defaultdict(int)
    for r in records:
        counts[r["status"]] += 1
    return {"plan_id": plan_obj.get("plan_id") or plan_id(plan_obj),
            "concurrency": concurrency, "dispatched": records,
            "summary": dict(counts)}


def main():
    ap = argparse.ArgumentParser(description="Plan a fan-out from a graphify graph.")
    ap.add_argument("--graph", default=None,
                    help="path to graphify graph.json (optional: without it the "
                         "plan loses only the import-adjacency coupling signal)")
    ap.add_argument("--items", required=True,
                    help='JSON file: [{"name","files":[...],"contract_group"?:"tag",'
                         '"after"?:["producer-item", ...]}]')
    ap.add_argument("--risk-markers", default="",
                    help="comma-separated path substrings that force the top tier")
    ap.add_argument("--trajectories", default=DEFAULT_TRAJECTORY_STORE,
                    help="trajectory-kb JSONL store for history-aware tiering + coupling "
                         "(default: the global store; missing file = ignored)")
    ap.add_argument("--no-trajectories", action="store_true",
                    help="ignore trajectory memory (marker-only plan)")
    ap.add_argument("--serial-verify-markers", default="",
                    help="comma-separated path substrings whose leaves must be "
                         "VERIFIED on the orchestrator's own session (e.g. an "
                         "authenticated UI); everything else offloads to a "
                         "session-less checker")
    ap.add_argument("--context-hops", type=int, default=1,
                    help="graph hops to include in each item's read-set "
                         "(0 = edited files only; default 1)")
    ap.add_argument("--max-context-files", type=int, default=40,
                    help="cap on an item's neighbour read-set; the overflow "
                         "count is reported, never silently dropped (default 40)")
    ap.add_argument("--coalesce-below", type=float, default=2.0,
                    help="cheap-tier items at or below this cost may be grouped "
                         "into one agent (0 disables; default 2)")
    ap.add_argument("--coalesce-max", type=int, default=4,
                    help="most items in one coalesced group (default 4)")
    ap.add_argument("--exec", dest="exec_template", default=None,
                    help="EXECUTE the plan: a command template run once per item, "
                         "e.g. 'claude -p {prompt}' or 'codex exec {prompt}'. "
                         "Split into argv BEFORE substitution (no shell). "
                         "Placeholders: {prompt} {name} {files}. Omit to only print "
                         "the plan.")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="most items dispatched at once (default 4)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --exec: show the dispatch order and argv, spawn nothing")
    ap.add_argument("--prompt-template", default=None,
                    help="override the per-item prompt; placeholders {name} {task} "
                         "{edit} {read} {tier}")
    ap.add_argument("--plan-file", default=None,
                    help="with --exec: execute this saved plan instead of "
                         "recomputing one (plan, review it, then run it)")
    ap.add_argument("--run-log", default=None,
                    help="with --exec: write the dispatch record as JSONL, so what "
                         "ACTUALLY ran can be checked against the plan")
    args = ap.parse_args()
    with open(args.items, encoding="utf-8") as f:
        items = json.load(f)
    markers = [m for m in args.risk_markers.split(",") if m]
    serial_markers = [m for m in args.serial_verify_markers.split(",") if m]
    trajectories = [] if args.no_trajectories else load_trajectories(args.trajectories)
    if args.plan_file:
        with open(args.plan_file, encoding="utf-8") as f:
            computed = json.load(f)
    else:
        computed = plan(items, args.graph, markers, trajectories,
                        serial_markers, args.context_hops,
                        args.max_context_files, args.coalesce_below,
                        args.coalesce_max)
    if not args.exec_template:
        print(json.dumps(computed, indent=2))
        return
    result = run_plan(computed, items, args.exec_template, args.concurrency,
                      args.dry_run, args.prompt_template)
    if args.run_log:
        with open(args.run_log, "w", encoding="utf-8") as f:
            for record in result["dispatched"]:
                f.write(json.dumps(dict(record, plan_id=result["plan_id"])) + "\n")
    print(json.dumps(result, indent=2))
    # a failed or blocked item must not exit 0 - a green exit is a claim
    if result["summary"].get("failed") or result["summary"].get("blocked"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
