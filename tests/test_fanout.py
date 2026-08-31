import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fanout as fp


class FanoutPlanTests(unittest.TestCase):
    def test_cluster_items_groups_shared_files(self):
        items = [
            {"name": "A", "files": ["x.py"]},
            {"name": "B", "files": ["x.py", "y.py"]},   # shares x.py with A
            {"name": "C", "files": ["z.py"]},           # disjoint
        ]
        clusters = sorted(sorted(c) for c in fp.cluster_items(items))
        self.assertEqual(clusters, [["A", "B"], ["C"]])

    def test_cluster_items_groups_contract_group(self):
        # File-disjoint but declared as halves of one contract -> serialize together.
        items = [
            {"name": "svc", "files": ["services/billing_export.py"], "contract_group": "billing-split"},
            {"name": "view", "files": ["views_billing_export.py"], "contract_group": "billing-split"},
            {"name": "other", "files": ["unrelated.tsx"]},
        ]
        clusters = sorted(sorted(c) for c in fp.cluster_items(items))
        self.assertEqual(clusters, [["other"], ["svc", "view"]])

    def test_paths_match_suffix_boundary(self):
        self.assertTrue(fp._paths_match("backend/app/services/billing.py", "app/services/billing.py"))
        self.assertTrue(fp._paths_match("a/b.py", "b.py"))
        self.assertTrue(fp._paths_match("x.py", "x.py"))
        self.assertFalse(fp._paths_match("xapp/s.py", "app/s.py"))  # not a /-boundary suffix
        self.assertFalse(fp._paths_match("a.py", "b.py"))

    def test_coupling_review_import_adjacent_defaults_parallel(self):
        items = [{"name": "A", "files": ["pkg/a.py"]}, {"name": "B", "files": ["pkg/b.py"]}]
        adj = {"pkg/a.py": {"pkg/b.py"}, "pkg/b.py": {"pkg/a.py"}}
        out = fp.coupling_review(items, adj, [])
        self.assertEqual(out[0]["pair"], ["A", "B"])
        self.assertIn("import-adjacent", out[0]["signals"])
        self.assertEqual(out[0]["default"], "parallel")

    def test_coupling_review_shared_risk_marker_defaults_serialize(self):
        items = [{"name": "A", "files": ["ledger_amounts.py"]}, {"name": "B", "files": ["payout_amounts.py"]}]
        out = fp.coupling_review(items, {}, ["_amounts"])
        self.assertEqual(out[0]["default"], "serialize")
        self.assertIn("shared-risk-marker:_amounts", out[0]["signals"])

    def test_coupling_review_same_migration_app_defaults_serialize(self):
        # File-disjoint leaves (different migration filenames) that both add a
        # migration to the SAME app collide on the number -> serialize signal.
        items = [
            {"name": "A", "files": ["app/modules/onboarding/migrations/0089_audit.py"]},
            {"name": "B", "files": ["app/modules/onboarding/migrations/0089_churn.py"]},
        ]
        out = fp.coupling_review(items, {}, [])
        self.assertEqual(out[0]["default"], "serialize")
        self.assertIn("same-migration-app:app/modules/onboarding", out[0]["signals"])

    def test_coupling_review_migrations_in_different_apps_stay_parallel(self):
        items = [
            {"name": "A", "files": ["apps/billing/migrations/0089_x.py"]},
            {"name": "B", "files": ["apps/customers/migrations/0089_y.py"]},
        ]
        out = fp.coupling_review(items, {}, [])
        self.assertEqual(out, [])  # different apps -> no collision, signal-free

    def test_coupling_review_omits_signal_free_and_co_clustered(self):
        items = [
            {"name": "A", "files": ["a.tsx"]},
            {"name": "B", "files": ["b.tsx"]},                              # A/B signal-free
            {"name": "C", "files": ["c.py"], "contract_group": "g"},
            {"name": "D", "files": ["d.py"], "contract_group": "g"},        # C/D already serialized
        ]
        out = fp.coupling_review(items, {}, [])
        self.assertEqual(out, [])

    def test_tier_for_uses_supplied_markers(self):
        self.assertEqual(fp.tier_for(["x/billing.py"], ["billing.py"]), "top")
        self.assertEqual(fp.tier_for(["app/api/c/route.ts"], ["/route.ts"]), "top")
        self.assertEqual(fp.tier_for(["features/cell.tsx"], ["billing.py"]), "cheap")
        self.assertEqual(fp.tier_for(["anything.py"], []), "cheap")

    def test_build_file_coupling_ignores_same_file_edges(self):
        nodes = {"n1": {"source_file": "a.py"}, "n2": {"source_file": "a.py"},
                 "n3": {"source_file": "b.py"}}
        links = [{"source": "n1", "target": "n2"}, {"source": "n1", "target": "n3"}]
        adj = fp.build_file_coupling(nodes, links)
        self.assertEqual(adj["a.py"], {"b.py"})
        self.assertEqual(adj["b.py"], {"a.py"})


