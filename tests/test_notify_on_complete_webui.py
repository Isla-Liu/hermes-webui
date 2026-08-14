import ast
from pathlib import Path
import json
import queue
import sys
import threading
import time
import types
from types import SimpleNamespace

from api import background_process as bp
from api import config, models, routes
from api.models import Session
from api.session_lineage import (
    build_completion_delivery_context,
    completion_delivery_metadata,
    mark_completion_execution_delivered,
    mark_completion_execution_started,
)
from api.turn_journal import read_turn_journal
from tests._wakeup_helpers import FakeProcessRegistry, install_fake_registry


def test_webui_drains_only_matching_background_completion_events():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "def _drain_webui_process_notifications(" in src
    assert "pending_async_acceptances: list | None = None" in src
    assert "from tools.process_registry import process_registry" in src
    assert "proc = process_registry.get(evt_sid)" in src
    assert "def _completion_event_targets_webui_session(evt_session_key: str, session_id: str)" in src
    assert "PROCESS_SESSION_INDEX.get(evt_session_key) == session_id" in src
    assert "not _completion_event_targets_webui_session(evt_session_key, session_id)" in src
    assert "skipped_events.append(evt)" in src
    assert "completion_queue.put(evt)" in src


def test_webui_injects_process_notifications_without_persisting_them_as_user_text():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "_process_notifications = _drain_webui_process_notifications(" in src
    assert "pending_async_acceptances=_pending_async_acceptances" in src
    assert "_accept_pending_async_delegations(" in src
    assert "[*_process_notifications, msg_text]" in src
    assert "_build_native_multimodal_message(workspace_ctx, _agent_msg_text" in src
    assert "persist_user_message=msg_text" in src


def test_webui_sets_gateway_session_platform_for_background_watchers():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    assert "'HERMES_SESSION_PLATFORM': 'webui'" in src
    assert "os.environ['HERMES_SESSION_PLATFORM'] = 'webui'" in src
    assert "old_session_platform = os.environ.get('HERMES_SESSION_PLATFORM')" in src
    assert "os.environ.pop('HERMES_SESSION_PLATFORM', None)" in src


def test_webui_age_gates_stale_background_completion_events():
    """Issue #4029: drain must drop completions older than the configured cap
    so stale notifications can't be prepended to an unrelated later turn."""
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    # The age-gate helper + its env override exist.
    assert "def _stale_completion_max_age_seconds()" in src
    assert "HERMES_WEBUI_STALE_COMPLETION_MAX_AGE_SECONDS" in src
    # The drain reads completed_at and drops over-age events without requeueing.
    assert "completed_at = evt.get('completed_at')" in src
    assert "stale_age = time.time() - completed_at" in src
    assert "is_stale = stale_age > stale_completion_max_age" in src
    assert "if is_stale:" in src
    # Over-age process events use the registry marker; async delegations finish
    # their durable delivery claim. Neither path is added to skipped_events.
    assert "_mark_process_completion_consumed(process_registry, evt_sid)" in src
    assert "complete_async_delegation_delivery(evt, claim)" in src


