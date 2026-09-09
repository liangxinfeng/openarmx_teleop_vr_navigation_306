import json
from pathlib import Path

import pytest

from openarmx_teleop_vr_navigation_306.task_workflow import (
    TaskWorkflow,
    navigation_terminal_outcome,
    task_chain_templates,
)


CATALOG = {
    "task_locations": {
        "迎宾区": [{"task": "迎宾接待", "body_height_level": 5}],
        "电视区": [{"task": "丢垃圾", "body_height_level": 3}],
    }
}


def test_confirm_navigate_arrive_and_advance():
    workflow = TaskWorkflow(CATALOG)
    workflow.submit("先迎宾再丢垃圾", [
        {"task": "迎宾接待", "location": "迎宾区"},
        {"task": "丢垃圾", "location": "电视区"},
    ])
    workflow.confirm(["迎宾区", "电视区"])
    workflow.navigation_started()
    assert workflow.navigation_arrived("迎宾区")["body_height_level"] == 5
    assert workflow.snapshot()["state"] == "adjusting_height"
    workflow.height_adjusted()
    state, completed, upcoming = workflow.complete_action()
    assert state == "next"
    assert completed["task"] == "迎宾接待"
    assert upcoming["body_height_level"] == 3
    workflow.navigation_started()
    workflow.navigation_arrived("电视区")
    workflow.height_adjusted()
    state, _, upcoming = workflow.complete_action()
    assert state == "done" and upcoming is None
    assert workflow.snapshot()["state"] == "completed"


def test_missing_waypoint_fails_closed_before_navigation():
    workflow = TaskWorkflow(CATALOG)
    workflow.submit("迎宾", [{"task": "迎宾接待", "location": "迎宾区"}])
    with pytest.raises(ValueError, match="迎宾区"):
        workflow.confirm([])
    assert workflow.snapshot()["state"] == "pending_confirmation"


def test_catalog_rejects_fabricated_pair_and_active_replacement():
    workflow = TaskWorkflow(CATALOG)
    with pytest.raises(ValueError):
        workflow.submit("错误任务", [{"task": "丢垃圾", "location": "迎宾区"}])
    workflow.submit("迎宾", [{"task": "迎宾接待", "location": "迎宾区"}])
    workflow.confirm(["迎宾区"])
    with pytest.raises(RuntimeError):
        workflow.submit("丢垃圾", [{"task": "丢垃圾", "location": "电视区"}])


def test_height_failure_stops_before_operator_action():
    workflow = TaskWorkflow(CATALOG)
    workflow.submit("迎宾", [{"task": "迎宾接待", "location": "迎宾区"}])
    workflow.confirm(["迎宾区"])
    workflow.navigation_started()
    workflow.navigation_arrived("迎宾区")
    assert workflow.height_failed("高度未到位") is True
    assert workflow.snapshot()["state"] == "failed"
    with pytest.raises(RuntimeError):
        workflow.complete_action()


def test_navigation_failure_can_skip_to_next_task():
    workflow = TaskWorkflow(CATALOG)
    workflow.submit("连续任务", [
        {"task": "迎宾接待", "location": "迎宾区"},
        {"task": "丢垃圾", "location": "电视区"},
    ])
    workflow.confirm(["迎宾区", "电视区"])
    workflow.navigation_started()
    assert workflow.navigation_failed("未到达迎宾区") is True
    assert workflow.snapshot()["state"] == "navigation_failed"
    result, skipped, upcoming = workflow.skip_navigation_failure()
    assert result == "next"
    assert skipped["location"] == "迎宾区"
    assert upcoming["location"] == "电视区"
    assert workflow.snapshot()["state"] == "preparing"


def test_navigation_failure_can_retry_same_task():
    workflow = TaskWorkflow(CATALOG)
    workflow.submit("连续任务", [
        {"task": "迎宾接待", "location": "迎宾区"},
        {"task": "丢垃圾", "location": "电视区"},
    ])
    workflow.confirm(["迎宾区", "电视区"])
    workflow.navigation_started()
    workflow.navigation_failed("未到达迎宾区")
    current = workflow.retry_navigation_failure()
    assert current["location"] == "迎宾区"
    assert workflow.snapshot()["current_index"] == 0
    assert workflow.snapshot()["state"] == "preparing"
    workflow.navigation_started()
    assert workflow.snapshot()["state"] == "navigating"


def test_operator_stop_navigation_keeps_current_task_retryable():
    workflow = TaskWorkflow(CATALOG)
    workflow.submit("迎宾", [{"task": "迎宾接待", "location": "迎宾区"}])
    workflow.confirm(["迎宾区"])
    workflow.navigation_started()
    current = workflow.operator_stopped_navigation()
    assert current["location"] == "迎宾区"
    assert workflow.snapshot()["state"] == "navigation_failed"
    assert workflow.snapshot()["current_index"] == 0


def test_task_chain_templates_are_config_driven_and_validated():
    catalog = dict(CATALOG)
    catalog["task_templates"] = [{
        "id": "demo",
        "name": "演示流程",
        "tasks": [
            {"location": "迎宾区", "task": "迎宾接待"},
            {"location": "电视区", "task": "丢垃圾"},
        ],
    }]
    templates = task_chain_templates(catalog)
    assert templates[0]["id"] == "demo"
    assert templates[0]["task_count"] == 2
    catalog["task_templates"][0]["tasks"][0]["task"] = "不存在的动作"
    with pytest.raises(ValueError, match="未知任务"):
        task_chain_templates(catalog)


def test_showroom_single_waypoint_can_use_trusted_generic_task():
    workflow = TaskWorkflow(CATALOG)
    workflow.submit(
        "展厅模式单点任务：临时航点",
        [{
            "location": "临时航点",
            "task": "展厅定点服务",
            "body_height_level": 4,
        }],
        allow_unlisted=True,
    )
    workflow.confirm(["临时航点"])
    assert workflow.current()["body_height_level"] == 4


@pytest.mark.parametrize("state", [
    "succeeded", "succeeded_by_tolerance", "short_distance_succeeded",
    "sequence_succeeded",
])
def test_all_navigation_success_variants_advance_task(state):
    assert navigation_terminal_outcome(state) == "succeeded"


@pytest.mark.parametrize("state", [
    "failed", "short_distance_failed", "rejected", "canceled",
])
def test_all_navigation_failure_variants_stop_task(state):
    assert navigation_terminal_outcome(state) == "failed"
