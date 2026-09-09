"""Touch-friendly task orchestration web service for VR navigation."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
from pathlib import Path
import signal
import ssl
import sys
import threading
import time
import uuid
import wave

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from ament_index_python.packages import get_package_share_directory
import numpy as np

from .task_page import PAGE
from .task_planner import TaskPlanner


UI_VERSION = "20260828-task-console-v14-order-layout"
NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


class TaskVoiceServer:
    def __init__(self, catalog_path, dispatcher_url):
        self.catalog_path = Path(catalog_path)
        self.dispatcher_url = dispatcher_url.rstrip("/")
        suffix = "/api/tasks/submit"
        root = (
            self.dispatcher_url[:-len(suffix)]
            if self.dispatcher_url.endswith(suffix)
            else self.dispatcher_url.rsplit("/api/", 1)[0]
        )
        self.status_url = root + "/api/navigation/state"
        self.server_epoch = uuid.uuid4().hex
        self.plan_revision = 0
        self.latest_generated_plan = {}
        self.catalog_revision = ""
        self.catalog = {}
        self.catalog_tasks = []
        self.catalog_by_pair = {}
        self.planner = None
        self.asr = None
        self.asr_lock = threading.Lock()
        self._navigation_lock = asyncio.Lock()
        self._navigation_cache = {}
        self._navigation_cache_at = 0.0
        self.reload_catalog(force=True)

    @staticmethod
    def _task_signature(tasks):
        return tuple(
            (
                str(item.get("task", "")),
                str(item.get("location", "")),
                int(item.get("body_height_level", 5)),
            )
            for item in tasks or []
            if isinstance(item, dict)
        )

    def reload_catalog(self, force=False):
        """Reload the source catalog when it changes, without a rebuild."""
        stat = self.catalog_path.stat()
        revision = f"{stat.st_mtime_ns}-{stat.st_size}"
        if not force and revision == self.catalog_revision:
            return False
        raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        entries = []
        by_pair = {}
        for location, tasks in raw.get("task_locations", {}).items():
            location = str(location).strip()
            if not location or not isinstance(tasks, list):
                continue
            for entry in tasks:
                if isinstance(entry, str):
                    task, level = entry, 5
                elif isinstance(entry, dict):
                    task = entry.get("task", "")
                    level = entry.get("body_height_level", 5)
                else:
                    continue
                task = str(task).strip()
                if not task:
                    continue
                try:
                    level = max(1, min(5, int(level)))
                except (TypeError, ValueError):
                    level = 5
                item = {
                    "id": f"{location}::{task}",
                    "location": location,
                    "task": task,
                    "body_height_level": level,
                }
                entries.append(item)
                by_pair[(task, location)] = item
        if not entries:
            raise ValueError("任务库中没有有效的地点任务")
        previous_llm = getattr(self.planner, "_llm", None)
        planner = TaskPlanner(self.catalog_path)
        if previous_llm is not None:
            planner._llm = previous_llm
        self.planner = planner
        self.catalog = raw
        self.catalog_tasks = entries
        self.catalog_by_pair = by_pair
        self.catalog_revision = revision
        return True

    def catalog_snapshot(self):
        self.reload_catalog()
        return {
            "revision": self.catalog_revision,
            "version": self.catalog.get("version", 1),
            "height_levels": self.catalog.get("height_levels", {}),
            "tasks": [dict(item) for item in self.catalog_tasks],
        }

    def _normalise_catalog_tasks(self, tasks):
        self.reload_catalog()
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("请至少选择一个任务")
        if len(tasks) > 30:
            raise ValueError("单次任务不能超过30项")
        result = []
        for raw in tasks:
            if not isinstance(raw, dict):
                raise ValueError("任务卡片格式无效")
            pair = (
                str(raw.get("task", "")).strip(),
                str(raw.get("location", "")).strip(),
            )
            configured = self.catalog_by_pair.get(pair)
            if configured is None:
                raise ValueError(f"任务库中不存在：{pair[1]} / {pair[0]}")
            result.append({
                "task": configured["task"],
                "location": configured["location"],
                "body_height_level": configured["body_height_level"],
            })
        return result

    def _normalise_voice_tasks(self, tasks):
        self.reload_catalog()
        result = []
        for raw in tasks or []:
            if not isinstance(raw, dict):
                continue
            pair = (
                str(raw.get("task", "")).strip(),
                str(raw.get("location", "")).strip(),
            )
            configured = self.catalog_by_pair.get(pair)
            if configured is not None:
                result.append({
                    "task": configured["task"],
                    "location": configured["location"],
                    "body_height_level": configured["body_height_level"],
                })
        return result

    def _remember_generated_plan(self, text, tasks, origin="voice"):
        self.plan_revision += 1
        copied = [dict(item) for item in tasks]
        self.latest_generated_plan = {
            "version": f"generated-{self.plan_revision}",
            "server_epoch": self.server_epoch,
            "plan_revision": self.plan_revision,
            "origin": origin,
            "state": "pending_confirmation",
            "source_text": str(text or ""),
            "tasks": copied,
            "current_index": 0,
            "current": dict(copied[0]) if copied else {},
            "total": len(copied),
            "message": f"已生成{len(copied)}项任务，等待确认分发",
            "updated_at": time.time(),
        }
        return dict(self.latest_generated_plan)

    def _display_snapshot(self, workflow_snapshot):
        workflow = workflow_snapshot if isinstance(workflow_snapshot, dict) else {}
        generated = self.latest_generated_plan
        if not generated:
            return workflow
        if self._task_signature(generated.get("tasks", [])) == self._task_signature(
            workflow.get("tasks", [])
        ):
            return workflow
        return dict(generated)

    async def initialize(self):
        self.reload_catalog(force=True)
        await self.planner.initialize()
        if "/home/ubuntu/code/void-cog" not in sys.path:
            sys.path.insert(0, "/home/ubuntu/code/void-cog")
        from void_cog.services.asr.asr_sherpa import SherpaASRService

        self.asr = SherpaASRService()
        await asyncio.to_thread(self.asr._load_model)

    def recognize(self, payload):
        with wave.open(io.BytesIO(payload), "rb") as source:
            if source.getsampwidth() != 2:
                raise ValueError("仅支持16位PCM WAV")
            channels = source.getnchannels()
            rate = source.getframerate()
            samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        samples = samples.astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        with self.asr_lock:
            result = self.asr.recognize(samples, sample_rate=rate)
        text = str(result.text or "").strip()
        if not text:
            raise ValueError("没有识别到有效语音")
        return text

    async def _dispatch_tasks(self, text, tasks, origin):
        generated = self._remember_generated_plan(text, tasks, origin=origin)
        result = {
            "source_text": text,
            "tasks": [dict(item) for item in tasks],
            "server_epoch": self.server_epoch,
            "plan_revision": self.plan_revision,
            "generated_plan": generated,
            "task_dispatch": generated,
            "submitted": False,
        }
        payload = {
            "source_text": text,
            "tasks": [dict(item) for item in tasks],
            "replace_existing": True,
        }
        try:
            connector = TCPConnector(ssl=False)
            async with ClientSession(
                connector=connector, timeout=ClientTimeout(total=5.0)
            ) as session:
                async with session.post(self.dispatcher_url, json=payload) as response:
                    dispatched = await response.json()
                    if response.status >= 300:
                        result["dispatch_message"] = dispatched.get(
                            "message", "任务暂未进入执行队列"
                        )
                        return result
        except Exception as error:
            result["dispatch_message"] = str(error)
            return result
        snapshot = dispatched.get("task_dispatch", generated)
        result["tasks"] = snapshot.get("tasks", result["tasks"])
        result["task_dispatch"] = snapshot
        result["submitted"] = True
        return result

    async def plan_and_dispatch(self, text):
        self.reload_catalog()
        planned = await self.planner.plan(text)
        tasks = self._normalise_voice_tasks(planned.get("tasks", []))
        planned["server_epoch"] = self.server_epoch
        if not tasks:
            planned["tasks"] = []
            planned["submitted"] = False
            planned["no_tasks"] = True
            return planned
        result = await self._dispatch_tasks(str(text).strip(), tasks, "voice")
        result["reply"] = planned.get("reply", "")
        return result

    async def _navigation_payload(self, max_age=0.25):
        now = time.monotonic()
        if self._navigation_cache and now - self._navigation_cache_at <= max_age:
            return self._navigation_cache
        async with self._navigation_lock:
            now = time.monotonic()
            if self._navigation_cache and now - self._navigation_cache_at <= max_age:
                return self._navigation_cache
            connector = TCPConnector(ssl=False)
            async with ClientSession(
                connector=connector, timeout=ClientTimeout(total=2.0)
            ) as session:
                async with session.get(self.status_url) as response:
                    payload = await response.json()
                    if response.status >= 300:
                        raise RuntimeError(payload.get("message", "机器人任务状态读取失败"))
            self._navigation_cache = payload
            self._navigation_cache_at = time.monotonic()
            return payload

    async def state_payload(self):
        base = {
            "ui_version": UI_VERSION,
            "server_epoch": self.server_epoch,
            "plan_revision": self.plan_revision,
            "generated_plan": self.latest_generated_plan,
            "catalog": self.catalog_snapshot(),
        }
        try:
            payload = await self._navigation_payload()
            base.update({
                "online": True,
                "task_dispatch": self._display_snapshot(payload.get("task_dispatch", {})),
                "relocalization": payload.get("relocalization", {}),
                "pose_fresh": bool(payload.get("pose_fresh", False)),
            })
        except Exception as error:
            base.update({
                "online": False,
                "task_dispatch": self._display_snapshot({}),
                "error": str(error),
            })
        return base

    async def state_api(self, request):
        del request
        return web.json_response(await self.state_payload(), headers=NO_CACHE_HEADERS)

    async def task_status_api(self, request):
        return await self.state_api(request)

    async def current_plan_api(self, request):
        del request
        return web.json_response({
            "ui_version": UI_VERSION,
            "server_epoch": self.server_epoch,
            "plan_revision": self.plan_revision,
            "generated_plan": self.latest_generated_plan,
        }, headers=NO_CACHE_HEADERS)

    async def catalog_api(self, request):
        del request
        return web.json_response({
            "ui_version": UI_VERSION,
            "catalog": self.catalog_snapshot(),
        }, headers=NO_CACHE_HEADERS)

    async def events_api(self, request):
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        previous = None
        heartbeat_at = 0.0
        try:
            while True:
                payload = await self.state_payload()
                encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if encoded != previous:
                    await response.write(f"event: state\ndata: {encoded}\n\n".encode("utf-8"))
                    previous = encoded
                    heartbeat_at = time.monotonic()
                elif time.monotonic() - heartbeat_at >= 8.0:
                    await response.write(b": heartbeat\n\n")
                    heartbeat_at = time.monotonic()
                await asyncio.sleep(0.35)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        return response

    async def index(self, request):
        del request
        bootstrap = json.dumps(
            await self.state_payload(), ensure_ascii=False, separators=(",", ":")
        ).replace("</", "<\\/")
        return web.Response(
            text=PAGE.replace("__BOOTSTRAP_STATE__", bootstrap),
            content_type="text/html",
            headers={**NO_CACHE_HEADERS, "X-Task-UI-Version": UI_VERSION},
        )

    async def health(self, request):
        del request
        return web.json_response({
            "ok": True,
            "asr_ready": self.asr is not None,
            "ui_version": UI_VERSION,
            "server_epoch": self.server_epoch,
            "catalog_revision": self.catalog_revision,
        }, headers=NO_CACHE_HEADERS)

    async def plan_api(self, request):
        try:
            payload = await request.json()
            result = await self.plan_and_dispatch(payload.get("text", ""))
            print(
                f"文字任务：count={len(result.get('tasks', []))}, "
                f"submitted={result.get('submitted', False)}",
                flush=True,
            )
            return web.json_response(result)
        except Exception as error:
            print(f"文字任务处理未完成：{error}", flush=True)
            return web.json_response({"error": str(error)}, status=422)

    async def catalog_plan_api(self, request):
        try:
            payload = await request.json()
            tasks = self._normalise_catalog_tasks(payload.get("tasks", []))
            source = "卡片编排：" + " → ".join(
                f"{item['location']}·{item['task']}" for item in tasks
            )
            result = await self._dispatch_tasks(source, tasks, "catalog")
            print(
                f"卡片任务：count={len(tasks)}, submitted={result.get('submitted', False)}",
                flush=True,
            )
            return web.json_response(result)
        except (TypeError, ValueError) as error:
            return web.json_response({"error": str(error)}, status=422)
        except Exception as error:
            print(f"卡片任务处理未完成：{error}", flush=True)
            return web.json_response({"error": str(error)}, status=500)

    async def audio_api(self, request):
        text = ""
        try:
            payload = await request.read()
            if len(payload) > 12 * 1024 * 1024:
                raise ValueError("录音超过12MB")
            text = await asyncio.to_thread(self.recognize, payload)
            result = await self.plan_and_dispatch(text)
            result["transcript"] = text
            print(
                f"语音任务：count={len(result.get('tasks', []))}, "
                f"submitted={result.get('submitted', False)}",
                flush=True,
            )
            return web.json_response(result)
        except Exception as error:
            print(f"语音任务处理未完成：{error}", flush=True)
            return web.json_response(
                {"error": str(error), "transcript": text}, status=422
            )


async def run(args):
    package = Path(get_package_share_directory("openarmx_teleop_vr_navigation_306"))
    source_catalog = Path(
        "/home/ubuntu/ros2_ws/src/openarmx_teleop_vr_navigation_306/config/task_actions.json"
    )
    catalog = source_catalog if source_catalog.is_file() else package / "config" / "task_actions.json"
    server = TaskVoiceServer(catalog, args.dispatcher_url)
    await server.initialize()
    app = web.Application(client_max_size=12 * 1024 * 1024)
    app.router.add_get("/", server.index)
    app.router.add_get("/api/health", server.health)
    app.router.add_get("/api/state", server.state_api)
    app.router.add_get("/api/events", server.events_api)
    app.router.add_get("/api/task-status", server.task_status_api)
    app.router.add_get("/api/current-plan", server.current_plan_api)
    app.router.add_get("/api/catalog", server.catalog_api)
    app.router.add_post("/api/plan", server.plan_api)
    app.router.add_post("/api/catalog-plan", server.catalog_plan_api)
    app.router.add_post("/api/audio", server.audio_api)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    cert_dir = Path(args.certificate_directory).expanduser()
    for _ in range(100):
        if (cert_dir / "cert.pem").is_file() and (cert_dir / "key.pem").is_file():
            break
        await asyncio.sleep(0.1)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_dir / "cert.pem", cert_dir / "key.pem")
    await web.TCPSite(runner, args.host, args.port, ssl_context=context).start()
    print(f"触屏任务编排中心已启动：https://<机器人IP>:{args.port}", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(handled_signal, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--dispatcher-url", default="https://127.0.0.1:8445/api/tasks/submit"
    )
    parser.add_argument(
        "--certificate-directory", default="~/.ros/openarmx_teleop_vr_306_v4"
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
