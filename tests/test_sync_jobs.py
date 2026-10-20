import json
import select
import subprocess
import sys
import threading

import pandas as pd
import pytest
from test_sync_service import raw, request

import data_loading
import desk_sync
import sync_checkpoints


def test_worker_saves_without_another_ui_run(storage, monkeypatch):
    import sync_jobs

    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [raw()])
    jobs = sync_jobs.SyncJobs()
    assert jobs.start(request(projects=["SDAX"]))
    jobs.thread.join(10)
    assert not jobs.thread.is_alive()
    assert pd.read_pickle(storage).loc[0, "Hauptkategorie"] == "Main"
    status = jobs.snapshot()
    assert status["state"] == "completed" and status["report"]["complete"]


def test_two_ui_coordinators_and_cli_admit_only_one_import(storage, monkeypatch):
    import sync_jobs

    entered, release = threading.Event(), threading.Event()

    def fetch(*a, **k):
        entered.set()
        assert release.wait(10)
        return [raw()]

    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", fetch)
    first, second = sync_jobs.SyncJobs(), sync_jobs.SyncJobs()
    try:
        assert first.start(request(projects=["SDAX"]))
        assert entered.wait(5)
        assert not second.start(request(projects=["SDAX"]))
        with pytest.raises(data_loading.ImportBusyError):
            desk_sync.run_sync(request(projects=["SDAX"]))
        assert second.snapshot()["state"] == "running"
    finally:
        release.set()
        if first.thread:
            first.thread.join(10)


@pytest.mark.parametrize("later_state", ["completed", "interrupted", "running"])
def test_new_shared_run_supersedes_local_pre_admission_failure(
    storage, monkeypatch, later_state
):
    import sync_jobs

    write = desk_sync.write_json_atomic

    def fail_status(*a, **k):
        raise OSError("storage temporarily unavailable")

    monkeypatch.setattr(desk_sync, "write_json_atomic", fail_status)
    jobs = sync_jobs.SyncJobs()
    assert not jobs.start(request(projects=["SDAX"]))
    jobs.thread.join(10)
    assert jobs.snapshot()["state"] == "failed"
    with data_loading.import_operation_lock(storage):
        assert jobs.snapshot()["state"] == "running"
    assert jobs.snapshot()["state"] == "failed"
    monkeypatch.setattr(desk_sync, "write_json_atomic", write)
    newer = request(projects=["SDEU"])
    if later_state == "completed":
        monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [])
        assert desk_sync.run_sync(newer).complete
    else:
        write(
            desk_sync.status_path(storage),
            {"run_id": "new-cli", "state": "running", "request": newer.to_dict()},
        )
    if later_state == "running":
        with data_loading.import_operation_lock(storage):
            assert jobs.snapshot()["state"] == "running"
    else:
        assert jobs.snapshot()["state"] == later_state
    assert jobs.snapshot()["request"]["projects"] == ["SDEU"]
    if later_state == "interrupted":
        accepted = []
        monkeypatch.setattr(jobs, "start", lambda saved: accepted.append(saved) or True)
        assert jobs.resume()
        assert accepted[0].projects == ("SDEU",) and accepted[0].resume


