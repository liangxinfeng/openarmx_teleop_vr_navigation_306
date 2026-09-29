#!/usr/bin/env python3
"""One-shot probe: run the DeliveryHandoffMonitor against the real config
for ~10 s and print what the VR side would see.  Read-only against the ops
server (a couple of GET polls); run on the robot with the deployed module."""

import json
import sys
import time

sys.path.insert(0, '/home/ubuntu/ros2_ws/src/openarmx_teleop_vr_navigation_306')

from openarmx_teleop_vr_navigation_306.delivery_handoff import (  # noqa: E402
    DeliveryHandoffMonitor,
)

CONFIG = (
    '/home/ubuntu/ros2_ws/src/openarmx_teleop_vr_navigation_306'
    '/config/delivery_handoff.json'
)


class PrintLogger:
    def info(self, text):
        print(f'[info] {text}')

    def warning(self, text):
        print(f'[warn] {text}')


def main():
    monitor = DeliveryHandoffMonitor(CONFIG, PrintLogger())
    monitor.start()
    try:
        for round_index in range(4):
            time.sleep(2.5)
            print(
                f'--- t+{(round_index + 1) * 2.5:.1f}s ---\n'
                + json.dumps(monitor.snapshot(), ensure_ascii=False, indent=2)
            )
    finally:
        monitor.stop()


if __name__ == '__main__':
    main()