def test_drain_orders_converge_on_one_completion(monkeypatch, tmp_path):
    execution_actions = []
    worker_threads = []
    admissions = []
    real_thread = threading.Thread

    class ImmediateThread:
        def __init__(self, *args, target=None, **kwargs):
            self.target = target
            self.args = kwargs.pop("args", ())
            self.kwargs = kwargs.pop("kwargs", {})

        def start(self):
            if self.target is not None:
                self.target(*self.args, **self.kwargs)

    class RecordedThread:
        def __init__(self, *args, **kwargs):
            self.thread = real_thread(*args, **kwargs)
            worker_threads.append(self.thread)

        def start(self):
            self.thread.start()

        def __getattr__(self, name):
            return getattr(self.thread, name)

    def admitted_worker(
        session_id,
        prompt,
        _model,
        _workspace,
        stream_id,
        _attachments,
        *,
        admission,
        completion_context,
        **_kwargs,
    ):
        admissions.append(admission)
        admission.admitted.set()
        assert admission.gate.wait(timeout=5)
        if not admission.abort.is_set():
            mark_completion_execution_started(
                completion_context,
                reservation_id=stream_id,
            )
            execution_actions.append(
                {"session_id": session_id, "prompt": prompt, "stream_id": stream_id}
            )
            mark_completion_execution_delivered(
                completion_context,
                reservation_id=stream_id,
            )

    bp_threading = SimpleNamespace(**vars(bp.threading))
    bp_threading.Thread = ImmediateThread
    routes_threading = SimpleNamespace(**vars(routes.threading))
    routes_threading.Thread = RecordedThread
    monkeypatch.setattr(bp, "threading", bp_threading)
    monkeypatch.setattr(routes, "threading", routes_threading)
    monkeypatch.setattr(routes, "_run_admitted_agent_streaming", admitted_worker)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_a, **_k: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "_resolve_chat_workspace_with_recovery",
        lambda _session, _requested: str(tmp_path),
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda *_a, **_k: ("test-model", None, False),
    )
    monkeypatch.setattr(routes, "create_stream_channel", queue.Queue)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "webui_gateway_chat_enabled", lambda _config: False)
    monkeypatch.setattr(bp, "_mark_registry_completion_consumed", lambda _process_id: None)

    for order in ("next-first", "teardown-first"):
        order_dir = tmp_path / order
        order_dir.mkdir()
        monkeypatch.setattr(config, "SESSION_DIR", order_dir, raising=False)
        monkeypatch.setattr(models, "SESSION_DIR", order_dir)
        with config.LOCK:
            config.SESSIONS.clear()
        with config.ACTIVE_RUNS_LOCK:
            config.ACTIVE_RUNS.clear()
        with routes.STREAMS_LOCK:
            routes.STREAMS.clear()
        with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
            config.DEFERRED_PROCESS_WAKEUPS.clear()

        root = Session(
            session_id=f"root-{order}",
            title="root",
            profile="default",
            pre_compression_snapshot=True,
        )
        root.save()
        tip = Session(
            session_id=f"tip-{order}",
            title="tip",
            profile="default",
            parent_session_id=root.session_id,
        )
        tip.save()
        with config.LOCK:
            config.SESSIONS[root.session_id] = root
            config.SESSIONS[tip.session_id] = tip

        process_id = f"proc-{order}"
        prompt = "same completion"
        assert bp.record_deferred_wakeup(tip.session_id, process_id, prompt)
        action_start = len(execution_actions)
        thread_start = len(worker_threads)
        admission_start = len(admissions)
        if order == "next-first":
            claimed = bp.claim_deferred_wakeups(root.session_id)
            assert len(claimed) == 1
            entry = claimed[0]
            bp._start_server_side_wakeup_turn(
                tip.session_id,
                entry["wakeup_prompt"],
                process_id=entry["process_id"],
            )
            assert bp.drain_deferred_wakeups_for_session(root.session_id) == 0
        else:
            assert bp.drain_deferred_wakeups_for_session(root.session_id) == 1
            assert bp.claim_deferred_wakeups(root.session_id) == []

        for worker in worker_threads[thread_start:]:
            worker.join(timeout=5)
            assert not worker.is_alive()

        completion_key = f"process:{process_id}"
        metadata = completion_delivery_metadata(
            build_completion_delivery_context(
                {
                    "type": "process_complete",
                    "process_id": process_id,
                    "session_key": f"ui:{root.session_id}",
                    "origin_ui_session_id": root.session_id,
                    "origin_profile": "default",
                },
                tip.session_id,
            )
        )
        receipt_document = json.loads(
            (order_dir / "_completion_delivery_receipts.json").read_text(encoding="utf-8")
        )
        assert receipt_document["version"] == 2
        assert list(receipt_document["receipts"]) == [completion_key]
        receipt = receipt_document["receipts"][completion_key]
        assert receipt["state"] == "incorporated"
        assert receipt["execution_state"] == "delivered"
        assert receipt["reservation_id"] == admissions[admission_start].stream_id

        persisted_tip = json.loads((order_dir / f"{tip.session_id}.json").read_text(encoding="utf-8"))
        persisted_root = json.loads((order_dir / f"{root.session_id}.json").read_text(encoding="utf-8"))
        tip_rows = [
            row for row in persisted_tip["messages"]
            if row.get("_completion_delivery") == metadata
        ]
        context_rows = [
            row for row in persisted_tip["context_messages"]
            if row.get("_completion_delivery") == metadata
        ]
        assert len(tip_rows) == len(context_rows) == 1
        assert not [
            row for row in persisted_root["messages"] + persisted_root["context_messages"]
            if row.get("_completion_delivery") == metadata
        ]
        submitted = [
            event for event in read_turn_journal(tip.session_id)["events"]
            if event.get("_completion_delivery") == metadata
        ]
        assert len(submitted) == 1
        assert submitted[0]["stream_id"] == receipt["reservation_id"]
        assert execution_actions[action_start:] == [
            {
                "session_id": tip.session_id,
                "prompt": prompt,
                "stream_id": receipt["reservation_id"],
            }
        ]
        for evidence in submitted + tip_rows + context_rows:
            assert "owner_token" not in evidence
            assert "completion_owner_token" not in evidence
            assert "owner_token" not in evidence["_completion_delivery"]

        routes._abort_prepared_chat_turn(
            tip,
            admissions[admission_start].stream_id,
            admissions[admission_start],
        )


