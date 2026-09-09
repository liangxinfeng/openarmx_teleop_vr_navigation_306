"""Regression coverage for the touch task console and live state channel."""

import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

from openarmx_teleop_vr_navigation_306.task_voice_web import TaskVoiceServer
from openarmx_teleop_vr_navigation_306.task_planner import TaskPlanner


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _write_catalog(path, entries):
    locations = {}
    for location, task, level in entries:
        locations.setdefault(location, []).append(
            {"task": task, "body_height_level": level}
        )
    path.write_text(
        json.dumps({"version": 1, "task_locations": locations}, ensure_ascii=False),
        encoding="utf-8",
    )


def _server(path):
    return TaskVoiceServer(path, "https://127.0.0.1:8445/api/tasks/submit")


def test_server_restart_has_a_new_epoch(tmp_path):
    catalog = tmp_path / "tasks.json"
    _write_catalog(catalog, [("迎宾区", "迎宾接待", 5)])
    first = _server(catalog)
    second = _server(catalog)

    assert first.server_epoch != second.server_epoch


def test_catalog_sequence_preserves_order_duplicates_and_configured_height(tmp_path):
    catalog = tmp_path / "tasks.json"
    _write_catalog(
        catalog,
        [("前台区", "拿水", 4), ("迎宾区", "迎宾接待", 5)],
    )
    server = _server(catalog)
    tasks = server._normalise_catalog_tasks([
        {"location": "迎宾区", "task": "迎宾接待"},
        {"location": "前台区", "task": "拿水", "body_height_level": 1},
        {"location": "迎宾区", "task": "迎宾接待"},
    ])

    assert [(item["location"], item["task"]) for item in tasks] == [
        ("迎宾区", "迎宾接待"),
        ("前台区", "拿水"),
        ("迎宾区", "迎宾接待"),
    ]
    assert tasks[1]["body_height_level"] == 4


def test_catalog_hot_reload_detects_new_source_content(tmp_path):
    catalog = tmp_path / "tasks.json"
    _write_catalog(catalog, [("迎宾区", "迎宾接待", 5)])
    server = _server(catalog)
    previous = server.catalog_revision
    time.sleep(0.002)
    _write_catalog(
        catalog,
        [("迎宾区", "迎宾接待", 5), ("电视区", "垃圾清理", 3)],
    )

    snapshot = server.catalog_snapshot()
    assert snapshot["revision"] != previous
    assert [item["task"] for item in snapshot["tasks"]] == ["迎宾接待", "垃圾清理"]


def test_generated_plan_has_single_server_revision(tmp_path):
    catalog = tmp_path / "tasks.json"
    _write_catalog(catalog, [("迎宾区", "迎宾接待", 5)])
    server = _server(catalog)
    plan = server._remember_generated_plan(
        "卡片编排",
        [{"location": "迎宾区", "task": "迎宾接待", "body_height_level": 5}],
        origin="catalog",
    )

    assert plan["server_epoch"] == server.server_epoch
    assert plan["plan_revision"] == 1
    assert plan["origin"] == "catalog"


def test_page_uses_sse_touch_tabs_and_no_live_plan_cache():
    page = (PACKAGE_ROOT / "openarmx_teleop_vr_navigation_306" / "task_page.py").read_text(
        encoding="utf-8"
    )

    assert "const UI_VERSION=String(BOOTSTRAP.ui_version||'')" in page
    assert "task-console-v13-dynamic-hints" not in page
    assert "new EventSource('/api/events')" in page
    assert "setInterval(syncState,2000)" in page
    assert "/api/catalog-plan" in page
    assert "pointerdown" in page
    assert "touchstart" in page
    assert "卡片编排" in page
    assert "yundie.taskQueue.v2" in page
    assert "latestTaskPlan" not in page
    assert "function clearElement(element)" in page
    assert "replaceChildren()" not in page
    assert "store.catalogRevision=catalog.revision" in page
    assert 'id="transcript"' in page
    assert "data.transcript||'未识别到有效语句'" in page
    assert ".library,.sequence{min-height:0;overflow:hidden}" in page
    assert "grid-template-rows:minmax(140px,.45fr) minmax(0,1.55fr)" in page


def test_configured_hints_parse_plain_spoken_multitask_order(tmp_path):
    catalog = tmp_path / "tasks.json"
    catalog.write_text(json.dumps({
        "version": 1,
        "task_locations": {
            "迎宾区": [{"task": "迎宾接待", "hints": ["有客人来了"]}],
            "前台区": [{"task": "拿水", "hints": ["整瓶水"]}],
            "电视区": [{"task": "垃圾清理", "hints": ["垃圾处理掉"]}],
            "洗衣区": [{"task": "送洗衣篮", "hints": ["篮子拿去洗"]}],
        },
    }, ensure_ascii=False), encoding="utf-8")
    planner = TaskPlanner(catalog)

    tasks = planner.tasks_from_hints(
        "有客人来了，然后给我整瓶水，再把垃圾处理掉，最后把篮子拿去洗"
    )
    assert [(item["location"], item["task"]) for item in tasks] == [
        ("迎宾区", "迎宾接待"),
        ("前台区", "拿水"),
        ("电视区", "垃圾清理"),
        ("洗衣区", "送洗衣篮"),
    ]