def test_killed_process_is_observable_and_its_checkpoint_can_resume(
    storage, monkeypatch
):
    import sync_jobs

    code = """import sys,time,json
from datetime import datetime,UTC
import data_loading,desk_sync,sync_checkpoints
data_loading.DATA_PATH=sys.argv[1]
def fetch(*a,partial_cb,**kwargs):
    partial_cb([json.loads(sys.argv[2])])
    print('checkpoint',flush=True)
    time.sleep(30)
sync_checkpoints.fetch_jira_issues=fetch
desk_sync.run_sync(desk_sync.make_sync_request(datetime(2026,9,1,tzinfo=UTC),datetime(2026,9,10,tzinfo=UTC),['SDAX']))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(storage), json.dumps(raw())],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline().strip() == "checkpoint"
    finally:
        process.terminate()
        process.communicate(timeout=10)
    jobs = sync_jobs.SyncJobs()
    assert jobs.snapshot()["state"] == "interrupted"
    monkeypatch.setattr(sync_checkpoints, "fetch_jira_issues", lambda *a, **k: [])
    assert jobs.resume()
    jobs.thread.join(10)
    assert not jobs.thread.is_alive()
    assert pd.read_pickle(storage).key.tolist() == ["SDAX-1"]
    assert jobs.snapshot()["state"] in {"completed", "partial"}


def test_cli_and_both_ui_controls_produce_identical_schema_after_category_retry(
    storage, monkeypatch, public_ui
):
    from unittest.mock import Mock

    import requests
    from streamlit.testing.v1 import AppTest

    import backfill_jira
    import category_sync
    import jira_loader
    import sync_jobs

    jira = Mock()
    jira.myself.return_value = {"timeZone": "UTC"}
    jira.enhanced_search_issues.return_value = {"issues": [raw()]}
    monkeypatch.setattr(jira_loader, "JIRA", lambda *a, **k: jira)
    monkeypatch.setattr(
        sync_checkpoints, "fetch_jira_issues", jira_loader.fetch_jira_issues
    )
    monkeypatch.setenv("JIRA_URL", "https://jira.example")
    monkeypatch.setenv("JIRA_USERNAME", "test-user")
    monkeypatch.setenv("JIRA_PASSWORD", "synthetic-password")
    actual_resolver = category_sync.CategoryResolver
    frames = []
    for adapter in ("sidebar", "custom", "cli"):
        path = storage.parent / adapter / "cache.pkl"
        monkeypatch.setattr(data_loading, "DATA_PATH", str(path))
        attempts = {}

        def fetch(cloud, workspace, oid, _attempts=attempts):
            _attempts[oid] = _attempts.get(oid, 0) + 1
            if oid == "main-1" and _attempts[oid] == 1:
                raise requests.Timeout("transient")
            return {"id": oid, "label": "Main" if oid == "main-1" else "Sub"}

        monkeypatch.setattr(jira_loader, "fetch_asset_object_uncached", fetch)
        monkeypatch.setattr(
            desk_sync,
            "CategoryResolver",
            lambda: actual_resolver(sleep=lambda seconds: None),
        )
        jobs = sync_jobs.SyncJobs()
        monkeypatch.setattr(sync_jobs, "JOB", jobs)
        if adapter == "cli":
            assert (
                backfill_jira.main(
                    [
                        "--project",
                        "SDAX",
                        "--start",
                        "2026-09-01",
                        "--end",
                        "2026-09-10",
                    ]
                )
                == 0
            )
        elif adapter == "sidebar":
            # Sidebar always requests all desks. Return a ticket only for SDAX.
            jira.enhanced_search_issues.side_effect = lambda **kwargs: {
                "issues": [raw()] if "project = SDAX" in kwargs["jql_str"] else []
            }
            app = AppTest.from_string(
                "from datetime import datetime,UTC\nimport streamlit as st\nfrom sync_jobs import render_sidebar_refresh\nwith st.sidebar:\n    render_sidebar_refresh(datetime(2026,9,1,tzinfo=UTC),datetime(2026,9,10,tzinfo=UTC))"
            ).run()
            app.button(key="sync_sidebar_start").click().run()
            assert not app.exception
        else:
            app = AppTest.from_string(
                "from temporary_history_pull import render_history_pull\nrender_history_pull()"
            ).run()
            from datetime import date

            app.date_input(key="temporary_history_dates").set_value(
                (date(2026, 9, 1), date(2026, 9, 10))
            ).run()
            app.multiselect(key="temporary_history_desks").set_value(["SDAX"]).run()
            app.button(key="temporary_history_start").click().run()
            assert not app.exception
        if adapter != "cli":
            jobs.thread.join(10)
            assert not jobs.thread.is_alive()
            assert jobs.snapshot()["report"]["complete"]
        frame = pd.read_pickle(path)
        assert frame.Hauptkategorie.tolist() == ["Main"]
        assert frame.Unterkategorie.tolist() == ["Sub"]
        assert attempts == {"main-1": 2, "sub-1": 1}
        frames.append(frame)
    pd.testing.assert_frame_equal(frames[0], frames[1])
    pd.testing.assert_frame_equal(frames[1], frames[2])