def test_current_turn_fresh_process_and_async_choose_same_root_and_tip_owner(
    monkeypatch,
    tmp_path,
):
    """R3: both fresh kinds converge on one root/profile and current tip."""
    from api import streaming
    from tests.test_background_process_restart_recovery import (
        _install_current_turn_async_source,
    )

    monkeypatch.setattr(config, "SESSION_DIR", tmp_path, raising=False)
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    with config.LOCK:
        config.SESSIONS.clear()
    with config.ACTIVE_RUNS_LOCK:
        config.ACTIVE_RUNS.clear()
    with routes.STREAMS_LOCK:
        routes.STREAMS.clear()
    with config.BG_TASK_COMPLETE_EVENTS_SEEN_LOCK:
        config.BG_TASK_COMPLETE_EVENTS_SEEN.clear()
    with config.DEFERRED_PROCESS_WAKEUPS_LOCK:
        config.DEFERRED_PROCESS_WAKEUPS.clear()

    root = Session(
        session_id="convergence-root",
        title="root",
        profile="default",
        pre_compression_snapshot=True,
    )
    root.save()
    tip = Session(
        session_id="convergence-tip",
        title="tip",
        profile="default",
        parent_session_id=root.session_id,
    )
    tip.save()
    with config.LOCK:
        config.SESSIONS[root.session_id] = root
        config.SESSIONS[tip.session_id] = tip

    registry = FakeProcessRegistry()
    process_id = "proc-root-tip-convergence"
    delegation_id = "deleg-root-tip-convergence"
    registry.register(process_id, root.session_id)
    registry.register(delegation_id, root.session_id)
    install_fake_registry(monkeypatch, registry)
    process_module = sys.modules["tools.process_registry"]
    monkeypatch.setattr(
        process_module,
        "format_process_notification",
        lambda evt: (
            f"[ASYNC DELEGATION BATCH COMPLETE — {evt['delegation_id']}]\n"
            "converged"
        ),
        raising=False,
    )
    async_state = _install_current_turn_async_source(monkeypatch)
    process_event = {
        "type": "completion",
        "session_id": process_id,
        "session_key": root.session_id,
        "origin_ui_session_id": root.session_id,
        "origin_profile": "default",
        "command": "true",
        "exit_code": 0,
        "output": "ordinary",
        "completed_at": time.time(),
    }
    async_event = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": root.session_id,
        "origin_ui_session_id": root.session_id,
        "origin_profile": "default",
        "status": "completed",
        "is_batch": True,
        "results": [{"task_index": 0, "status": "completed", "summary": "async"}],
        "completed_at": time.time(),
    }
    registry.completion_queue.put(process_event)
    registry.completion_queue.put(async_event)

    class ImmediateThread:
        def __init__(self, *args, target=None, **kwargs):
            self.target = target
            self.args = kwargs.pop("args", ())
            self.kwargs = kwargs.pop("kwargs", {})

        def start(self):
            assert self.target is not None
            self.target(*self.args, **self.kwargs)

    bp_threading = types.SimpleNamespace(**vars(bp.threading))
    bp_threading.Thread = ImmediateThread
    monkeypatch.setattr(bp, "threading", bp_threading)
    monkeypatch.setattr(bp, "_session_has_active_turn", lambda _sid: False)
    monkeypatch.setattr(bp, "_emit_bg_task_complete_events_coalesced", lambda *_a: 0)
    starts = []

    def accept_canonical_turn(
        session_id,
        message,
        *,
        source,
        completion_context,
        completion_acceptance,
    ):
        starts.append(
            {
                "session_id": session_id,
                "message": message,
                "source": source,
                "context": completion_context,
            }
        )
        completion_acceptance()
        return {"_status": 200, "stream_id": completion_context.correlation_id[:32]}

    monkeypatch.setattr(routes, "start_session_turn", accept_canonical_turn)
    result = streaming._drain_webui_process_notifications(tip.session_id)

    print(
        "CURRENT_TURN_ROOT_TIP_OWNER="
        + json.dumps(
            {
                "contexts": len(starts),
                "kinds": sorted(
                    start["context"].completion_key.split(":", 1)[0] for start in starts
                ),
            },
            sort_keys=True,
        )
    )
    # Baseline reaches the drain but settles both sources directly, leaving zero
    # CompletionDeliveryContext instances. That is behavioral RED, not AST RED.
    assert result is None
    assert len(starts) == 2
    assert {start["context"].completion_key for start in starts} == {
        f"process:{process_id}",
        f"async_delegation:{delegation_id}",
    }
    for start in starts:
        context = start["context"]
        assert start["session_id"] == tip.session_id
        assert context.origin_ui_session_id == root.session_id
        assert context.root_session_id == root.session_id
        assert context.delivery_session_id == tip.session_id
        assert context.profile == "default"
    assert registry.is_completion_consumed(process_id) is True
    assert async_state["delivery_state"] == "delivered"
    assert async_state["claims"] == [(delegation_id, "webui-background")]
    assert len(async_state["completions"]) == 1

    cross_profile = dict(process_event)
    cross_profile["session_id"] = "proc-cross-profile"
    cross_profile["origin_profile"] = "named-other-profile"
    registry.completion_queue.put(cross_profile)
    streaming._drain_webui_process_notifications(tip.session_id)
    assert registry.completion_queue.qsize() == 1
    assert registry.completion_queue.get_nowait() is cross_profile


def test_current_turn_handoff_static_owner_and_order_contract():
    source = Path("api/streaming.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    [drain] = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_drain_webui_process_notifications"
    ]
    assert [arg.arg for arg in drain.args.args] == ["session_id"]
    assert drain.args.kwonlyargs == []
    assert drain.returns is not None
    assert ast.unparse(drain.returns) == "None"
    drain_source = ast.get_source_segment(source, drain) or ""
    assert drain_source.count("_process_one(evt)") == 1
    assert "scan_budget = completion_queue.qsize()" in drain_source
    assert drain_source.index("for evt in skipped_events:") < drain_source.index(
        "_process_one(evt)"
    )
    assert "_format_process_notification" not in drain_source
    assert "pending_async_acceptances" not in source
    assert "def _accept_pending_async_delegations" not in source
    assert "_process_notifications = _drain_webui_process_notifications" not in source
    assert "_agent_msg_text" not in source
    assert "_build_native_multimodal_message(workspace_ctx, msg_text" in source
    assert "persist_user_message=msg_text" in source
