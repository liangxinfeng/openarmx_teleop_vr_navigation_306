"""Sequential local speech output for task workflow events."""

from __future__ import annotations

import asyncio
import json
import queue
import threading

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import String


class TaskSpeechNode(Node):
    def __init__(self):
        super().__init__('openarmx_navigation_task_speech_306')
        self.declare_parameter(
            'input_topic', '/openarmx_teleop_vr_navigation_306/task_speech'
        )
        self.declare_parameter('volume', 1.5)
        self._queue = queue.Queue(maxsize=24)
        self._stop = threading.Event()
        self.create_subscription(
            String, str(self.get_parameter('input_topic').value), self._on_text, 10
        )
        self._worker = threading.Thread(
            target=self._speech_worker, name='task-speech', daemon=True
        )
        self._worker.start()

    def _on_text(self, message):
        try:
            payload = json.loads(message.data)
            text = str(payload.get('text', '')).strip()
        except (TypeError, ValueError, json.JSONDecodeError):
            text = str(message.data).strip()
        if not text:
            return
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            self.get_logger().warning('任务语音队列已满，丢弃过期提示')

    def _speech_worker(self):
        try:
            from autolife_s2_voice.run.tts.tts_factory import create_tts_engine

            engine = create_tts_engine(
                engine='matcha', playback='auto',
                volume=float(self.get_parameter('volume').value),
            )
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.get_logger().info(
                f'任务语音已就绪：{engine.engine_name} / {engine.playback_mode}'
            )
            while not self._stop.is_set():
                try:
                    text = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if text is None:
                    break
                try:
                    loop.run_until_complete(engine.speak(text))
                except Exception as error:
                    self.get_logger().error(f'任务语音播报失败：{error}')
            loop.close()
        except Exception as error:
            self.get_logger().error(f'任务语音初始化失败：{error}')

    def destroy_node(self):
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=2.0)
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = TaskSpeechNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
