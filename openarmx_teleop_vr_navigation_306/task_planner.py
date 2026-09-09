"""Natural-language task planner constrained by the package task catalog."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys

import yaml


PAIR_PATTERN = re.compile(
    r"\[TASK:\s*(.+?)\s*\]\s*\[LOCATION:\s*(.+?)\s*\]"
)
CLAUSE_SEPARATOR = re.compile(
    r"(?:然后|接着|随后|紧接着|最后|再去|再把|再|之后|并且|，|。|；|,|;)"
)
NEGATION_PATTERN = re.compile(r"(?:不要|不用|不需要|别去|别做|取消|先不|我自己|我来做)")


class TaskPlanner:
    def __init__(self, catalog_path, void_cog_root="/home/ubuntu/code/void-cog"):
        self.catalog_path = Path(catalog_path)
        self.void_cog_root = Path(void_cog_root)
        self.catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        self.tasks = []
        for location, entries in self.catalog.get("task_locations", {}).items():
            for entry in entries:
                task = entry if isinstance(entry, str) else entry.get("task", "")
                if str(task).strip():
                    hints = [] if isinstance(entry, str) else entry.get("hints", [])
                    if not isinstance(hints, list):
                        hints = []
                    hints = [
                        str(hint).strip() for hint in hints if str(hint).strip()
                    ]
                    # Task names are always valid exact hints.  Everything else
                    # stays data-driven in task_actions.json.
                    task = str(task).strip()
                    self.tasks.append({
                        "task": task,
                        "location": str(location).strip(),
                        "hints": list(dict.fromkeys([task, *hints])),
                    })
        self._pairs = {(item["task"], item["location"]) for item in self.tasks}
        self._locations = {
            item["location"] for item in self.tasks if item["location"]
        }
        self._llm = None

    async def initialize(self):
        root = str(self.void_cog_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        from void_cog.services.llm import create_llm_service

        config_path = self.void_cog_root / "config" / "default.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        self._llm = create_llm_service(config, provider="openai_compat")

    def _prompt(self):
        table = "\n".join(
            (
                f"- 地点：{item['location']}；任务：{item['task']}；"
                f"用户常用说法：{'、'.join(item['hints'])}"
            )
            for item in self.tasks
        )
        return f"""你是服务机器人的任务规划器。只根据用户真实意图，从能力表拆分任务。

【能力表】
{table}

【输出】
先用一句简短中文确认；然后严格按用户顺序输出零个或多个：
[TASK:能力表任务][LOCATION:能力表地点]

【规则】
1. 只提取明确要求机器人做的事；用户自己做、提问、否定或取消的事不执行。
2. 保留“先、然后、再、最后”的顺序和明确的重复次数，不合并、不漏项。
3. 拿取/取出与送到/放置方向相反，必须按动词选择；不唯一时追问且不输出任务标记。
4. 每个任务只能绑定能力表中的原地点。用户明确说了错误地点时必须拒绝或追问，不能偷偷改到正确地点，也不能换任务；同句其他合法任务照常输出。
5. 用户没说地点时，才可补全能力表中该任务的唯一地点。能力表外需求不输出标记。
6. 标记必须放在回复末尾，任务与地点逐项成对出现。
7. “用户常用说法”来自可热更新的任务库。用户不必逐字说出提示词，语义相同也应映射到对应任务；不得映射到其他任务。

【关键示例】
用户：把这个包裹送到存放物品的地方
输出：好的，我把包裹送过去放好。[TASK:放置配送物品][LOCATION:配送区]

用户：去物品区把要配送的盒子夹起来
输出：好的，我去把盒子夹起来。[TASK:夹取配送物品][LOCATION:物品区]

用户：去电视区给我拿杯水
输出：拿水只能在前台区执行，请确认是否改去前台区。

