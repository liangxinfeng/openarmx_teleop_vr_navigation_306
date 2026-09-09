"""Thread-safe, side-effect-free task dispatch state machine."""

from __future__ import annotations

import copy
import threading
import time


ACTIVE_STATES = {
    "preparing", "navigating", "navigation_failed",
    "adjusting_height", "awaiting_action",
}


def task_chain_templates(catalog):
    """Return validated task-chain templates from the shared task catalog."""
    capabilities = set()
    for location, entries in catalog.get("task_locations", {}).items():
        location = str(location).strip()
        for entry in entries:
            task = entry if isinstance(entry, str) else entry.get("task", "")
            task = str(task).strip()
            if location and task:
                capabilities.add((location, task))

    templates = []
    identifiers = set()
    for index, item in enumerate(catalog.get("task_templates", []), 1):
        if not isinstance(item, dict):
            raise ValueError(f"第{index}个预设任务链格式无效")
        identifier = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        if not identifier or not name or identifier in identifiers:
            raise ValueError(f"第{index}个预设任务链缺少唯一 id 或名称")
        tasks = []
        for task_index, task_item in enumerate(item.get("tasks", []), 1):
            if not isinstance(task_item, dict):
                raise ValueError(f"预设任务链“{name}”第{task_index}项格式无效")
            location = str(task_item.get("location", "")).strip()
            task = str(task_item.get("task", "")).strip()
            if (location, task) not in capabilities:
                raise ValueError(f"预设任务链“{name}”包含未知任务：{location} / {task}")
            tasks.append({"location": location, "task": task})
        if not tasks:
            raise ValueError(f"预设任务链“{name}”不能为空")
        identifiers.add(identifier)
        templates.append({
            "id": identifier,
            "name": name,
            "description": str(item.get("description", "")).strip(),
            "tasks": tasks,
            "task_count": len(tasks),
        })
    return templates


def navigation_terminal_outcome(state):
    """Normalize regular Nav2 and short-distance servo terminal states."""
    state = str(state or "").strip().lower()
    if state in {"succeeded", "succeeded_by_tolerance"} or state.endswith("_succeeded"):
        return "succeeded"
    if (
        state in {"failed", "rejected", "canceled", "cancelled"}
        or state.endswith("_failed")
        or state.endswith("_rejected")
        or state.endswith("_canceled")
        or state.endswith("_cancelled")
    ):
        return "failed"
    return None