class WavesTests(unittest.TestCase):
    """Waves: ONE GLOBAL schedule over all items. Items in one wave are
    mutually conflict-free (no shared file, no shared contract_group) AND have
    no `after` dependency between them -> run concurrently; each wave starts
    from the integrated result of the waves before it. Clusters stay the
    merge-safety grouping (MSP boundaries); waves are the execution order."""

    def _waves(self, items):
        adj = fp._conflict_adjacency(items)
        order = {it["name"]: i for i, it in enumerate(items)}
        return fp.global_waves(items, adj, order)

    def test_chain_hub_goes_first_then_leaves_fan_out(self):
        # A-B share f1, B-C share f2: B is the hub -> wave 1 = [B], then A, C.
        items = [
            {"name": "A", "files": ["f1.py"]},
            {"name": "B", "files": ["f1.py", "f2.py"]},
            {"name": "C", "files": ["f2.py"]},
        ]
        self.assertEqual(self._waves(items), [["B"], ["A", "C"]])

    def test_clique_degenerates_to_singleton_waves_in_input_order(self):
        items = [
            {"name": "A", "files": ["models.py"]},
            {"name": "B", "files": ["models.py"]},
            {"name": "C", "files": ["models.py"]},
        ]
        self.assertEqual(self._waves(items), [["A"], ["B"], ["C"]])

    def test_contract_group_conflicts_without_shared_files(self):
        items = [
            {"name": "svc", "files": ["services/x.py"], "contract_group": "g"},
            {"name": "view", "files": ["views/y.py"], "contract_group": "g"},
        ]
        self.assertEqual(self._waves(items), [["svc"], ["view"]])

    def test_after_orders_consumers_behind_producer_but_parallel_together(self):
        # The S4 case: BE produces an API field; FE1 + FE2 consume it. All
        # file-disjoint. contract_group would force BE->FE1->FE2 fully serial;
        # `after` must yield [[BE], [FE1, FE2]] - consumers fan out together.
        items = [
            {"name": "BE", "files": ["api/views.py"]},
            {"name": "FE1", "files": ["app/a.tsx"], "after": ["BE"]},
            {"name": "FE2", "files": ["app/b.tsx"], "after": ["BE"]},
        ]
        self.assertEqual(self._waves(items), [["BE"], ["FE1", "FE2"]])

    def test_after_composes_with_conflicts(self):
        # D after C, and D shares a file with E: D must land after C AND never
        # share a wave with E.
        items = [
            {"name": "C", "files": ["c.py"]},
            {"name": "D", "files": ["shared.py"], "after": ["C"]},
            {"name": "E", "files": ["shared.py"]},
        ]
        waves = self._waves(items)
        wave_of = {n: i for i, w in enumerate(waves) for n in w}
        self.assertGreater(wave_of["D"], wave_of["C"])
        self.assertNotEqual(wave_of["D"], wave_of["E"])

    def test_after_unknown_name_raises(self):
        items = [{"name": "A", "files": ["a.py"], "after": ["nope"]}]
        with self.assertRaises(ValueError):
            self._waves(items)

    def test_after_cycle_raises(self):
        items = [
            {"name": "A", "files": ["a.py"], "after": ["B"]},
            {"name": "B", "files": ["b.py"], "after": ["A"]},
        ]
        with self.assertRaises(ValueError):
            self._waves(items)

    def test_plan_emits_msps_and_clusters_not_waves(self):
        import tempfile
        items = [
            {"name": "A", "files": ["f1.py"]},
            {"name": "B", "files": ["f1.py", "f2.py"]},
            {"name": "C", "files": ["f2.py"]},
            {"name": "solo", "files": ["elsewhere.tsx"]},
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as g:
            json.dump({"nodes": [], "links": []}, g)
            graph_path = g.name
        try:
            out = fp.plan(items, graph_path, [])
        finally:
            os.unlink(graph_path)
        self.assertNotIn("waves", out)          # not emitted: ordering is internal
        self.assertNotIn("ready_after", out)
        flat = [n for c in out["clusters"] for n in c]
        self.assertEqual(sorted(flat), sorted(it["name"] for it in items))
        self.assertIn(["solo"], out["clusters"])   # no conflicts -> cluster of one
        # two items sharing a file are never in DIFFERENT clusters
        files = {it["name"]: set(it["files"]) for it in items}
        where = {n: i for i, c in enumerate(out["clusters"]) for n in c}
        for a in files:
            for b in files:
                if a < b and files[a] & files[b]:
                    self.assertEqual(where[a], where[b],
                                     f"{a} and {b} share a file, different clusters")

    def test_plan_works_without_graph_and_drops_decided_pairs(self):
        # --graph is optional: clustering/waves/tiers still compute; and a
        # pair with a declared `after` path is a DECIDED order, so it must
        # not reappear in coupling_review.
        items = [
            {"name": "BE", "files": ["ledger_api.py"]},
            {"name": "FE", "files": ["ledger_ui.tsx"], "after": ["BE"]},
        ]
        out = fp.plan(items, None, ["ledger"], trajectories=[])
        # separate MSPs (no shared file, no contract group), so the chain does
        # NOT fuse - a cluster cannot span two MSPs. It stays a dispatch edge.
        self.assertEqual([sorted(c) for c in out["clusters"]], [["BE"], ["FE"]])
        self.assertEqual(out["cluster_after"], {1: [0]})
        pairs = [set(p["pair"]) for p in out["coupling_review"]]
        self.assertNotIn({"BE", "FE"}, pairs)


class ExecutionCostTests(unittest.TestCase):
    """The additive outputs: they must cut wall-clock/tokens without ever
    loosening the merge-safety guarantee clustering provides."""

    def test_item_cost_prefers_explicit_then_file_count(self):
        self.assertEqual(fp.item_cost({"name": "A", "files": ["a"], "cost": 9}), 9.0)
        self.assertEqual(fp.item_cost({"name": "A", "files": ["a", "b"]}), 2.0)
        self.assertEqual(fp.item_cost({"name": "A", "files": []}), 1.0)
        # a bool is not a cost (True == 1 in Python; it must not be read as one)
        self.assertEqual(fp.item_cost({"name": "A", "files": ["a", "b"], "cost": True}), 2.0)

    def test_downstream_cost_is_the_longest_chain_not_the_local_size(self):
        items = [{"name": "P", "files": ["p.py"], "cost": 1},
                 {"name": "M", "files": ["m.py"], "cost": 1, "after": ["P"]},
                 {"name": "L", "files": ["l.py"], "cost": 1, "after": ["M"]},
                 {"name": "BIG", "files": ["b.py"], "cost": 2}]
        dc = fp.downstream_cost(items, {i["name"]: fp.item_cost(i) for i in items})
        self.assertEqual(dc["P"], 3.0)    # P -> M -> L
        self.assertEqual(dc["BIG"], 2.0)  # big but nothing behind it
        self.assertGreater(dc["P"], dc["BIG"])  # chain beats raw size

    def test_critical_path_orders_the_chain_head_before_a_bigger_leaf(self):
        """Both are wave-0 eligible. HEAD is small but carries a 3-deep chain;
        BIG is a fatter single job with nothing behind it. The longer POLE wins,
        and a chain counts its whole remaining depth - so HEAD dispatches first.
        (Were BIG's own cost > the whole chain, BIG would rightly go first.)"""
        items = [{"name": "BIG", "files": ["b.py"], "cost": 2, "task": "t"},
                 {"name": "HEAD", "files": ["h.py"], "cost": 1, "task": "t"},
                 {"name": "MID", "files": ["m.py"], "after": ["HEAD"], "task": "t"},
                 {"name": "TAIL", "files": ["t.py"], "after": ["MID"], "task": "t"}]
        pl = fp.plan(items, None, [], trajectories=[])
        res = fp.run_plan(pl, items, "true", dry_run=True, branches=False)
        order = [r["cluster"] for r in res["dispatched"]]
        self.assertLess(order.index("HEAD"), order.index("BIG"))

    def test_a_conflict_becomes_one_agent_and_nothing_else_waits(self):
        """A and B collide, so they are ONE cluster walked by one agent - no
        waiting involved. SLOW is unrelated and starts immediately."""
        items = [{"name": "A", "files": ["a.py"]},
                 {"name": "SLOW", "files": ["s.py"]},
                 {"name": "B", "files": ["a.py"]}]  # conflicts with A only
        out = fp.plan(items, None, [], trajectories=[])
        clusters = [sorted(c) for c in out["clusters"]]
        self.assertIn(["A", "B"], clusters)
        self.assertIn(["SLOW"], clusters)
        self.assertEqual(out["cluster_after"], {})   # nothing waits on anything

    def test_declared_producers_become_cluster_edges(self):
        items = [{"name": "BE", "files": ["api.py"]},
                 {"name": "FE1", "files": ["a.tsx"], "after": ["BE"]},
                 {"name": "FE2", "files": ["b.tsx"], "after": ["BE"]}]
        out = fp.plan(items, None, [], trajectories=[])
        clusters, edges = out["clusters"], out["cluster_after"]
        idx = {c[0]: i for i, c in enumerate(clusters)}
        self.assertEqual(edges[idx["FE1"]], [idx["BE"]])
        self.assertEqual(edges[idx["FE2"]], [idx["BE"]])
        self.assertNotIn(idx["FE1"], edges[idx["FE2"]])  # consumers stay parallel

    def test_context_pack_carries_edit_set_and_graph_neighbours(self):
        adj = {"app/svc.py": {"app/models.py"}, "app/models.py": {"app/svc.py"}}
        packs = fp.context_packs([{"name": "A", "files": ["app/svc.py"]}], adj)
        self.assertEqual(packs["A"]["edit"], ["app/svc.py"])
        self.assertIn("app/models.py", packs["A"]["read"])

    def test_context_pack_reports_truncation_instead_of_hiding_it(self):
        adj = {"hub.py": {"n%d.py" % i for i in range(10)}}
        packs = fp.context_packs([{"name": "A", "files": ["hub.py"]}], adj, cap=3)
        self.assertEqual(len(packs["A"]["read"]), 3)
        self.assertEqual(packs["A"]["read_truncated"], 7)

    def test_context_pack_works_without_a_graph(self):
        packs = fp.context_packs([{"name": "A", "files": ["a.py"]}], {})
        self.assertEqual(packs["A"], {"edit": ["a.py"]})

    def test_shared_context_lists_only_files_two_items_would_both_read(self):
        packs = {"A": {"edit": ["a.py"], "read": ["shared.py", "onlyA.py"]},
                 "B": {"edit": ["b.py"], "read": ["shared.py"]}}
        out = fp.shared_context(packs, [["A", "B"]])
        self.assertEqual(out["0"], ["shared.py"])

    def test_coalesce_groups_small_cheap_items_and_never_top_tier(self):
        items = [{"name": n, "files": ["%s.py" % n]} for n in ("a", "b", "c")]
        items.append({"name": "risky", "files": ["auth/x.py"]})
        waves = [["a", "b", "c", "risky"]]
        tier = {"a": "cheap", "b": "cheap", "c": "cheap", "risky": "top"}
        cost = {n: 1.0 for n in ("a", "b", "c", "risky")}
        out = fp.coalesce_groups(items, waves, tier, cost)
        self.assertEqual(out["0"], [["a", "b", "c"]])
        self.assertNotIn("risky", [n for g in out["0"] for n in g])

    def test_coalesce_skips_a_lone_item_and_respects_max_size(self):
        waves = [["a", "b", "c"]]
        tier = {n: "cheap" for n in "abc"}
        cost = {n: 1.0 for n in "abc"}
        self.assertEqual(fp.coalesce_groups([], waves, tier, cost, max_size=2)["0"],
                         [["a", "b"]])  # the odd one out is left to run alone
        self.assertEqual(fp.coalesce_groups([], [["a"]], tier, cost), {})

    def test_coalesce_never_merges_across_different_predecessors(self):
        """Merging items with different deps would make the group wait on the
        UNION - the barrier ready_after exists to remove."""
        waves = [["a", "b"]]
        tier = {"a": "cheap", "b": "cheap"}
        cost = {"a": 1.0, "b": 1.0}
        deps = {"a": ["P1"], "b": ["P2"]}
        self.assertEqual(fp.coalesce_groups([], waves, tier, cost, deps=deps), {})
        same = {"a": ["P1"], "b": ["P1"]}
        self.assertEqual(fp.coalesce_groups([], waves, tier, cost, deps=same)["0"],
                         [["a", "b"]])

    def test_verdict_groups_collapse_identical_signals(self):
        review = [{"pair": ["a", "b"], "signals": ["shared-risk-marker:auth"], "default": "serialize"},
                  {"pair": ["c", "d"], "signals": ["shared-risk-marker:auth"], "default": "serialize"},
                  {"pair": ["e", "f"], "signals": ["import-adjacent"], "default": "parallel"}]
        groups = fp.verdict_groups(review)
        self.assertEqual(len(groups), 2)          # 3 pairs -> 2 renderings
        self.assertEqual(groups[0]["count"], 2)   # biggest bucket first
        self.assertEqual(sum(g["count"] for g in groups), 3)  # no pair dropped

    def test_verify_mode_splits_serial_surfaces_from_offloadable(self):
        items = [{"name": "ui", "files": ["src/pages/Admin.tsx"]},
                 {"name": "calc", "files": ["app/services/total.py"]}]
        modes = fp.verify_modes(items, ["src/pages/"])
        self.assertEqual(modes["ui"], "serial")
        self.assertEqual(modes["calc"], "offload")

    def test_new_outputs_do_not_change_clustering_or_tiers(self):
        """The safety guarantee is unchanged: same clusters, same tiers."""
        items = [{"name": "A", "files": ["x.py"]},
                 {"name": "B", "files": ["x.py"]},
                 {"name": "C", "files": ["auth/y.py"]}]
        out = fp.plan(items, None, ["auth/"])
        self.assertEqual(sorted(sorted(c) for c in out["clusters"]),
                         [["A", "B"], ["C"]])
        self.assertEqual(out["tier"], {"A": "cheap", "B": "cheap", "C": "top"})

    def test_empty_sections_are_omitted_not_padded(self):
        out = fp.plan([{"name": "A", "files": ["a.py"]}], None, [])
        for key in ("verdict_groups", "shared_context", "coalesce"):
            self.assertNotIn(key, out)


class RunnerTests(unittest.TestCase):
    """The runner turns a plan from advice into execution. Its job is DISPATCH
    (order, concurrency, dependencies) - never a claim about the work's quality."""

    ITEMS = [{"name": "P", "files": ["p.py"], "task": "build the endpoint"},
             {"name": "C1", "files": ["c1.tsx"], "after": ["P"]},
             {"name": "C2", "files": ["c2.tsx"], "after": ["P"]}]

    def test_build_argv_cannot_be_reinterpreted_as_shell(self):
        """A prompt full of metacharacters must stay ONE argument."""
        nasty = 'fix "it"; rm -rf /; `whoami` $(id) && echo\nnewline'
        argv = fp.build_argv("agent exec {prompt}", nasty, {"name": "A", "files": []})
        self.assertEqual(argv[:2], ["agent", "exec"])
        self.assertEqual(argv[2], nasty)   # intact, and a single argv element
        self.assertEqual(len(argv), 3)     # nothing split into extra tokens

    def test_argv_carries_the_tier_so_the_spawn_can_route_on_it(self):
        """Tiering saves nothing unless something ROUTES on it, and the routing knob
        is the command line. Before this, {tier} reached the prompt only - the agent
        could read about its tier and not be selected by it."""
        argv = fp.build_argv("agent --model {tier} -p {prompt}", "do it",
                             {"name": "A", "files": []}, tier="cheap")
        self.assertEqual(argv, ["agent", "--model", "cheap", "-p", "do it"])

    def test_argv_carries_cluster_and_msp(self):
        argv = fp.build_argv("agent --tag {cluster}/{msp} -p {prompt}", "x",
                             {"name": "A", "files": []}, cluster="c3", msp=2)
        self.assertIn("c3/2", argv)

    def test_unsupplied_placeholders_collapse_to_empty_not_literal(self):
        """A template asking for a tier that was never supplied must not pass the
        literal string '{tier}' to the agent as if it were a model name."""
        argv = fp.build_argv("agent --model {tier} -p {prompt}", "x",
                             {"name": "A", "files": []})
        self.assertNotIn("{tier}", argv)

    def test_tier_substitution_cannot_inject_extra_argv(self):
        """The same guarantee the prompt has: a value lands as ONE token."""
        argv = fp.build_argv("agent --model {tier} -p {prompt}", "x",
                             {"name": "A", "files": []},
                             tier="a b; rm -rf /")
        self.assertEqual(argv[2], "a b; rm -rf /")
        self.assertEqual(len(argv), 5)

    def test_render_prompt_is_self_contained(self):
        """A spawned process inherits no conversation."""
        pack = {"edit": ["a.py"], "read": ["b.py"]}
        out = fp.render_prompt(self.ITEMS[0], pack, "top")
        self.assertIn("build the endpoint", out)
        self.assertIn("a.py", out)
        self.assertIn("b.py", out)
        self.assertIn("disjoint", out)  # warns against straying outside its files

    def test_dry_run_dispatches_producers_before_consumers(self):
        plan_obj = fp.plan(self.ITEMS, None, [])
        res = fp.run_plan(plan_obj, self.ITEMS, "true {prompt}", dry_run=True)
        order = [r["item"] for r in res["dispatched"]]
        self.assertLess(order.index("P"), order.index("C1"))
        self.assertLess(order.index("P"), order.index("C2"))
        self.assertEqual(res["summary"]["dry-run"], 3)

    def test_failure_blocks_dependents_but_not_independent_work(self):
        items = self.ITEMS + [{"name": "SOLO", "files": ["s.py"]}]
        plan_obj = fp.plan(items, None, [])
        # `false` exits non-zero for every item; SOLO still gets its own attempt
        res = fp.run_plan(plan_obj, items, "false", concurrency=2)
        by_item = {r["item"]: r for r in res["dispatched"]}
        self.assertEqual(by_item["P"]["status"], "failed")
        self.assertEqual(by_item["C1"]["status"], "blocked")
        self.assertEqual(by_item["C2"]["status"], "blocked")
        self.assertEqual(by_item["SOLO"]["status"], "failed")  # attempted, not skipped
        self.assertNotIn("ok", res["summary"])

    def test_successful_run_marks_every_item_ok(self):
        plan_obj = fp.plan(self.ITEMS, None, [])
        res = fp.run_plan(plan_obj, self.ITEMS, "true", concurrency=3)
        self.assertEqual(res["summary"]["ok"], 3)
        self.assertTrue(all(r["exit"] == 0 for r in res["dispatched"]))

    def test_run_log_ties_back_to_the_plan_it_executed(self):
        plan_obj = fp.plan(self.ITEMS, None, [])
        res = fp.run_plan(plan_obj, self.ITEMS, "true", concurrency=3)
        self.assertEqual(res["plan_id"], plan_obj["plan_id"])

    def test_plan_id_is_stable_and_changes_with_the_plan(self):
        a = fp.plan(self.ITEMS, None, [])
        self.assertEqual(a["plan_id"], fp.plan(self.ITEMS, None, [])["plan_id"])
        b = fp.plan(self.ITEMS, None, ["p.py"])  # different tiers -> different plan
        self.assertNotEqual(a["plan_id"], b["plan_id"])


class BoundedReturnAndStateTests(unittest.TestCase):
    """Two levers against caller-context bloat: workers return a SMALL fixed
    shape, and the run's state lives on DISK so nobody has to hold it."""

    ITEMS = [{"name": "A", "files": ["a.py"]},
             {"name": "B", "files": ["b.py"], "after": ["A"]}]

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_prompt_carries_the_return_contract(self):
        out = fp.render_prompt({"name": "A", "files": ["a.py"]}, {}, "cheap")
        self.assertIn("ONE line of JSON", out)
        self.assertIn('"files_changed"', out)

    def test_contract_can_be_disabled(self):
        out = fp.render_prompt({"name": "A", "files": ["a.py"]}, {}, "cheap", contract=None)
        self.assertNotIn("ONE line of JSON", out)

    def test_parse_return_takes_the_last_json_line_and_ignores_narration(self):
        text = ('Thinking about it...\nI edited some files.\n'
                '{"item":"A","status":"ok","files_changed":["a.py"]}')
        self.assertEqual(fp.parse_return(text)["item"], "A")
        self.assertIsNone(fp.parse_return("no json at all"))
        self.assertIsNone(fp.parse_return('{"broken": '))

    def test_worker_output_goes_to_disk_and_the_record_stays_small(self):
        plan_obj = fp.plan(self.ITEMS, None, [])
        noisy = 'printf "%s\\n" $(seq 1 500); printf \'{"item":"x","status":"ok"}\\n\''
        res = fp.run_plan(plan_obj, self.ITEMS, "bash -c %s" % shlex.quote(noisy),
                          concurrency=2, run_dir=self.dir)
        rec = next(r for r in res["dispatched"] if r["item"] == "A")
        self.assertEqual(rec["returned"]["status"], "ok")   # structured half kept
        self.assertNotIn("output_tail", rec)                # bulk NOT in the record
        self.assertTrue(os.path.exists(rec["output_file"])) # bulk IS on disk
        self.assertGreater(os.path.getsize(rec["output_file"]), 1000)

    def test_unstructured_output_is_truncated_not_dropped(self):
        plan_obj = fp.plan([self.ITEMS[0]], None, [])
        res = fp.run_plan(plan_obj, [self.ITEMS[0]],
                          "bash -c %s" % shlex.quote('printf "%s\\n" $(seq 1 900)'),
                          run_dir=self.dir, max_output=200)
        rec = res["dispatched"][0]
        self.assertLessEqual(len(rec["output_tail"]), 200)
        self.assertTrue(os.path.exists(rec["output_file"]))

    def test_state_is_written_to_disk_with_the_plan(self):
        plan_obj = fp.plan(self.ITEMS, None, [])
        fp.run_plan(plan_obj, self.ITEMS, "true", run_dir=self.dir)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "plan.json")))
        state = fp.load_state(self.dir)
        self.assertEqual(state["plan_id"], plan_obj["plan_id"])
        self.assertEqual(state["items"]["A"]["status"], "ok")

    def test_resume_skips_completed_work(self):
        plan_obj = fp.plan(self.ITEMS, None, [])
        fp.run_plan(plan_obj, self.ITEMS, "true", run_dir=self.dir)
        # `false` would fail everything - anything already ok must not be re-run
        again = fp.run_plan(plan_obj, self.ITEMS, "false", run_dir=self.dir, resume=True)
        self.assertEqual(again["summary"].get("resumed"), 2)
        self.assertNotIn("failed", again["summary"])

    def test_resume_refuses_a_different_plan(self):
        """Prior results describe a different work-set; reusing them would report
        work as done that this plan never ran."""
        fp.run_plan(fp.plan(self.ITEMS, None, []), self.ITEMS, "true", run_dir=self.dir)
        changed = [{"name": "A", "files": ["a.py"]}, {"name": "C", "files": ["c.py"]}]
        with self.assertRaises(ValueError):
            fp.run_plan(fp.plan(changed, None, []), changed, "true",
                        run_dir=self.dir, resume=True)

    def test_worker_is_told_which_item_it_is(self):
        """A script worker must not have to parse the prompt to identify itself."""
        plan_obj = fp.plan([self.ITEMS[0]], None, [])
        res = fp.run_plan(plan_obj, [self.ITEMS[0]],
                          "bash -c %s" % shlex.quote(
                              'printf \'{"item":"%s","status":"ok"}\\n\' "$FANOUT_ITEM"'),
                          run_dir=self.dir)
        self.assertEqual(res["dispatched"][0]["returned"]["item"], "A")

    def test_item_name_cannot_escape_the_run_directory(self):
        self.assertNotIn("/", fp._safe("../../etc/passwd"))
        self.assertTrue(fp._safe("../../etc/passwd"))