def test_configured_hints_respect_negation_and_wrong_explicit_location(tmp_path):
    catalog = tmp_path / "tasks.json"
    catalog.write_text(json.dumps({
        "version": 1,
        "task_locations": {
            "前台区": [{"task": "拿水", "hints": ["拿瓶水"]}],
            "电视区": [{"task": "垃圾清理", "hints": ["丢垃圾"]}],
        },
    }, ensure_ascii=False), encoding="utf-8")
    planner = TaskPlanner(catalog)

    negated = planner.tasks_from_hints("不用拿瓶水，然后丢垃圾")
    assert [(item["location"], item["task"]) for item in negated] == [
        ("电视区", "垃圾清理")
    ]
    assert planner.tasks_from_hints("去电视区拿瓶水") == []


def test_catalog_hot_reload_rebuilds_hint_parser(tmp_path):
    catalog = tmp_path / "tasks.json"
    catalog.write_text(json.dumps({
        "version": 1,
        "task_locations": {
            "前台区": [{"task": "拿水", "hints": ["拿水"]}],
        },
    }, ensure_ascii=False), encoding="utf-8")
    server = _server(catalog)
    assert server.planner.tasks_from_hints("送洗") == []

    time.sleep(0.002)
    catalog.write_text(json.dumps({
        "version": 2,
        "task_locations": {
            "洗衣区": [{"task": "送洗衣篮", "hints": ["送洗"]}],
        },
    }, ensure_ascii=False), encoding="utf-8")
    assert server.reload_catalog()
    parsed = server.planner.tasks_from_hints("把这个送洗")
    assert [(item["location"], item["task"]) for item in parsed] == [
        ("洗衣区", "送洗衣篮")
    ]


def test_package_task_catalog_has_editable_hints_for_every_task():
    catalog = json.loads((PACKAGE_ROOT / "config" / "task_actions.json").read_text(
        encoding="utf-8"
    ))
    entries = [
        entry
        for location_entries in catalog["task_locations"].values()
        for entry in location_entries
    ]
    assert entries
    assert all(isinstance(entry.get("hints"), list) and entry["hints"] for entry in entries)


def test_package_hints_cover_hotel_plain_language_chain():
    planner = TaskPlanner(PACKAGE_ROOT / "config" / "task_actions.json")
    parsed = planner.tasks_from_hints(
        "给我去迎宾区去接待，然后丢一下垃圾，然后把毛巾放进洗衣篮，"
        "然后再把洗衣篮里的毛巾拿去洗衣房洗了"
    )
    assert [(item["location"], item["task"]) for item in parsed] == [
        ("迎宾区", "迎宾接待"),
        ("电视区", "垃圾清理"),
        ("毛巾区", "毛巾放入洗衣篮"),
        ("洗衣区", "篮子送到洗衣房桌面"),
    ]


def test_package_hints_preserve_delivery_garbage_water_spoken_order():
    planner = TaskPlanner(PACKAGE_ROOT / "config" / "task_actions.json")
    text = "先帮我去配送区配送物品，然后再帮我丢个垃圾，然后再帮我拿一瓶水。"
    parsed = planner.tasks_from_hints(text)
    expected = [
        ("配送区", "放置配送物品"),
        ("电视区", "垃圾清理"),
        ("前台区", "拿水"),
    ]
    assert [(item["location"], item["task"]) for item in parsed] == expected

    # Configured hints are the ordered ground truth even if the model returns
    # every valid task in a different order.
    wrong_model_order = [
        {"location": "前台区", "task": "拿水"},
        {"location": "配送区", "task": "放置配送物品"},
        {"location": "电视区", "task": "垃圾清理"},
    ]
    merged = planner.merge_hint_and_model_tasks(text, wrong_model_order)
    assert [(item["location"], item["task"]) for item in merged] == expected


def test_configured_hints_remain_available_when_model_fails(tmp_path):
    catalog = tmp_path / "tasks.json"
    catalog.write_text(json.dumps({
        "version": 1,
        "task_locations": {
            "前台区": [{"task": "拿水", "hints": ["整瓶水"]}],
        },
    }, ensure_ascii=False), encoding="utf-8")
    planner = TaskPlanner(catalog)

    class FailedModel:
        async def chat(self, *_args, **_kwargs):
            return SimpleNamespace(success=False, content="", error="offline")

    planner._llm = FailedModel()
    result = asyncio.run(planner.plan("给我整瓶水"))
    assert result["tasks"] == [{"location": "前台区", "task": "拿水"}]
