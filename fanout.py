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
import re
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


def msp_items(items):
    """Group items into MSPs - the unit that becomes one branch and one PR.

    Items are unioned when they MUST serialize: a shared edited file, or a
    declared contract_group (two halves of one contract, even when their edited
    files are disjoint). Distinct MSPs are file- and contract-disjoint, so two
    open PRs never touch the same file.

    This does not DEFINE an MSP boundary - recon does that, semantically. This
    only FUSES two that turn out to collide, because a cluster converges into
    one branch and so cannot span two MSPs."""
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


def _pinned_contract_groups(items):
    """contract_groups whose interface is PINNED by a producer item in the same
    group (an item of type "contract" that the other members declare `after`).
    An unpinned group must serialize under one owner - two agents building two
    halves of one interface invent two different shapes. A pinned one may split,
    because the shape is fixed before either half starts."""
    pinned = set()
    by_group = defaultdict(list)
    for it in items:
        g = it.get("contract_group")
        if g:
            by_group[g].append(it)
    for g, members in by_group.items():
        producers = {m["name"] for m in members if m.get("type") == "contract"}
        if not producers:
            continue
        consumers = [m for m in members if m["name"] not in producers]
        if consumers and all(producers & set(m.get("after") or [])
                             for m in consumers):
            pinned.add(g)
    return pinned


def cluster_items(items):
    """Partition items into CLUSTERS: the unit one agent owns and walks
    sequentially.

    A cluster is a weakly-connected component of the serialization graph, whose
    edges are the two things that force two leaves not to run concurrently:

      - a shared edited file, or an UNPINNED contract_group  (undirected: they
        must not run at once, but either order is fine)
      - a declared `after` edge                              (directed: one
        genuinely needs the other's output)

    Both are satisfied by the same primitive - one agent, sequential - so both
    are edges here. Independent leaves come out as clusters of one, which is the
    common case and the best one.

    An `after` edge whose endpoints land in DIFFERENT clusters (a diamond, or a
    dependency across MSPs) stays a dispatch edge; see `cluster_after`."""
    parent = {it["name"]: it["name"] for it in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    msp_of = {}
    for group in msp_items(items):
        for n in group:
            msp_of[n] = group[0]

    pinned = _pinned_contract_groups(items)
    owner, group_owner = {}, {}
    by_name = {it["name"]: it for it in items}
    for it in items:
        for f in it["files"]:
            if f in owner:
                union(it["name"], owner[f])
            else:
                owner[f] = it["name"]
        g = it.get("contract_group")
        if g and g not in pinned:
            if g in group_owner:
                union(it["name"], group_owner[g])
            else:
                group_owner[g] = it["name"]

    # A directed edge fuses only where fusing COSTS nothing: a true chain link,
    # one producer with one consumer. Fusing a BRANCHING edge would destroy the
    # parallelism it exists to enable - a pinned contract is exactly one
    # producer with two consumers, and the whole point of pinning is that the
    # two halves then run at once. Branching edges stay dispatch edges.
    # Cross-MSP edges never fuse either: that would collapse two PRs into one.
    consumers = defaultdict(set)
    producers = defaultdict(set)
    for it in items:
        for dep in (it.get("after") or []):
            if dep in by_name:
                consumers[dep].add(it["name"])
                producers[it["name"]].add(dep)
    for it in items:
        n = it["name"]
        for dep in producers[n]:
            same_msp = msp_of.get(dep) == msp_of.get(n)
            chain_link = len(consumers[dep]) == 1 and len(producers[n]) == 1
            if same_msp and chain_link:
                union(n, dep)

    groups = defaultdict(list)
    for it in items:
        groups[find(it["name"])].append(it["name"])
    return list(groups.values())


def cluster_after(items, clusters):
    """Per cluster, the OTHER clusters whose build must complete before it
    starts - the `after` edges whose endpoints fell in different clusters.

    Carried by the CLUSTER that needs it, never by its MSP: if one of B's five
    leaves depends on A, that leaf's cluster waits and B's other four start
    immediately. Gating a whole MSP would be a barrier at MSP scope, which is
    the error waves made one level up."""
    cid = {}
    for i, members in enumerate(clusters):
        for n in members:
            cid[n] = i
    out = {i: set() for i in range(len(clusters))}
    for it in items:
        mine = cid[it["name"]]
        for dep in (it.get("after") or []):
            if dep in cid and cid[dep] != mine:
                out[mine].add(cid[dep])
    return {i: sorted(v) for i, v in out.items() if v}


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
                             if any(marker_matches(f, m) for f in files[a])
                             and any(marker_matches(f, m) for f in files[b])})
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