class V2Planner(unittest.TestCase):
    """MSPs vs clusters, the two-axis tier, and reconcile."""

    def test_msp_fuses_on_shared_file_cluster_follows(self):
        items = [{"name": "a", "files": ["x.py"]}, {"name": "b", "files": ["x.py"]}]
        self.assertEqual([sorted(g) for g in fp.msp_items(items)], [["a", "b"]])
        self.assertEqual([sorted(c) for c in fp.cluster_items(items)], [["a", "b"]])

    def test_disjoint_items_are_clusters_of_one(self):
        items = [{"name": "a", "files": ["x.py"]}, {"name": "b", "files": ["y.py"]}]
        self.assertEqual(sorted(len(c) for c in fp.cluster_items(items)), [1, 1])

    def test_pinned_contract_splits_into_parallel_clusters(self):
        """One MSP (contract_group), three clusters: the pin exists so the two
        halves run at once."""
        items = [
            {"name": "iface", "files": ["schema.json"], "type": "contract",
             "contract_group": "login"},
            {"name": "be", "files": ["api.py"], "contract_group": "login",
             "after": ["iface"]},
            {"name": "fe", "files": ["ui.tsx"], "contract_group": "login",
             "after": ["iface"]},
        ]
        self.assertEqual(len(fp.msp_items(items)), 1)
        clusters = fp.cluster_items(items)
        self.assertEqual(len(clusters), 3, clusters)
        # be and fe both wait on iface's cluster, and on nothing else
        edges = fp.cluster_after(items, clusters)
        self.assertEqual(len(edges), 2)
        self.assertTrue(all(len(v) == 1 for v in edges.values()))

    def test_unpinned_contract_serializes_under_one_owner(self):
        items = [
            {"name": "be", "files": ["api.py"], "contract_group": "login"},
            {"name": "fe", "files": ["ui.tsx"], "contract_group": "login"},
        ]
        self.assertEqual([sorted(c) for c in fp.cluster_items(items)], [["be", "fe"]])

    def test_cross_msp_chain_does_not_fuse(self):
        """A cluster cannot span two MSPs, so even a plain a->b chain in two
        MSPs stays two clusters joined by a dispatch edge."""
        chain = [{"name": "a", "files": ["x.py"]},
                 {"name": "b", "files": ["y.py"], "after": ["a"]}]
        clusters = fp.cluster_items(chain)
        self.assertEqual(len(clusters), 2)
        self.assertEqual(len(fp.cluster_after(chain, clusters)), 1)

    def test_single_consumer_pin_fuses_but_branching_pin_does_not(self):
        """Pinning buys parallelism only when there are two halves to run at
        once. With one consumer there is nothing to parallelize, so the chain
        link fuses; with two, the halves stay separate."""
        one = [
            {"name": "iface", "files": ["s.json"], "type": "contract",
             "contract_group": "g"},
            {"name": "be", "files": ["api.py"], "contract_group": "g",
             "after": ["iface"]},
        ]
        self.assertEqual(len(fp.cluster_items(one)), 1)
        two = one + [{"name": "fe", "files": ["ui.tsx"], "contract_group": "g",
                      "after": ["iface"]}]
        self.assertEqual(len(fp.cluster_items(two)), 3)

    def test_cross_msp_edge_is_carried_by_the_cluster_not_the_msp(self):
        """One of B's leaves depends on A; B's other leaf must not wait."""
        items = [
            {"name": "a", "files": ["a.py"]},
            {"name": "b_dep", "files": ["b1.py", "shared.py"], "after": ["a"]},
            {"name": "b_free", "files": ["b2.py", "shared.py"]},
        ]
        clusters = fp.cluster_items(items)
        edges = fp.cluster_after(items, clusters)
        waiting = [i for i in range(len(clusters)) if i in edges]
        self.assertEqual(len(waiting), 1)
        # the waiting cluster is the one holding b_dep; a's cluster is free
        self.assertIn("b_dep", clusters[waiting[0]])

    def test_tier_two_axes(self):
        self.assertEqual(fp.tier_for(["api/auth/x.py"], ["auth"], "simple"), "top")
        self.assertEqual(fp.tier_for(["web/copy.ts"], ["auth"], "complex"), "top")
        self.assertEqual(fp.tier_for(["web/copy.ts"], ["auth"], "simple"), "cheap")

    def test_tier_without_complexity_is_unchanged(self):
        self.assertEqual(fp.tier_for(["web/copy.ts"], ["auth"]), "cheap")
        self.assertEqual(fp.tier_for(["api/auth/x.py"], ["auth"]), "top")

    def test_reconcile_flags_only_cross_msp_misses(self):
        items = [{"name": "a", "files": ["a.py"]}, {"name": "b", "files": ["b.py"]}]
        clusters = fp.cluster_items(items)
        noise = fp.reconcile(items, clusters, {"a": ["a.py", "a_helper.py"]})
        self.assertTrue(noise["clean"])
        self.assertEqual(noise["collisions"], 0)
        hit = fp.reconcile(items, clusters, {"a": ["a.py", "b.py"]})
        self.assertFalse(hit["clean"])
        self.assertEqual(hit["collisions"], 1)
        self.assertEqual(hit["findings"][0]["severity"], "collision")

    def test_plan_emits_the_v2_keys(self):
        items = [{"name": "a", "files": ["a.py"]}, {"name": "b", "files": ["b.py"]}]
        pl = fp.plan(items, None, ["auth"], trajectories=[])
        for key in ("msps", "clusters", "plan_id", "tier"):
            self.assertIn(key, pl)