class TaskWorkflow:
    def __init__(self, catalog: dict):
        self._lock = threading.RLock()
        self._catalog = {}
        for location, entries in catalog.get("task_locations", {}).items():
            for entry in entries:
                if isinstance(entry, str):
                    entry = {"task": entry, "body_height_level": 5}
                task = str(entry.get("task", "")).strip()
                if task and location:
                    self._catalog[(task, str(location).strip())] = max(
                        1, min(5, int(entry.get("body_height_level", 5)))
                    )
        self._version = 0
        self._state = "idle"
        self._source_text = ""
        self._tasks = []
        self._index = -1
        self._message = "等待网页端分发任务"
        self._updated_at = time.time()

    def _touch(self, message=None):
        self._version += 1
        self._updated_at = time.time()
        if message is not None:
            self._message = str(message)

    def submit(self, source_text: str, tasks: list[dict], allow_unlisted=False):
        with self._lock:
            if self._state in ACTIVE_STATES:
                raise RuntimeError("当前任务串仍在执行，请完成或取消后再分发新任务")
            accepted = []
            for item in tasks:
                task = str(item.get("task", "")).strip()
                location = str(item.get("location", "")).strip()
                level = self._catalog.get((task, location))
                if level is None and not allow_unlisted:
                    raise ValueError(f"任务不在能力库中：{location} / {task}")
                if level is None:
                    level = max(1, min(5, int(item.get("body_height_level", 5))))
                accepted.append({
                    "task": task,
                    "location": location,
                    "body_height_level": level,
                })
            if not accepted:
                raise ValueError("没有可分发的有效任务")
            self._source_text = str(source_text).strip()[:1000]
            self._tasks = accepted
            self._index = 0
            self._state = "pending_confirmation"
            self._touch(f"收到{len(accepted)}项任务，等待VR遥操者确认分发")
            return self.snapshot()

    def confirm(self, waypoint_names):
        with self._lock:
            if self._state != "pending_confirmation":
                raise RuntimeError("当前没有待确认的任务串")
            known = set(waypoint_names)
            missing = sorted({item["location"] for item in self._tasks} - known)
            if missing:
                raise ValueError("航点库缺少地点：" + "、".join(missing))
            self._state = "preparing"
            self._touch("任务分发已确认，正在准备第一个任务")
            return copy.deepcopy(self._tasks[self._index])

    def navigation_started(self):
        with self._lock:
            if self._state != "preparing":
                raise RuntimeError("任务当前不允许启动导航")
            self._state = "navigating"
            current = self._tasks[self._index]
            self._touch(f"正在前往{current['location']}")

    def navigation_failed(self, message):
        with self._lock:
            if self._state != "navigating":
                return False
            # Keep navigation failure distinct from an unrecoverable task
            # failure.  The operator can retry this same waypoint, or
            # explicitly skip it when the location is genuinely unreachable.
            self._state = "navigation_failed"
            self._touch(message or "导航失败")
            return True

    def retry_navigation_failure(self):
        """Prepare the current failed waypoint for another navigation attempt."""
        with self._lock:
            if self._state != "navigation_failed":
                raise RuntimeError("当前没有可重试的导航失败任务")
            current = copy.deepcopy(self._tasks[self._index])
            self._state = "preparing"
            self._touch(f"正在重新前往{current['location']}")
            return current

    def operator_stopped_navigation(self):
        """Keep an operator-interrupted mission on its current waypoint."""
        with self._lock:
            # A cancellation status can race the HTTP service response and may
            # already have changed navigating -> navigation_failed.
            if self._state not in {"navigating", "navigation_failed"}:
                return None
            current = copy.deepcopy(self._tasks[self._index])
            self._state = "navigation_failed"
            self._touch(f"前往{current['location']}已中断，可重试当前点位")
            return current

    def skip_navigation_failure(self):
        """Skip a waypoint that failed to navigate and continue the chain."""
        with self._lock:
            if self._state != "navigation_failed":
                raise RuntimeError("当前没有可跳过的导航失败任务")
            skipped = copy.deepcopy(self._tasks[self._index])
            if self._index + 1 >= len(self._tasks):
                self._state = "completed"
                self._touch("最后一个导航失败任务已跳过，任务流程结束")
                return "done", skipped, None
            self._index += 1
            self._state = "preparing"
            upcoming = copy.deepcopy(self._tasks[self._index])
            self._touch(f"已跳过未到达点位，准备前往{upcoming['location']}")
            return "next", skipped, upcoming

    def manual_relocalization_required(self, message=None):
        """Keep the mission retryable while the operator restores localization."""
        with self._lock:
            if not self._tasks:
                return False
            if self._state in {"completed", "cancelled"}:
                return False
            self._state = "pending_confirmation"
            self._touch(
                message
                or "自动重定位失败，请在导航界面进行手动重定位后再次确认任务"
            )
            return True

    def navigation_arrived(self, waypoint_name=""):
        with self._lock:
            if self._state != "navigating":
                return None
            current = self._tasks[self._index]
            if waypoint_name and waypoint_name != current["location"]:
                return None
            self._state = "adjusting_height"
            self._touch(f"已到达{current['location']}，正在确认任务执行条件")
            return copy.deepcopy(current)

    def height_adjusted(self):
        with self._lock:
            if self._state != "adjusting_height":
                return None
            current = copy.deepcopy(self._tasks[self._index])
            self._state = "awaiting_action"
            self._touch(f"已确认周围环境安全，开始执行{current['task']}")
            return current

    def height_failed(self, message):
        with self._lock:
            if self._state != "adjusting_height":
                return False
            self._state = "failed"
            self._touch(message or "任务执行条件确认失败")
            return True

    def complete_action(self):
        with self._lock:
            if self._state != "awaiting_action":
                raise RuntimeError("当前不是等待动作完成的阶段")
            completed = copy.deepcopy(self._tasks[self._index])
            if self._index + 1 >= len(self._tasks):
                self._state = "completed"
                self._touch("全部任务已完成")
                return "done", completed, None
            self._index += 1
            self._state = "preparing"
            upcoming = copy.deepcopy(self._tasks[self._index])
            self._touch(f"当前任务完成，准备前往{upcoming['location']}")
            return "next", completed, upcoming

    def cancel(self, message="任务串已取消"):
        with self._lock:
            self._state = "cancelled"
            self._touch(message)

    def current(self):
        with self._lock:
            if 0 <= self._index < len(self._tasks):
                return copy.deepcopy(self._tasks[self._index])
            return None

    def snapshot(self):
        with self._lock:
            current = (
                copy.deepcopy(self._tasks[self._index])
                if 0 <= self._index < len(self._tasks) else None
            )
            return {
                "version": self._version,
                "state": self._state,
                "source_text": self._source_text,
                "tasks": copy.deepcopy(self._tasks),
                "current_index": self._index,
                "current": current,
                "total": len(self._tasks),
                "message": self._message,
                "updated_at": self._updated_at,
            }
