#!/usr/bin/env python3
"""Software-level acceptance tests for DeliveryHandoffMonitor.

Runs directly on the robot (python3, stdlib only).  A local mock plays the
hotel ops /api/ops server speaking the 2026-09-23 handoffs[] contract; the
monitor polls it through the same config hot-reload path used in
production.  The mock is on 127.0.0.1, which the URL guard only permits
because the test config sets allow_lan_addresses."""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

PACKAGE_PARENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_PARENT))

from openarmx_teleop_vr_navigation_306.delivery_handoff import (  # noqa: E402
    DeliveryHandoffMonitor,
    HandoffConfigError,
    _target_url,
)

HOST = '127.0.0.1'
PORT = 8907
HOTEL = 'TESTHOTEL'
BASE = f'/api/ops/hotels/{HOTEL}'
GET_PATH = f'{BASE}/humanoid-handoff'
OPEN_PATH = f'{GET_PATH}/open'
WASH_PATH = f'{BASE}/wash-complete'
DRY_PATH = f'{BASE}/dry-complete'


class ConsoleLogger:
    def info(self, text):
        print(f'[info] {text}')

    def warning(self, text):
        print(f'[warn] {text}')


class MockOpsState:
    def __init__(self):
        self.lock = threading.Lock()
        self.orders = []          # handoffs[] payload
        self.get_delay = 0.0
        self.open_delay = 0.0
        self.open_failure = None  # (http_status, code, message)
        self.wash_washed = set()  # order_ids that passed wash-complete
        self.action_failure = None  # (path_suffix, http_status, code, message)
        self.get_count = 0
        self.open_count = 0
        self.wash_count = 0
        self.dry_count = 0
        self.get_inflight = 0
        self.get_max_inflight = 0
        self.open_bodies = []
        self.wash_bodies = []
        self.dry_bodies = []


MOCK = MockOpsState()


def make_order(order_id, **fields):
    order = {
        'order_id': order_id,
        'room': fields.get('room'),
        'status': fields.get('status'),
        'arrived': fields.get('arrived', False),
        'task_type': fields.get('task_type'),
        'task_id': fields.get('task_id'),
        'door_id': fields.get('door_id'),
        'opened': fields.get('opened', False),
        'closed': fields.get('closed', False),
    }
    return {key: value for key, value in order.items() if value is not None}


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != GET_PATH:
            self._json(404, {'message': 'not found'})
            return
        with MOCK.lock:
            MOCK.get_count += 1
            MOCK.get_inflight += 1
            MOCK.get_max_inflight = max(MOCK.get_max_inflight, MOCK.get_inflight)
            payload = {'handoffs': [dict(order) for order in MOCK.orders]}
            delay = MOCK.get_delay
        if delay:
            time.sleep(delay)
        with MOCK.lock:
            MOCK.get_inflight -= 1
        self._json(200, payload)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length) or b'{}')
        with MOCK.lock:
            failure = MOCK.action_failure
            washed = set(MOCK.wash_washed)
        if failure is not None:
            path_suffix, status, code, message = failure
            if self.path.endswith(path_suffix):
                self._json(status, {
                    'code': code, 'message': f'{code} {message}'.strip(),
                })
                return
        if self.path == WASH_PATH:
            with MOCK.lock:
                MOCK.wash_count += 1
                MOCK.wash_bodies.append(body)
                MOCK.wash_washed.add(str(body.get('order_id')))
            self._json(200, {
                'order_id': body.get('order_id'), 'status': 'WASHING',
                'wash_completed_at': '2026-09-23T12:00:00.000Z',
                'laundry_completed_at': None,
            })
            return
        if self.path == DRY_PATH:
            order_id = str(body.get('order_id'))
            if order_id not in washed:
                self._json(409, {
                    'code': 'DRY_COMPLETE_NOT_READY',
                    'message': 'DRY_COMPLETE_NOT_READY 尚未洗衣完成，不能烘干完成',
                })
                return
            with MOCK.lock:
                MOCK.dry_count += 1
                MOCK.dry_bodies.append(body)
            self._json(200, {
                'order_id': order_id, 'status': 'WASHING',
                'laundry_completed_at': '2026-09-23T12:30:00.000Z',
            })
            return
        if self.path != OPEN_PATH:
            self._json(404, {'message': 'not found'})
            return
        with MOCK.lock:
            MOCK.open_count += 1
            MOCK.open_bodies.append(body)
            delay = MOCK.open_delay
            failure = MOCK.open_failure
        if delay:
            time.sleep(delay)
        if failure is not None:
            status, code, message = failure
            self._json(status, {
                'code': code, 'message': f'{code} {message}'.strip(),
            })
            return
        self._json(200, {
            'order_id': body.get('order_id'), 'action': 'open',
            'result': 'applied', 'task_id': body.get('task_id'),
            'door_id': body.get('door_id'), 'idempotent': False,
        })