class MarkerMatching(unittest.TestCase):
    """Substring matching was wrong in both directions on the same run."""

    def test_marker_does_not_fire_inside_a_longer_word(self):
        self.assertFalse(fp.marker_matches(
            "app/(unauthenticated)/forgotPassword/page.tsx", "auth"))

    def test_marker_fires_on_a_camelcase_word(self):
        self.assertTrue(fp.marker_matches("app/types/AuthResponse.ts", "auth"))

    def test_marker_fires_on_a_path_segment(self):
        self.assertTrue(fp.marker_matches("app/auth/api/route.ts", "auth"))

    def test_marker_is_case_insensitive_both_ways(self):
        self.assertTrue(fp.marker_matches("app/AUTH/x.ts", "auth"))
        self.assertTrue(fp.marker_matches("app/auth/x.ts", "AUTH"))

    def test_path_shaped_marker_keeps_substring_behaviour(self):
        self.assertTrue(fp.marker_matches("src/api/auth/login.py", "api/auth"))
        self.assertFalse(fp.marker_matches("src/api/users/x.py", "api/auth"))
        self.assertTrue(fp.marker_matches("db/0001_init.sql", ".sql"))

    def test_tier_uses_it(self):
        self.assertEqual(
            fp.tier_for(["app/(unauthenticated)/forgotPassword/page.tsx"],
                        ["auth"]), "cheap")
        self.assertEqual(fp.tier_for(["app/types/AuthResponse.ts"], ["auth"]),
                         "top")


