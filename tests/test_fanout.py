import json
import os
import sys
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

    def test_plan_emits_global_waves_and_clusters(self):
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
        self.assertIn("waves", out)
        flat = [n for w in out["waves"] for n in w]
        self.assertEqual(sorted(flat), sorted(it["name"] for it in items))
        # solo has no conflicts -> rides the first wave alongside the hub
        self.assertIn("solo", out["waves"][0])
        # no wave contains two items that share a file
        files = {it["name"]: set(it["files"]) for it in items}
        for wave in out["waves"]:
            for i in range(len(wave)):
                for j in range(i + 1, len(wave)):
                    self.assertFalse(files[wave[i]] & files[wave[j]],
                                     f"{wave[i]} and {wave[j]} share a file in one wave")

    def test_plan_works_without_graph_and_drops_decided_pairs(self):
        # --graph is optional: clustering/waves/tiers still compute; and a
        # pair with a declared `after` path is a DECIDED order, so it must
        # not reappear in coupling_review.
        items = [
            {"name": "BE", "files": ["ledger_api.py"]},
            {"name": "FE", "files": ["ledger_ui.tsx"], "after": ["BE"]},
        ]
        out = fp.plan(items, None, ["ledger"])
        self.assertEqual(out["waves"], [["BE"], ["FE"]])
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
        items = [{"name": "BIG", "files": ["b.py"], "cost": 2},
                 {"name": "HEAD", "files": ["h.py"], "cost": 1},
                 {"name": "MID", "files": ["m.py"], "after": ["HEAD"]},
                 {"name": "TAIL", "files": ["t.py"], "after": ["MID"]}]
        waves = fp.plan(items, None, [])["waves"]
        self.assertLess(waves[0].index("HEAD"), waves[0].index("BIG"))

    def test_ready_after_is_a_dag_never_a_whole_wave_barrier(self):
        items = [{"name": "A", "files": ["a.py"]},
                 {"name": "SLOW", "files": ["s.py"]},
                 {"name": "B", "files": ["a.py"]}]  # conflicts with A only
        out = fp.plan(items, None, [])
        ra = out["ready_after"]
        self.assertEqual(ra["A"], [])
        self.assertEqual(ra["SLOW"], [])
        self.assertEqual(ra["B"], ["A"])       # waits on its conflict...
        self.assertNotIn("SLOW", ra["B"])      # ...not on the whole wave

    def test_ready_after_honors_declared_producers(self):
        items = [{"name": "BE", "files": ["api.py"]},
                 {"name": "FE1", "files": ["a.tsx"], "after": ["BE"]},
                 {"name": "FE2", "files": ["b.tsx"], "after": ["BE"]}]
        ra = fp.plan(items, None, [])["ready_after"]
        self.assertEqual(ra["FE1"], ["BE"])
        self.assertEqual(ra["FE2"], ["BE"])
        self.assertNotIn("FE1", ra["FE2"])  # two consumers stay parallel

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


if __name__ == "__main__":
    unittest.main()