def write_config(path, url, **extra):
    config = {
        'base_url': url,
        'hotel_id': HOTEL,
        'poll_interval_seconds': extra.pop('poll', 0.5),
        'request_timeout_seconds': extra.pop('timeout', 3.0),
        'allow_lan_addresses': extra.pop('allow_lan', True),
    }
    config.update(extra)
    path.write_text(json.dumps(config, indent=2), encoding='utf-8')


def wait_for(predicate, timeout=8.0, description='condition'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError(f'timed out waiting for {description}')


RESULTS = []


def check(name, condition, detail=''):
    RESULTS.append((name, bool(condition)))
    print(f'{"PASS" if condition else "FAIL"}  {name}  {detail}')


def main():
    import os
    os.environ['http_proxy'] = 'http://127.0.0.1:1'
    os.environ['https_proxy'] = 'http://127.0.0.1:1'
    os.environ['all_proxy'] = 'http://127.0.0.1:1'

    server = ThreadingHTTPServer((HOST, PORT), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        _target_url('file:///etc/passwd', HOTEL, False)
        check('guard: file scheme rejected', False)
    except HandoffConfigError:
        check('guard: file scheme rejected', True)
    try:
        _target_url('http://localhost:3000', HOTEL, False)
        check('guard: localhost rejected', False)
    except HandoffConfigError:
        check('guard: localhost rejected', True)
    try:
        url = _target_url(f'http://{HOST}:{PORT}', HOTEL, True)
        check(
            'guard: allow_lan permits loopback and builds path',
            url == f'http://{HOST}:{PORT}{GET_PATH}',
            url,
        )
    except HandoffConfigError as error:
        check('guard: allow_lan permits loopback and builds path', False, str(error))
    try:
        _target_url(f'http://{HOST}:{PORT}', '../evil', True)
        check('guard: hotel_id traversal rejected', False)
    except HandoffConfigError:
        check('guard: hotel_id traversal rejected', True)

    with TemporaryDirectory() as temp:
        config_path = Path(temp) / 'delivery_handoff.json'
        write_config(config_path, f'http://{HOST}:{PORT}')
        monitor = DeliveryHandoffMonitor(config_path, ConsoleLogger())
        monitor.start()

        # Case 1: no active orders.
        wait_for(lambda: monitor.snapshot()['state'] == 'no_task',
                 description='no_task')
        snap = monitor.snapshot()
        check(
            'case1 no orders: no error, cannot open',
            snap['state'] == 'no_task' and snap['can_open'] is False
            and snap['orders'] == [],
            json.dumps(snap, ensure_ascii=False),
        )
        check(
            'ambient shell proxy env is ignored',
            snap['last_success_age'] is not None and MOCK.get_count >= 1,
            f'gets={MOCK.get_count}',
        )

        # Case 2: delivering.
        with MOCK.lock:
            MOCK.orders = [make_order(
                'ORD1', room='2727', status='PICKUP_STARTED',
                task_type='PICKUP_FROM_ROOM',
                task_id='ORD1-PICKUP_FROM_ROOM',
            )]
        wait_for(lambda: monitor.snapshot()['state'] == 'delivering',
                 description='delivering')
        snap = monitor.snapshot()
        success, message = monitor.request_open()
        check(
            'case2 delivering: open denied without POST',
            snap['can_open'] is False and success is False and MOCK.open_count == 0,
            message,
        )
        check(
            'case2 order fields exposed',
            snap['order_id'] == 'ORD1' and snap['room_no'] == '2727'
            and snap['status_code'] == 'PICKUP_STARTED'
            and snap['status_label'] == '已接单 · 前往客房',
            json.dumps(snap, ensure_ascii=False),
        )

        # Case 3: arrived but door not ready.
        with MOCK.lock:
            MOCK.orders[0]['status'] = 'PICKUP_AT_LAUNDRY'
            MOCK.orders[0]['arrived'] = True
        wait_for(lambda: monitor.snapshot()['state'] == 'arrived_wait_door',
                 description='arrived_wait_door')
        success, message = monitor.request_open()
        check(
            'case3 arrived/no door: open denied',
            monitor.snapshot()['can_open'] is False and success is False
            and MOCK.open_count == 0,
            monitor.snapshot()['label'],
        )

        # Case 4: ready to open -> open succeeds with the order identity.
        with MOCK.lock:
            MOCK.orders[0]['door_id'] = '2'
            MOCK.open_delay = 0.6
        wait_for(lambda: monitor.snapshot()['state'] == 'ready_to_open',
                 description='ready_to_open')
        snap = monitor.snapshot()
        check(
            'case4 ready: can_open with server door and room',
            snap['can_open'] is True and snap['door_id'] == '2'
            and snap['room_no'] == '2727' and len(snap['orders']) == 1,
            json.dumps(snap, ensure_ascii=False),
        )
        success, message = monitor.request_open()
        wait_for(lambda: monitor.snapshot()['state'] == 'opened',
                 description='opened')
        snap = monitor.snapshot()
        check(
            'case4 open success: body carries order_id+task_id+door',
            success is True and MOCK.open_count == 1
            and MOCK.open_bodies[-1] == {
                'order_id': 'ORD1',
                'task_id': 'ORD1-PICKUP_FROM_ROOM',
                'door_id': '2',
            }
            and snap['opened_at'] is not None and '尽快' in message,
            message,
        )
        time.sleep(1.2)
        check(
            'case4 opened sticky while task unchanged',
            monitor.snapshot()['state'] == 'opened'
            and monitor.snapshot()['can_open'] is False,
            monitor.snapshot()['state'],
        )
        again_ok, _ = monitor.request_open()
        check(
            'case4 repeat open is idempotent, no extra POST',
            again_ok is True and MOCK.open_count == 1,
        )

        # Case 6: order switch clears old open state.
        with MOCK.lock:
            MOCK.orders = [make_order(
                'ORD2', room='2801', status='PICKUP_AT_LAUNDRY', arrived=True,
                task_type='PICKUP_FROM_ROOM',
                task_id='ORD2-PICKUP_FROM_ROOM', door_id='3',
            )]
        wait_for(
            lambda: monitor.snapshot()['state'] == 'ready_to_open'
            and monitor.snapshot()['order_id'] == 'ORD2',
            description='primary order switch',
        )
        snap = monitor.snapshot()
        check(
            'case6 order switch clears old open state',
            snap['opened_at'] is None and snap['door_id'] == '3'
            and snap['room_no'] == '2801',
            json.dumps(snap, ensure_ascii=False),
        )

        # Case 5: duplicate input during a slow open -> single POST.
        opens_before = MOCK.open_count
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(monitor.request_open()))
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wait_for(lambda: monitor.snapshot()['state'] == 'opened',
                 description='opened ORD2')
        check(
            'case5 duplicate input: exactly one POST, one success',
            MOCK.open_count == opens_before + 1
            and [ok for ok, _ in results].count(True) == 1,
            f'opens={MOCK.open_count}',
        )

        # Case 8: 409 handling maps the server code.
        with MOCK.lock:
            MOCK.orders = [make_order(
                'ORD3', room='2802', status='PICKUP_AT_LAUNDRY', arrived=True,
                task_type='PICKUP_FROM_ROOM',
                task_id='ORD3-PICKUP_FROM_ROOM', door_id='1',
            )]
            MOCK.open_failure = (409, 'HUMANOID_HANDOFF_NOT_ARRIVED', '')
        wait_for(
            lambda: monitor.snapshot()['order_id'] == 'ORD3'
            and monitor.snapshot()['state'] == 'ready_to_open',
            description='order switch to ORD3',
        )
        success, message = monitor.request_open()
        check(
            'case8 409 mapped: friendly message, retryable state',
            success is False and message == '配送机器人尚未到达'
            and monitor.snapshot()['state'] == 'ready_to_open',
            f'message={message}',
        )
        with MOCK.lock:
            MOCK.open_failure = None
        time.sleep(0.8)
        success, message = monitor.request_open()
        check('case8 retry after 409 can succeed', success is True)

        # Wash/dry-complete against the 0923 contract.
        with MOCK.lock:
            MOCK.orders = [make_order(
                'ORD4', room='2810', status='WASHING', arrived=False,
            )]
        wait_for(lambda: monitor.snapshot()['order_id'] == 'ORD4',
                 description='washing order')
        snap = monitor.snapshot()
        check(
            'washing order: state washing, no task, cannot open',
            snap['state'] == 'washing' and snap['task_id'] is None
            and snap['can_open'] is False,
            json.dumps(snap, ensure_ascii=False),
        )
        success, message = monitor.request_dry_complete()
        check(
            'dry before wash: 409 mapped, nothing sent',
            success is False and '洗衣完成' in message and MOCK.dry_count == 0,
            message,
        )
        success, message = monitor.request_wash_complete()
        snap = monitor.snapshot()
        check(
            'wash-complete: order_id body, frees slot, no dry',
            success is True and MOCK.wash_count == 1
            and MOCK.wash_bodies == [{'order_id': 'ORD4'}]
            and snap['wash_complete_sent_at'] is not None
            and snap['dry_complete_sent_at'] is None
            and '烘干' in message,
            message,
        )
        success, message = monitor.request_wash_complete()
        check(
            'wash-complete repeat accepted (idempotent)',
            success is True and MOCK.wash_count == 2,
        )
        success, message = monitor.request_dry_complete()
        snap = monitor.snapshot()
        check(
            'dry-complete: order_id body, waiting window recorded',
            success is True and MOCK.dry_count == 1
            and MOCK.dry_bodies == [{'order_id': 'ORD4'}]
            and snap['dry_complete_sent_at'] is not None
            and snap['dry_complete_order'] == 'ORD4' and '等待' in message,
            message,
        )

        # Two concurrent orders: primary is the actionable one; wash/dry
        # still target the WASHING order.
        with MOCK.lock:
            MOCK.orders = [
                make_order('ORD5', room='2811', status='WASHING', arrived=False),
                make_order(
                    'ORD6', room='2812', status='PICKUP_AT_LAUNDRY', arrived=True,
                    task_type='PICKUP_FROM_ROOM',
                    task_id='ORD6-PICKUP_FROM_ROOM', door_id='5',
                ),
            ]
        wait_for(
            lambda: monitor.snapshot()['order_id'] == 'ORD6'
            and monitor.snapshot()['state'] == 'ready_to_open',
            description='two orders, actionable primary',
        )
        snap = monitor.snapshot()
        check(
            'two orders: primary is actionable, list kept',
            snap['door_id'] == '5' and len(snap['orders']) == 2
            and snap['room_no'] == '2812',
            json.dumps(snap['orders'], ensure_ascii=False),
        )
        success, message = monitor.request_wash_complete()
        check(
            'two orders: wash targets the WASHING order',
            success is True and MOCK.wash_bodies[-1] == {'order_id': 'ORD5'},
            f'wash_bodies={MOCK.wash_bodies[-1]}',
        )

        # Phase labels move with the primary order.
        with MOCK.lock:
            MOCK.orders = [make_order(
                'ORD7', room='2820', status='PICKUP_TO_LAUNDRY',
                task_type='PICKUP_FROM_ROOM',
                task_id='ORD7-PICKUP_FROM_ROOM',
            )]
        wait_for(
            lambda: monitor.snapshot()['status_code'] == 'PICKUP_TO_LAUNDRY',
            description='PICKUP_TO_LAUNDRY',
        )
        check(
            'phase: mid-transit label',
            monitor.snapshot()['status_label'] == '前来洗衣房',
        )
        with MOCK.lock:
            MOCK.orders[0]['status'] = 'WASHING'
            MOCK.orders[0]['task_id'] = None
            MOCK.orders[0]['task_type'] = None
        wait_for(lambda: monitor.snapshot()['status_code'] == 'WASHING',
                 description='WASHING')
        check(
            'phase: WASHING has no outbound label',
            monitor.snapshot()['status_label'] is None,
        )

        # Case 7: network failure -> unknown, then recovery.
        write_config(config_path, f'http://{HOST}:1', poll=0.5)
        wait_for(lambda: monitor.snapshot()['state'] == 'unknown',
                 timeout=10.0, description='unknown after outage')
        snap = monitor.snapshot()
        check(
            'case7 outage: unknown state distinct from arrived=false',
            snap['state'] == 'unknown' and '连接失败' in snap['detail'],
            snap['detail'],
        )
        write_config(config_path, f'http://{HOST}:{PORT}', poll=0.5)
        wait_for(
            lambda: monitor.snapshot()['state'] in ('washing', 'no_task'),
            timeout=10.0, description='recovery after config restore',
        )
        check(
            'case7 recovery: polling resumes after outage',
            monitor.snapshot()['state'] in ('washing', 'no_task'),
            monitor.snapshot()['state'],
        )

        # Serial polling proof.
        with MOCK.lock:
            MOCK.orders = [make_order(
                'ORD8', room='2830', status='PICKUP_STARTED',
                task_type='PICKUP_FROM_ROOM',
                task_id='ORD8-PICKUP_FROM_ROOM',
            )]
            MOCK.get_delay = 0.3
        wait_for(lambda: monitor.snapshot()['order_id'] == 'ORD8',
                 description='final order')
        time.sleep(2.5)
        with MOCK.lock:
            MOCK.get_delay = 0.0
            max_inflight = MOCK.get_max_inflight
        check(
            'serial polling: GETs never overlap',
            max_inflight == 1,
            f'max_inflight={max_inflight} total_gets={MOCK.get_count}',
        )

        monitor.stop()

    server.shutdown()
    failures = [name for name, ok in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed')
    if failures:
        print('FAILED:', '; '.join(failures))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