class ClusterDispatch(unittest.TestCase):
    """One agent per cluster; dependents unlock as producers finish."""

    def _plan(self, items):
        return fp.plan(items, None, [], trajectories=[])

    def test_one_dispatch_per_cluster_not_per_item(self):
        items = [{"name": "a", "files": ["x.py"], "task": "do a"},
                 {"name": "b", "files": ["x.py"], "task": "do b"},
                 {"name": "c", "files": ["y.py"], "task": "do c"}]
        pl = self._plan(items)
        res = fp.run_plan(pl, items, "true", dry_run=True)
        self.assertEqual(len(res["dispatched"]), 2)   # a+b share x.py -> one agent

    def test_cluster_prompt_lists_every_leaf_with_its_files(self):
        items = [{"name": "a", "files": ["x.py"], "task": "do a"},
                 {"name": "b", "files": ["x.py"], "task": "do b"}]
        pl = self._plan(items)
        prompt = fp.render_cluster_prompt(
            pl["clusters"][0], {i["name"]: i for i in items},
            pl["context_packs"], "cheap", [], None, fp.RETURN_CONTRACT)
        self.assertIn("do a", prompt)
        self.assertIn("do b", prompt)
        self.assertIn("x.py", prompt)
        self.assertIn("ORDER", prompt)          # multi-leaf ordering instruction

    def test_file_notes_reach_the_agent(self):
        items = [{"name": "a", "files": ["x.py", "y.py"], "task": "do a",
                  "file_notes": {"x.py": "add the endpoint", "z.py": "not mine"}}]
        prompt = fp.render_cluster_prompt(
            ["a"], {i["name"]: i for i in items}, {}, "cheap", [], None, None)
        self.assertIn("add the endpoint", prompt)
        self.assertNotIn("not mine", prompt)     # notes outside `edit` are dropped

    def test_dependent_cluster_waits_and_siblings_do_not(self):
        items = [{"name": "a", "files": ["a.py"], "task": "t"},
                 {"name": "b", "files": ["b.py"], "task": "t", "after": ["a"]},
                 {"name": "free", "files": ["c.py"], "task": "t"}]
        pl = self._plan(items)
        res = fp.run_plan(pl, items, "true", dry_run=True)
        order = [r["cluster"] for r in res["dispatched"]]
        self.assertLess(order.index("a"), order.index("b"))
        self.assertEqual(len(order), 3)

    def test_failed_cluster_blocks_only_its_dependents(self):
        items = [{"name": "a", "files": ["a.py"], "task": "t"},
                 {"name": "b", "files": ["b.py"], "task": "t", "after": ["a"]},
                 {"name": "free", "files": ["c.py"], "task": "t"}]
        pl = self._plan(items)
        res = fp.run_plan(pl, items, "false")     # every worker exits non-zero
        by = {r["cluster"]: r for r in res["dispatched"]}
        self.assertEqual(by["a"]["status"], "failed")
        self.assertEqual(by["b"]["status"], "blocked")
        self.assertEqual(by["free"]["status"], "failed")   # ran, did not wait

    def test_cluster_tier_is_the_riskiest_leaf(self):
        """One agent does every leaf in the cluster, so the cluster runs at the
        tier of its riskiest member - not the average, and not the first."""
        items = [{"name": "safe", "files": ["copy.ts"], "task": "t"},
                 {"name": "risky", "files": ["copy.ts", "api/auth/x.py"],
                  "task": "t"}]
        pl = fp.plan(items, None, ["auth"], trajectories=[])
        self.assertEqual(pl["tier"]["safe"], "cheap")
        res = fp.run_plan(pl, items, "true", dry_run=True)
        self.assertEqual(len(res["dispatched"]), 1)
        self.assertEqual(res["dispatched"][0]["tier"], "top")

    def test_underspecified_items_are_named(self):
        items = [{"name": "a", "files": ["x.py"]},
                 {"name": "b", "files": ["y.py"], "task": "do b"}]
        pl = self._plan(items)
        self.assertEqual(pl["underspecified"], ["a"])

    def test_multi_leaf_returns_are_all_kept(self):
        self.assertEqual(len(fp.parse_returns('{"a":1}\nnoise\n{"b":2}')), 2)