用户：我自己去拿水，你不用过去
输出：好的，有需要再叫我。
"""

    @staticmethod
    def _clauses(text):
        """Yield non-empty command clauses with their source positions."""
        start = 0
        for separator in CLAUSE_SEPARATOR.finditer(text):
            clause = text[start:separator.start()].strip()
            if clause:
                yield start, clause
            start = separator.end()
        clause = text[start:].strip()
        if clause:
            yield start, clause

    def tasks_from_hints(self, text):
        """Resolve configured natural phrases without hard-coded task names.

        This deterministic layer is intentionally conservative: a clause with
        a negation is ignored, and an explicitly named configured location must
        match the task's configured location.  Multiple clauses preserve the
        speaker's order and can repeat the same task.
        """
        text = str(text or "").strip()
        matches = []
        for clause_offset, clause in self._clauses(text):
            if NEGATION_PATTERN.search(clause):
                continue
            explicit_locations = {
                location for location in self._locations if location in clause
            }
            clause_matches = []
            for item in self.tasks:
                if explicit_locations and item["location"] not in explicit_locations:
                    continue
                best = None
                for hint in item["hints"]:
                    position = clause.find(hint)
                    if position < 0:
                        continue
                    candidate = (position, -len(hint))
                    if best is None or candidate < best:
                        best = candidate
                if best is not None:
                    clause_matches.append((
                        clause_offset + best[0],
                        best[1],
                        item["task"],
                        item["location"],
                    ))
            # One pair may have several overlapping hints; task entries are
            # unique, so sorting is enough to retain all distinct tasks in a
            # clause such as “拿水和丢垃圾”.
            clause_matches.sort()
            matches.extend(clause_matches)
        matches.sort()
        return [
            {"task": task, "location": location, "_position": position}
            for position, _negative_length, task, location in matches
        ]

    def _estimated_position(self, text, task, location):
        item = next(
            (
                candidate for candidate in self.tasks
                if candidate["task"] == task and candidate["location"] == location
            ),
            None,
        )
        phrases = [location, task, *(item or {}).get("hints", [])]
        positions = [text.find(phrase) for phrase in phrases if phrase and phrase in text]
        return min(positions) if positions else len(text) + 1000

    def merge_hint_and_model_tasks(self, text, model_tasks):
        """Use configured hints as ordered ground truth and let the model fill gaps."""
        hinted = self.tasks_from_hints(text)
        if not hinted:
            return model_tasks
        merged = [dict(item) for item in hinted]
        available = {}
        for item in hinted:
            pair = (item["task"], item["location"])
            available[pair] = available.get(pair, 0) + 1
        used = {}
        for order, item in enumerate(model_tasks):
            pair = (item["task"], item["location"])
            used[pair] = used.get(pair, 0) + 1
            if used[pair] <= available.get(pair, 0):
                continue
            merged.append({
                **item,
                "_position": self._estimated_position(text, *pair),
                "_model_order": order,
            })
        merged.sort(key=lambda item: (
            int(item.get("_position", len(text) + 1000)),
            int(item.get("_model_order", -1)),
        ))
        return [
            {"task": item["task"], "location": item["location"]}
            for item in merged
        ]

    async def plan(self, text):
        text = str(text).strip()
        if not text:
            raise ValueError("指令不能为空")
        hinted = self.tasks_from_hints(text)
        configured_fallback = [
            {"task": item["task"], "location": item["location"]}
            for item in hinted
        ]
        try:
            if self._llm is None:
                await self.initialize()
            response = await self._llm.chat(
                [
                    {"role": "system", "content": self._prompt()},
                    {"role": "user", "content": text},
                ],
                temperature=0.1,
                max_tokens=512,
            )
        except Exception:
            if configured_fallback:
                return {
                    "source_text": text,
                    "reply": "已根据任务库提示词完成编排。",
                    "tasks": configured_fallback,
                }
            raise
        if not response.success or not response.content:
            if configured_fallback:
                return {
                    "source_text": text,
                    "reply": "已根据任务库提示词完成编排。",
                    "tasks": configured_fallback,
                }
            raise RuntimeError(response.error or "任务规划模型没有返回内容")
        raw = response.content.strip()
        pairs = PAIR_PATTERN.findall(raw)
        explicit_locations = {
            item["location"] for item in self.tasks if item["location"] in text
        }
        single_explicit = (
            next(iter(explicit_locations))
            if len(pairs) == 1 and len(explicit_locations) == 1 else None
        )
        model_tasks = []
        for task, location in pairs:
            task, location = task.strip(), location.strip()
            if (task, location) not in self._pairs:
                continue
            if single_explicit is not None and location != single_explicit:
                continue
            model_tasks.append({"task": task, "location": location})
        tasks = self.merge_hint_and_model_tasks(text, model_tasks)
        reply = re.sub(r"\[(?:TASK|LOCATION):\s*.+?\s*\]", "", raw).strip()
        return {"source_text": text, "reply": reply, "tasks": tasks}
