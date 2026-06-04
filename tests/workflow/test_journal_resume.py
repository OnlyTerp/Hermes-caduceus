"""Regression tests for Loom resume caching.

A leaf that failed for a transient reason must NOT be journaled as a reusable
result — otherwise resume replays a cached ``None`` instead of re-running it.
"""

from __future__ import annotations

from agent.workflow.journal import Journal, call_key, load_resume_cache


def _record(j, key, *, result, status):
    j.record(key, prompt="p", phase=None, result=result, status=status,
             tokens={"in": 1, "out": 1}, label="l")


def test_failed_leaf_is_not_cached_in_memory(tmp_path):
    j = Journal(str(tmp_path / "run1"))
    ok_key = call_key("ok", {}, None, 0)
    bad_key = call_key("bad", {}, None, 1)
    _record(j, ok_key, result="VALUE", status="done")
    _record(j, bad_key, result=None, status="failed")

    assert j.lookup(ok_key) == (True, "VALUE")
    # The failed leaf is not a cache hit, so a same-run lookup would re-run it.
    assert j.lookup(bad_key) == (False, None)


def test_resume_cache_skips_failed_rows(tmp_path):
    run_dir = tmp_path / "wf" / "run1"
    j = Journal(str(run_dir))
    ok_key = call_key("ok", {}, None, 0)
    bad_key = call_key("bad", {}, None, 1)
    _record(j, ok_key, result="VALUE", status="done")
    _record(j, bad_key, result=None, status="failed")

    cache = load_resume_cache(str(tmp_path / "wf"), "run1")

    assert cache == {ok_key: "VALUE"}
    assert bad_key not in cache  # resume re-runs the failed tail instead of caching None