class Recon(unittest.TestCase):
    """Deriving items from plain asks - the step that makes fanout usable
    without the caller doing the hard part first."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.stub = os.path.join(self.tmp, "stub.py")
        with open(self.stub, "w") as f:
            f.write(
                "import sys, json\n"
                "ask = sys.argv[1]\n"
                "if 'UNANSWERABLE' in ask:\n"
                "    print('I could not work that out'); raise SystemExit(0)\n"
                "print(json.dumps({'items': [\n"
                "  {'name': 'be', 'task': 'build the endpoint',\n"
                "   'files': ['api/x.py'], 'complexity': 'complex',\n"
                "   'acceptance': 'returns 200'},\n"
                "  {'name': 'fe', 'task': 'build the form',\n"
                "   'files': ['web/x.tsx'], 'complexity': 'simple'}]}))\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _exec(self):
        return "%s %s {prompt}" % (shlex.quote(sys.executable),
                                   shlex.quote(self.stub))

    def test_one_ask_becomes_several_items(self):
        items, report = fp.run_recon(["add a login form"], self._exec())
        self.assertEqual(sorted(i["name"] for i in items), ["be", "fe"])
        self.assertEqual(report[0]["items"], ["be", "fe"])

    def test_derived_items_carry_the_brief_into_the_plan(self):
        items, _ = fp.run_recon(["add a login form"], self._exec())
        pl = fp.plan(items, None, [], trajectories=[])
        self.assertNotIn("underspecified", pl)          # recon supplied tasks
        self.assertEqual(pl["tier"]["be"], "top")       # complexity axis applied
        self.assertEqual(pl["tier"]["fe"], "cheap")
        prompt = fp.render_cluster_prompt(
            ["be"], {i["name"]: i for i in items}, pl["context_packs"],
            "top", [], None, None)
        self.assertIn("build the endpoint", prompt)
        self.assertIn("returns 200", prompt)

    def test_name_collisions_across_asks_are_kept_distinct(self):
        items, _ = fp.run_recon(["one", "two"], self._exec(), concurrency=2)
        self.assertEqual(len(items), 4)
        self.assertEqual(len(set(i["name"] for i in items)), 4)

    def test_an_ask_that_yields_nothing_is_reported_not_dropped(self):
        items, report = fp.run_recon(["UNANSWERABLE"], self._exec())
        self.assertEqual(items, [])
        self.assertIn("error", report[0])

    def test_graph_is_named_in_the_brief_when_present(self):
        g = os.path.join(self.tmp, "graph.json")
        open(g, "w").write("{}")
        with_graph = fp.render_recon_prompt("x", g)
        self.assertIn(g, with_graph)
        self.assertIn("query it FIRST", with_graph)
        without = fp.render_recon_prompt("x", None)
        self.assertIn("No graphify graph", without)
        self.assertIn("grep", without)


class ClusterBriefs(unittest.TestCase):
    """The plan carries what a subagent must be told, so an orchestrator that
    dispatches its own agents sends the same thing --exec would."""

    def test_plan_carries_a_brief_per_cluster(self):
        items = [{"name": "a", "files": ["x.py"], "task": "do a",
                  "acceptance": "a works"},
                 {"name": "b", "files": ["y.py"], "task": "do b",
                  "after": ["a"]}]
        pl = fp.plan(items, None, [], trajectories=[])
        briefs = pl["cluster_briefs"]
        self.assertEqual(len(briefs), len(pl["clusters"]))
        for b in briefs:
            self.assertTrue(b["prompt"])
            self.assertIn("msp", b)
            self.assertIn(b["tier"], ("top", "cheap"))
        a = next(b for b in briefs if b["leaves"] == ["a"])
        self.assertIn("do a", a["prompt"])
        self.assertIn("a works", a["prompt"])
        b = next(x for x in briefs if x["leaves"] == ["b"])
        self.assertEqual(b["after_clusters"], [a["cluster"]])

    def test_exec_sends_the_plans_brief_verbatim(self):
        items = [{"name": "a", "files": ["x.py"], "task": "do a"}]
        pl = fp.plan(items, None, [], trajectories=[])
        res = fp.run_plan(pl, items, "true {prompt}", dry_run=True)
        sent = res["dispatched"][0]["argv"][-1]
        self.assertEqual(sent, pl["cluster_briefs"][0]["prompt"])

    def test_briefs_can_be_switched_off(self):
        items = [{"name": "a", "files": ["x.py"], "task": "do a"}]
        self.assertIn("cluster_briefs", fp.plan(items, None, [], trajectories=[]))
        off = fp.plan(items, None, [], trajectories=[], briefs=False)
        self.assertNotIn("cluster_briefs", off)
        # and --exec still works without them, by rendering its own
        res = fp.run_plan(off, items, "true {prompt}", dry_run=True)
        self.assertIn("do a", res["dispatched"][0]["argv"][-1])

    def test_brief_names_the_msp_each_cluster_belongs_to(self):
        items = [{"name": "iface", "files": ["s.json"], "type": "contract",
                  "contract_group": "g", "task": "t"},
                 {"name": "be", "files": ["api.py"], "contract_group": "g",
                  "after": ["iface"], "task": "t"},
                 {"name": "fe", "files": ["ui.tsx"], "contract_group": "g",
                  "after": ["iface"], "task": "t"}]
        pl = fp.plan(items, None, [], trajectories=[])
        msps = {b["msp"] for b in pl["cluster_briefs"]}
        self.assertEqual(len(msps), 1)        # three clusters, one MSP, one PR
        self.assertEqual(len(pl["cluster_briefs"]), 3)


class BranchPerMSP(unittest.TestCase):
    """One branch per MSP is what "one MSP = one PR" means, so --exec does it
    without being asked."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self._g(["init", "-q", "-b", "main", "."])
        for path in ("api/x.py", "web/y.tsx", "docs/d.md"):
            full = os.path.join(self.repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            open(full, "w").write("seed\n")
        self._g(["add", "-A"])
        self._g(["-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "init"])
        self.worker = os.path.join(self.repo, "w.py")
        open(self.worker, "w").write(
            "import os, json, pathlib\n"
            "for f in os.environ.get('FANOUT_FILES','').split(','):\n"
            "    if f:\n"
            "        p = pathlib.Path(f); p.parent.mkdir(parents=True, exist_ok=True)\n"
            "        p.write_text('edited\\n')\n"
            "print(json.dumps({'item': os.environ['FANOUT_CLUSTER'],"
            " 'status':'ok', 'files_changed': [], 'notes':'d'}))\n")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def _g(self, args):
        return subprocess.run(["git"] + args, cwd=self.repo,
                              capture_output=True, text=True)

    def _run(self, items, **kw):
        pl = fp.plan(items, None, [], trajectories=[])
        return pl, fp.run_plan(pl, items, "%s %s" % (sys.executable, self.worker),
                               cwd=self.repo, push_remote=None, **kw)

    def test_each_msp_gets_its_own_branch_and_worktree(self):
        items = [{"name": "a", "files": ["api/x.py"], "task": "t"},
                 {"name": "b", "files": ["web/y.tsx"], "task": "t"}]
        _, res = self._run(items)
        branches = self._g(["branch", "--list", "fanout/*"]).stdout
        self.assertIn("fanout/a", branches)
        self.assertIn("fanout/b", branches)
        self.assertEqual(len(res["msps"]), 2)
        for m in res["msps"]:
            self.assertEqual(m["status"], "committed")

    def test_clusters_of_one_msp_share_that_msps_tree(self):
        items = [{"name": "a", "files": ["api/x.py"], "task": "t"},
                 {"name": "b", "files": ["docs/d.md"], "task": "t", "msp": "bundle"},
                 {"name": "c", "files": ["api/x.py"], "task": "t", "msp": "bundle"}]
        pl, res = self._run(items)
        self.assertEqual(len(pl["msps"]), 1)          # `msp` field fused them
        self.assertEqual(len(res["msps"]), 1)         # one branch, one PR
        self.assertGreater(len(pl["clusters"]), 1)    # still parallel inside

    def test_a_failed_cluster_stops_its_msp_from_opening_a_pr(self):
        items = [{"name": "a", "files": ["api/x.py"], "task": "t"},
                 {"name": "b", "files": ["web/y.tsx"], "task": "t"}]
        pl = fp.plan(items, None, [], trajectories=[])
        res = fp.run_plan(pl, items, "false", cwd=self.repo, push_remote=None)
        self.assertEqual(res.get("msps", []), [])     # nothing committed
        self.assertEqual(res["summary"].get("failed"), 2)

    def test_a_consumer_sees_its_producers_committed_work(self):
        """Waiting is not enough. A cross-MSP consumer works in a DIFFERENT
        tree, so unless the producer's commit is brought in it builds against
        the base revision and the dependency is honoured in timing only."""
        probe = os.path.join(self.repo, "probe.py")
        open(probe, "w").write(
            "import os, json, pathlib\n"
            "c = os.environ['FANOUT_CLUSTER']\n"
            "if c == 'consumer':\n"
            "    pathlib.Path('seen.txt').write_text(pathlib.Path('api/x.py').read_text())\n"
            "for f in os.environ.get('FANOUT_FILES','').split(','):\n"
            "    if f:\n"
            "        p = pathlib.Path(f); p.parent.mkdir(parents=True, exist_ok=True)\n"
            "        p.write_text('by ' + c + chr(10))\n"
            "print(json.dumps({'item': c, 'status':'ok', 'files_changed': [],"
            " 'notes':'d'}))\n")
        items = [{"name": "producer", "files": ["api/x.py"], "task": "t"},
                 {"name": "consumer", "files": ["web/y.tsx"], "task": "t",
                  "after": ["producer"]}]
        pl = fp.plan(items, None, [], trajectories=[])
        fp.run_plan(pl, items, "%s %s" % (sys.executable, probe),
                    cwd=self.repo, push_remote=None)
        seen = os.path.join(self.repo, ".fanout", "trees", "consumer", "seen.txt")
        self.assertTrue(os.path.exists(seen), "consumer never ran")
        self.assertEqual(open(seen).read().strip(), "by producer")

    def test_rerun_reuses_an_existing_branch(self):
        """A second run, or a --resume after the tree was cleaned up, finds the
        branch already there. `worktree add -b` errors on that, and refusing to
        start is wrong when the work is sitting on that branch."""
        self._g(["branch", "fanout/a"])
        trees = fp.prepare_worktrees([["a"]], "main",
                                     os.path.join(self.repo, ".fanout", "t"),
                                     repo=self.repo)
        self.assertTrue(os.path.isdir(trees[0]["path"]))
        self.assertEqual(trees[0]["branch"], "fanout/a")

    def test_no_branches_escape_hatch_uses_one_tree(self):
        items = [{"name": "a", "files": ["api/x.py"], "task": "t"}]
        _, res = self._run(items, branches=False)
        self.assertNotIn("msps", res)
        self.assertEqual(self._g(["branch", "--list", "fanout/*"]).stdout.strip(), "")

    def test_declared_msp_can_only_merge_never_split(self):
        """Two leaves sharing a file stay one MSP even when recon labels them
        differently - a wrong label must not be able to break merge safety."""
        items = [{"name": "a", "files": ["api/x.py"], "msp": "one"},
                 {"name": "b", "files": ["api/x.py"], "msp": "two"}]
        self.assertEqual(len(fp.msp_items(items)), 1)


class TierRouting(unittest.TestCase):
    """Tiering saves nothing unless the SPAWN COMMAND routes on it."""

    def _items(self):
        return [{"name": "risky", "files": ["api/auth/x.py"], "task": "t"},
                {"name": "trivial", "files": ["web/copy.ts"], "task": "t"}]

    def test_model_placeholder_resolves_per_cluster(self):
        items = self._items()
        pl = fp.plan(items, None, ["auth"], trajectories=[])
        res = fp.run_plan(pl, items, "agent --model {model} -p {prompt}",
                          dry_run=True, branches=False,
                          tier_models={"top": "opus", "cheap": "sonnet"})
        got = {r["cluster"]: r["argv"][r["argv"].index("--model") + 1]
               for r in res["dispatched"]}
        self.assertEqual(got["risky"], "opus")
        self.assertEqual(got["trivial"], "sonnet")

    def test_tier_placeholder_is_populated_not_blank(self):
        items = self._items()
        pl = fp.plan(items, None, ["auth"], trajectories=[])
        res = fp.run_plan(pl, items, "agent --tier {tier} -p {prompt}",
                          dry_run=True, branches=False)
        tiers = {r["cluster"]: r["argv"][r["argv"].index("--tier") + 1]
                 for r in res["dispatched"]}
        self.assertEqual(tiers["risky"], "top")
        self.assertEqual(tiers["trivial"], "cheap")

    def test_unmapped_tier_leaves_the_model_empty_rather_than_guessing(self):
        items = self._items()
        pl = fp.plan(items, None, ["auth"], trajectories=[])
        res = fp.run_plan(pl, items, "agent --model {model}", dry_run=True,
                          branches=False, tier_models={"top": "opus"})
        blank = [r for r in res["dispatched"] if r["cluster"] == "trivial"]
        self.assertEqual(blank[0]["argv"][-1], "")


class BriefParity(unittest.TestCase):
    """Every dispatch path must send the SAME brief. If they can differ, the
    hand-dispatched agents are the ones told less, and nobody finds out."""

    ITEMS = [{"name": "A", "files": ["f1.py"], "task": "do A"},
             {"name": "B", "files": ["f1.py", "f2.py"], "task": "do B"},
             {"name": "C", "files": ["f2.py"], "task": "do C"}]

    def test_clusters_carry_their_own_walk_order(self):
        pl = fp.plan(self.ITEMS, None, [], trajectories=[])
        self.assertEqual(pl["clusters"], [["B", "A", "C"]])   # hub first

    def test_exec_matches_the_brief_with_and_without_briefs_emitted(self):
        with_b = fp.plan(self.ITEMS, None, [], trajectories=[])
        without = fp.plan(self.ITEMS, None, [], trajectories=[], briefs=False)
        a = fp.run_plan(with_b, self.ITEMS, "x {prompt}", dry_run=True,
                        branches=False)["dispatched"][0]["argv"][-1]
        b = fp.run_plan(without, self.ITEMS, "x {prompt}", dry_run=True,
                        branches=False)["dispatched"][0]["argv"][-1]
        self.assertEqual(a, with_b["cluster_briefs"][0]["prompt"])
        self.assertEqual(a, b)

    def test_leaf_order_survives_into_the_prompt(self):
        pl = fp.plan(self.ITEMS, None, [], trajectories=[])
        prompt = pl["cluster_briefs"][0]["prompt"]
        self.assertLess(prompt.index('"B"'), prompt.index('"A"'))

if __name__ == "__main__":
    unittest.main()