_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _path_words(path):
    """The words in a path, split on separators AND camelCase, lowercased.

    `app/types/AuthResponse.ts` -> {app, types, auth, response, ts}
    `app/(unauthenticated)/x`   -> {app, unauthenticated, x}

    The second one is the whole point: "unauthenticated" is one word and does
    NOT contain the word "auth", even though it contains the substring."""
    return {w.lower() for part in re.split(r"[^A-Za-z0-9]+", path) if part
            for w in _WORD.findall(part)}


def marker_matches(path, marker):
    """Does a risk marker apply to this path?

    Raw substring matching was wrong in both directions at once: marker `auth`
    fired on `app/(unauthenticated)/forgotPassword/page.tsx` (a copy change sent
    to the expensive model) and missed `app/types/AuthResponse.ts` (auth code
    sent to the cheap one).

    So: a plain marker matches a WORD of the path, case-insensitively. A marker
    containing any separator (`api/auth`, `.sql`) is meant as a path fragment and
    keeps case-insensitive substring behaviour, since that is the only thing it
    could mean."""
    m = marker.strip().lower()
    if not m:
        return False
    if not m.isalnum():
        return m in path.lower()
    return m in _path_words(path)


def tier_for(file_paths, risk_markers, complexity=None):
    """Two axes, because blast radius alone mis-routes in both directions: a
    one-word copy change under a marked path comes out `top`, and a hard
    refactor in an unmarked one comes out `cheap`.

                 | low blast radius | high blast radius
      simple     | cheap            | top
      complex    | top              | top

    `cheap` requires BOTH mechanical and low-blast-radius. The asymmetry is
    deliberate: a wrong cheap call costs a rebuild, a wrong top call costs
    tokens.

    complexity is OPTIONAL and comes from recon, which is the only part of the
    system that can judge it. When it is absent this falls back to the
    blast-radius-only behaviour, so a caller that does not supply it is
    unaffected."""
    for f in file_paths:
        if any(marker_matches(f, m) for m in risk_markers):
            return "top"
    if complexity is not None and str(complexity).lower() not in ("simple", "low",
                                                                 "mechanical"):
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
        t = tier_for(it["files"], risk_markers, it.get("complexity"))
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
    msps = msp_items(items)
    clusters = cluster_items(items)
    result = {
        "msps": msps,
        "clusters": clusters,
        "cluster_after": cluster_after(items, clusters),
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
    # An item with no `task` dispatches an agent whose entire brief is a NAME.
    # That is a plan defect, and a silent one - the run looks normal and the
    # work is guesswork. Name them so the caller fixes the items, not the plan.
    thin = sorted(it["name"] for it in items
                  if not (it.get("task") or it.get("text")))
    if thin:
        result["underspecified"] = thin
    result["plan_id"] = plan_id(result)
    return result


def reconcile(items, clusters, actual_files):
    """Check the prediction the whole plan rests on.

    v2 derives merge safety from files recon PREDICTED, which is a guess. This
    compares it against what each cluster actually changed (`files_changed` in
    the return contract) and reports the misses that matter.

    A miss inside the item's own MSP is noise - the file was already inside a
    unit that ships together. A miss that lands in ANOTHER MSP's file set means
    two MSPs were not disjoint after all and the plan parallelized a real
    collision. That is the finding; it is reported loudly rather than inferred
    later from a merge conflict."""
    msp_of, msp_files = {}, defaultdict(set)
    by_name = {it["name"]: it for it in items}
    for group in msp_items(items):
        for n in group:
            msp_of[n] = group[0]
    for it in items:
        msp_files[msp_of[it["name"]]] |= set(it["files"])

    cluster_of = {}
    for i, members in enumerate(clusters):
        for n in members:
            cluster_of[n] = i

    findings, clean = [], True
    for name, changed in sorted(actual_files.items()):
        it = by_name.get(name)
        if it is None:
            continue
        predicted, mine = set(it["files"]), msp_of[name]
        for f in sorted(set(changed) - predicted):
            collided = sorted(m for m, fs in msp_files.items()
                              if m != mine and f in fs)
            if collided:
                clean = False
                findings.append({
                    "item": name, "file": f, "severity": "collision",
                    "msp": mine, "also_in_msps": collided,
                    "detail": "recon did not predict this file, and it belongs "
                              "to another MSP - those MSPs were not disjoint",
                })
            else:
                findings.append({
                    "item": name, "file": f, "severity": "noise",
                    "msp": mine,
                    "detail": "unpredicted, but inside this item's own MSP",
                })
    return {"clean": clean, "findings": findings,
            "collisions": sum(1 for f in findings
                              if f["severity"] == "collision")}


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


# What a worker must hand back. A caller's context grows by whatever its workers
# RETURN, so an unbounded prose report is the single biggest source of bloat in a
# long run - and the part nobody notices, because each individual report looks
# reasonable. A fixed, small schema caps it by construction.
RETURN_CONTRACT = (
    'When finished, print ONE line of JSON and nothing after it:\n'
    '{"item":"<name>","status":"ok"|"failed","files_changed":["path",...],'
    '"notes":"<=200 chars"}\n'
    'Keep it short. Anything outside that line is recorded but not read back.'
)


def render_prompt(item, pack, tier, template=None, contract=RETURN_CONTRACT):
    """The self-contained prompt for ONE item. A spawned process is a FRESH
    session - it inherits no conversation - so everything needed must be in
    here. This is what context_packs buys: the agent is TOLD what to edit and
    what to read rather than spending its tokens finding out."""
    edit = ", ".join(pack.get("edit", []) or item.get("files", []))
    read = ", ".join(pack.get("read", []))
    if template:
        for key, val in (("{name}", item["name"]), ("{task}", item.get("task", "")),
                         ("{edit}", edit), ("{read}", read), ("{tier}", tier),
                         ("{contract}", contract or "")):
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
    if contract:
        lines.append("")
        lines.append(contract)
    return "\n".join(lines)


def parse_return(text):
    """The worker's structured line, if it produced one: the LAST line that parses
    as a JSON object. Everything else it printed stays in the log file and out of
    the caller's context."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def parse_returns(text):
    """Every structured line a worker produced, oldest first.

    A cluster agent owns SEVERAL leaves and reports one line each, so the
    single-line reader is not enough. Kept separate from `parse_return` rather
    than changing its return type, because callers depend on that shape."""
    out = []
    for line in (text or "").strip().splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
    return out


def cluster_order(clusters, edges, items):
    """Dispatch priority: whatever unblocks the most work goes first.

    Ranked by how many clusters wait on it (transitively), then by size. A
    cluster nothing depends on can start any time; one that three others are
    waiting for should not be sitting in the queue behind it."""
    cost = {it["name"]: item_cost(it) for it in items}
    waiters = defaultdict(set)
    for cid, deps in (edges or {}).items():
        for d in deps:
            waiters[int(d)].add(int(cid))
    # transitive: a cluster also unblocks whatever ITS waiters unblock
    def downstream(i, seen=None):
        seen = seen if seen is not None else set()
        for w in waiters.get(i, ()):
            if w not in seen:
                seen.add(w)
                downstream(w, seen)
        return seen
    ranked = sorted(
        range(len(clusters)),
        key=lambda i: (-len(downstream(i)),
                       -sum(cost.get(n, 1) for n in clusters[i]), i))
    return ranked


def render_cluster_prompt(members, by_name, packs, tier, order,
                          template=None, contract=None):
    """The self-contained brief for ONE cluster - the JSON spec the agent works
    from.

    A cluster is the unit one agent owns: it may hold several leaves, and it
    walks them in `order` because a later leaf can depend on an earlier one. A
    spawned process inherits no conversation, so the whole brief has to be here.
    The leaf list is emitted as JSON rather than prose so a non-LLM worker (a
    script, a wrapper) can consume the same dispatch."""
    seq = [n for n in order if n in members] + [n for n in members if n not in order]
    leaves = []
    for n in seq:
        item = by_name.get(n, {"name": n, "files": []})
        pack = packs.get(n, {})
        edit = pack.get("edit") or item.get("files", [])
        leaf = {"name": n,
                "task": item.get("task") or item.get("text") or n,
                "edit": edit,
                "read": pack.get("read", [])}
        if item.get("acceptance"):
            leaf["acceptance"] = item["acceptance"]
        # Per-file intent, when recon supplied it. `edit` says WHICH files; for a
        # leaf spanning more than a couple, that leaves the agent to infer what
        # each one is for. Recon already looked at them, so it can say.
        notes = {f: v for f, v in (item.get("file_notes") or {}).items() if f in edit}
        if notes:
            leaf["file_notes"] = notes
        leaves.append(leaf)
    spec = json.dumps({"leaves": leaves}, indent=2)
    if template:
        for key, val in (("{cluster}", ", ".join(seq)), ("{spec}", spec),
                         ("{tier}", tier), ("{contract}", contract or "")):
            template = template.replace(key, val)
        return template
    lines = []
    if len(seq) == 1:
        lines.append("Task: " + leaves[0]["task"])
    else:
        lines.append("You own %d related pieces of work. Do them IN THE ORDER "
                     "GIVEN - a later one may build on an earlier one." % len(seq))
    lines.append("")
    lines.append(spec)
    lines.append("")
    lines.append("Edit ONLY the files listed under `edit`. Other agents are "
                 "working in this tree RIGHT NOW on disjoint files; touching "
                 "anything outside your edit lists will collide with them.")
    if contract:
        lines.append("")
        if len(seq) > 1:
            lines.append("Emit ONE such line PER LEAF, in order:")
        lines.append(contract)
    return "\n".join(lines)


def build_argv(exec_template, prompt, item):
    """argv WITHOUT a shell: split the template FIRST, then substitute into the
    resulting tokens, so a prompt containing quotes, newlines, backticks or
    semicolons lands as ONE argument and can never be re-read as shell syntax."""
    return [tok.replace("{prompt}", prompt)
               .replace("{name}", item["name"])
               .replace("{files}", ",".join(item.get("files", [])))
            for tok in shlex.split(exec_template)]


def _safe(name):
    """Item name as a filename component - an item name is caller-supplied and
    must never escape the run directory."""
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)[:80] or "item"


def _write_state(run_dir, state):
    """Persist after EVERY completion, not at the end. The point of state on disk
    is that an interrupted run - a crash, a kill, a caller that ran out of room
    and handed over to a fresh one - can be resumed from it. State that is only
    written at the end is exactly the state you do not have when you need it."""
    if not run_dir:
        return
    tmp = os.path.join(run_dir, "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, os.path.join(run_dir, "state.json"))  # atomic: never half-written


def load_state(run_dir):
    """Previously recorded item outcomes, for --resume."""
    try:
        with open(os.path.join(run_dir, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def run_plan(plan_obj, items, exec_template, concurrency=4, dry_run=False,
             prompt_template=None, cwd=None, poll=0.2, run_dir=None,
             resume=False, max_output=4000, contract=RETURN_CONTRACT):
    """Dispatch ONE AGENT PER CLUSTER, unlocking dependents as each finishes.

    The cluster is the unit of work: an agent owns its leaves and walks them in
    order, because everything that must serialize is already inside one cluster.
    So dispatch has no barrier - a cluster with no `cluster_after` edge starts
    immediately whatever MSP it belongs to, and one with an edge starts the
    moment ITS producer finishes, never when an unrelated slow neighbour does.

    Priority goes to whatever unblocks the most work, so a cluster three others
    wait on does not sit behind one nothing depends on.

    A non-zero exit marks the cluster `failed` and its dependents `blocked`;
    independent work keeps going. Nothing is recorded as done because it was
    skipped."""
    by_name = {it["name"]: it for it in items}
    clusters = plan_obj.get("clusters") or [[it["name"]] for it in items]
    edges = {int(k): [int(v) for v in vs]
             for k, vs in (plan_obj.get("cluster_after") or {}).items()}
    packs, tier = plan_obj.get("context_packs", {}), plan_obj.get("tier", {})
    within = plan_obj.get("ready_after", {})
    order = [n for w in plan_obj.get("waves", []) for n in w]
    pid = plan_obj.get("plan_id") or plan_id(plan_obj)

    def label(i):
        return clusters[i][0] if len(clusters[i]) == 1 else "cluster-%d" % i

    def cluster_tier(i):
        # the riskiest leaf sets the cluster's tier: one agent does all of them
        return "top" if any(tier.get(n) == "top" for n in clusters[i]) else "cheap"

    pending = cluster_order(clusters, edges, items)
    status, running, records = {}, {}, []

    state = {"plan_id": pid, "items": {}}
    if run_dir and not dry_run:
        os.makedirs(os.path.join(run_dir, "items"), exist_ok=True)
        with open(os.path.join(run_dir, "plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan_obj, f, indent=2)   # the plan IS the state: keep it beside the log
        if resume:
            prior = load_state(run_dir)
            if prior.get("plan_id") not in (None, pid):
                raise ValueError("--resume against a DIFFERENT plan (%s != %s): the "
                                 "work-set changed, so prior results do not apply"
                                 % (prior.get("plan_id"), pid))
            state["items"] = prior.get("items", {})
            for i in list(pending):
                if state["items"].get(label(i), {}).get("status") == "ok":
                    pending.remove(i)
                    status[i] = "ok"
                    records.append({"item": label(i), "cluster": label(i),
                                    "status": "resumed", "exit": 0})

    def ok(i):
        return all(status.get(d) == "ok" for d in edges.get(i, []))

    def doomed(i):
        return any(status.get(d) in ("failed", "blocked") for d in edges.get(i, []))

    while pending or running:
        for i in [i for i, (p, _, _) in running.items() if p.poll() is not None]:
            proc, started, handles = running.pop(i)
            for h in handles:
                h.close()
            status[i] = "ok" if proc.returncode == 0 else "failed"
            record = {"item": label(i), "cluster": label(i), "items": clusters[i],
                      "tier": cluster_tier(i),
                      "status": status[i], "exit": proc.returncode,
                      "seconds": round(time.time() - started, 2)}
            if run_dir:
                out_path = os.path.join(run_dir, "items", "%s.out" % _safe(label(i)))
                try:
                    with open(out_path, encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    text = ""
                returned = parse_returns(text)
                if returned:
                    # `returned` keeps its old shape - the last structured line -
                    # so existing consumers are unaffected. A multi-leaf cluster
                    # reports one line per leaf, and those go in `returns`.
                    record["returned"] = returned[-1]
                    if len(returned) > 1:
                        record["returns"] = returned
                record["output_file"] = out_path       # the unbounded half stays on disk
                if not returned and text:
                    record["output_tail"] = text[-max_output:]
            records.append(record)
            state["items"][label(i)] = record
            _write_state(run_dir, state)
        for i in [i for i in pending if doomed(i)]:
            pending.remove(i)
            status[i] = "blocked"
            blocked = {"item": label(i), "cluster": label(i),
                       "items": clusters[i], "status": "blocked",
                       "reason": "a predecessor cluster did not succeed"}
            records.append(blocked)
            state["items"][label(i)] = blocked
            _write_state(run_dir, state)
        while len(running) < concurrency:
            nxt = next((i for i in pending if ok(i)), None)
            if nxt is None:
                break
            pending.remove(nxt)
            members = clusters[nxt]
            prompt = render_cluster_prompt(members, by_name, packs,
                                           cluster_tier(nxt), order,
                                           prompt_template, contract)
            head = by_name.get(members[0], {"name": members[0], "files": []})
            argv = build_argv(exec_template, prompt, head)
            if dry_run:
                status[nxt] = "ok"
                records.append({"item": label(nxt), "cluster": label(nxt),
                                "items": members, "tier": cluster_tier(nxt),
                                "status": "dry-run", "argv": argv})
            else:
                files = sorted({f for n in members
                                for f in by_name.get(n, {}).get("files", [])})
                # Output goes to a FILE, never to the caller's stream: a worker that
                # decides to narrate must not be able to flood whoever is watching.
                # A worker needs to know which cluster it IS to satisfy the return
                # contract; that is in the prompt, but a non-LLM worker (a script,
                # a wrapper) should not have to parse prose to find it.
                env = dict(os.environ, FANOUT_CLUSTER=label(nxt),
                           FANOUT_ITEM=members[0], FANOUT_ITEMS=",".join(members),
                           FANOUT_PLAN_ID=pid, FANOUT_TIER=cluster_tier(nxt),
                           FANOUT_FILES=",".join(files))
                handles = []
                if run_dir:
                    out = open(os.path.join(run_dir, "items",
                                            "%s.out" % _safe(label(nxt))),
                               "w", encoding="utf-8")
                    handles = [out]
                    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out,
                                            stderr=subprocess.STDOUT)
                else:
                    proc = subprocess.Popen(argv, cwd=cwd, env=env)
                running[nxt] = (proc, time.time(), handles)
        if running:
            if not dry_run:
                time.sleep(poll)
        elif pending and not any(ok(i) for i in pending):
            for i in pending:  # unreachable deps: say so, never silently drop
                status[i] = "blocked"
                records.append({"item": label(i), "cluster": label(i),
                                "items": clusters[i], "status": "blocked",
                                "reason": "dependencies can no longer be satisfied"})
            pending = []
    counts = defaultdict(int)
    for r in records:
        counts[r["status"]] += 1
    _write_state(run_dir, state)
    result = {"plan_id": pid, "concurrency": concurrency,
              "dispatched": records, "summary": dict(counts)}
    if run_dir:
        result["run_dir"] = run_dir
    return result


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
    ap.add_argument("--run-dir", default=None,
                    help="with --exec: keep the run's STATE on disk here (plan.json, "
                         "state.json, items/<name>.out). Default .fanout/<plan_id>. "
                         "State is written after every completion, so an interrupted "
                         "run is resumable and a caller need not hold it in context.")
    ap.add_argument("--no-run-dir", action="store_true",
                    help="do not persist run state (worker output goes to this "
                         "process's stdout instead)")
    ap.add_argument("--resume", action="store_true",
                    help="skip items already recorded ok in --run-dir; refuses to "
                         "resume across a different plan_id")
    ap.add_argument("--max-output-bytes", type=int, default=4000,
                    help="how much of a worker's raw output to keep in the record "
                         "when it returned no structured line (default 4000)")
    ap.add_argument("--no-return-contract", action="store_true",
                    help="do not ask workers for a structured one-line result "
                         "(they will return prose, which is what bloats a caller)")
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
    run_dir = None
    if not args.no_run_dir and not args.dry_run:
        run_dir = args.run_dir or os.path.join(
            ".fanout", computed.get("plan_id") or plan_id(computed))
    result = run_plan(computed, items, args.exec_template, args.concurrency,
                      args.dry_run, args.prompt_template, run_dir=run_dir,
                      resume=args.resume, max_output=args.max_output_bytes,
                      contract=None if args.no_return_contract else RETURN_CONTRACT)
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
