#!/usr/bin/env python3
"""End-to-end smoke test on the robot: real bridge node + mock ops server.

Starts the bridge with its own isolated config, then checks the
2026-09-23 handoffs[] contract end to end: offer card flow (accept /
reject), wash-complete / dry-complete routes, the combined return-chain
start (summon + chain), and the open path carrying order_id.

This harness only ever talks to the local bridge through curl with a
pinned constant origin; the mock ops server listens on loopback for the
bridge's own poller (allow_lan_addresses is what the test config
enables).  The empty default config is restored and the bridge stopped
afterwards."""

import json
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG_PATH = Path('/tmp/delivery_handoff_smoke_config.json')
if '..' in str(CONFIG_PATH):
    raise SystemExit('unsafe config path')
# The production bridge may be running and hot-reloads the shared config in
# ~/ros2_ws/src; the smoke bridge gets its own isolated config file via the
# delivery_handoff_config_path parameter so this test never disturbs it.
EMPTY_CONFIG = {
    '_comment': 'restored by smoke test',
    'base_url': '',
    'hotel_id': '',
    'poll_interval_seconds': 3.0,
    'request_timeout_seconds': 5.0,
    'allow_lan_addresses': False,
    'pickup_task_chain': '',
    'return_task_chain': '',
}
BRIDGE_ORIGIN = 'https://127.0.0.1:8443'
MOCK_PORT = 8911
HOTEL = 'SMOKE1'
BASE = f'/api/ops/hotels/{HOTEL}'
GET_PATH = f'{BASE}/humanoid-handoff'
OPEN_PATH = f'{GET_PATH}/open'
WASH_PATH = f'{BASE}/wash-complete'
DRY_PATH = f'{BASE}/dry-complete'

MOCK_STATE = {
    'order': None,
    'washed': set(),
    'open_bodies': [],
    'wash_bodies': [],
    'dry_bodies': [],
    'get_count': 0,
}
MOCK_LOCK = threading.Lock()


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
        with MOCK_LOCK:
            MOCK_STATE['get_count'] += 1
            order = dict(MOCK_STATE['order'] or {})
        self._json(200, {'handoffs': [order] if order else []})

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(length) or b'{}')
        if self.path == WASH_PATH:
            with MOCK_LOCK:
                MOCK_STATE['wash_bodies'].append(body)
                MOCK_STATE['washed'].add(str(body.get('order_id')))
            self._json(200, {
                'order_id': body.get('order_id'), 'status': 'WASHING',
                'wash_completed_at': '2026-09-23T12:00:00.000Z',
                'laundry_completed_at': None,
            })
            return
        if self.path == DRY_PATH:
            order_id = str(body.get('order_id'))
            with MOCK_LOCK:
                washed = set(MOCK_STATE['washed'])
            if order_id not in washed:
                self._json(409, {
                    'code': 'DRY_COMPLETE_NOT_READY',
                    'message': 'DRY_COMPLETE_NOT_READY 尚未洗衣完成',
                })
                return
            with MOCK_LOCK:
                MOCK_STATE['dry_bodies'].append(body)
            self._json(200, {
                'order_id': order_id, 'status': 'WASHING',
                'laundry_completed_at': '2026-09-23T12:30:00.000Z',
            })
            return
        if self.path != OPEN_PATH:
            self._json(404, {'message': 'not found'})
            return
        with MOCK_LOCK:
            MOCK_STATE['open_bodies'].append(body)
        self._json(200, {
            'order_id': body.get('order_id'), 'action': 'open',
            'result': 'applied', 'task_id': body.get('task_id'),
            'door_id': body.get('door_id'), 'idempotent': False,
        })


def https_json(method, path, payload=None):
    """Query the local bridge with curl; the origin is a fixed constant and
    the path is validated, so no untrusted value picks the target."""
    if not path.startswith('/') or '..' in path or '://' in path:
        raise ValueError(f'unexpected bridge path: {path!r}')
    command = [
        'curl', '-sk', '--max-time', '8', '-X', method, BRIDGE_ORIGIN + path,
        '-w', '\n%{http_code}',
    ]
    if payload is not None:
        command += [
            '-H', 'Content-Type: application/json',
            '-d', json.dumps(payload),
        ]
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=12, check=False
    )
    body, _, code = completed.stdout.rpartition('\n')
    try:
        parsed = json.loads(body or '{}')
    except json.JSONDecodeError:
        parsed = {}
    return int(code or 0), parsed


def wait_for(predicate, timeout, description):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as error:
            last = error
        time.sleep(0.4)
    raise AssertionError(f'timed out waiting for {description}: {last!r}')


def bridge_handoff(expected_state):
    """Return the delivery_handoff block only when it reaches the expected
    state, so wait_for's truthiness check cannot fire early."""
    def predicate():
        _status, payload = https_json('GET', '/api/navigation/state')
        handoff = (payload or {}).get('delivery_handoff') or {}
        return handoff if handoff.get('state') == expected_state else None
    return predicate


def set_order(**fields):
    with MOCK_LOCK:
        MOCK_STATE['order'] = fields


CATALOG_PATH = Path(
    '/home/ubuntu/ros2_ws/src/openarmx_teleop_vr_navigation_306'
    '/config/task_actions.txt'
)


def chain_step_count(name):
    """Count task rows under 任务串：<name> in the live catalog so the
    assertions follow the colleague's edits instead of hardcoding."""
    count = 0
    active = False
    for raw in CATALOG_PATH.read_text(encoding='utf-8').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if line.startswith('任务串'):
            active = line.replace('：', ':').split(':', 1)[-1].strip() == name
            continue
        if active:
            count += 1
    return count


def main():
    results = []

    def check(name, condition, detail=''):
        results.append((name, bool(condition)))
        print(f'{"PASS" if condition else "FAIL"}  {name}  {detail}')

    server = ThreadingHTTPServer(('127.0.0.1', MOCK_PORT), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    log_handle = tempfile.NamedTemporaryFile(
        prefix='bridge_smoke_', suffix='.log', delete=False
    )
    CONFIG_PATH.write_text(json.dumps(EMPTY_CONFIG, indent=2), encoding='utf-8')
    bridge = subprocess.Popen(
        [
            'bash', '-c',
            'source /opt/ros/jazzy/setup.bash && '
            'source /home/ubuntu/ros2_ws/install/setup.bash && '
            'exec /home/ubuntu/ros2_ws/venvs/openarmx_v4_placo/bin/python '
            '/home/ubuntu/ros2_ws/install/openarmx_teleop_vr_navigation_306'
            '/lib/openarmx_teleop_vr_navigation_306/navigation_web_bridge '
            '--ros-args -p delivery_handoff_config_path:=' + str(CONFIG_PATH),
        ],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        state = wait_for(
            lambda: (https_json('GET', '/api/navigation/state')[1] or {}),
            timeout=40,
            description='bridge https ready',
        )
        handoff = state.get('delivery_handoff')
        check('bridge serves delivery_handoff block', isinstance(handoff, dict))
        check(
            'empty config -> disabled state',
            handoff and handoff.get('state') == 'disabled',
            json.dumps(handoff, ensure_ascii=False),
        )

        status, result = https_json('POST', '/api/delivery-handoff/open')
        check(
            'open while disabled rejected with message',
            status == 409 and result.get('success') is False
            and '不允许开舱' in str(result.get('message', '')),
            json.dumps(result, ensure_ascii=False),
        )

        CONFIG_PATH.write_text(json.dumps({
            'base_url': f'http://127.0.0.1:{MOCK_PORT}',
            'hotel_id': HOTEL,
            'poll_interval_seconds': 1.0,
            'request_timeout_seconds': 3.0,
            'allow_lan_addresses': True,
            'pickup_task_chain': '洗衣房洗衣',
            'return_task_chain': '洗衣房取衣',
        }, indent=2), encoding='utf-8')
        wait_for(
            bridge_handoff('no_task'),
            timeout=15,
            description='hot-reload activates polling (no_task)',
        )
        check('hot-reload activates polling', True)

        # Outbound order start -> a task OFFER appears (A·A accept / X
        # reject); nothing auto-submits and no pending banner is raised.
        set_order(
            order_id='SMOKE-ORD-9', room='2727', status='PICKUP_STARTED',
            arrived=False, task_type='PICKUP_FROM_ROOM',
            task_id='SMOKE-ORD-9-PICKUP_FROM_ROOM', door_id=None,
        )

        def offered_state(expected_order=None):
            _s, payload = https_json('GET', '/api/navigation/state')
            block = (payload or {}).get('delivery_handoff') or {}
            offer = block.get('offer') or {}
            if offer.get('state') != 'offered':
                return None
            if expected_order and offer.get('order') != expected_order:
                return None
            return payload

        offered = wait_for(
            offered_state, timeout=15, description='delivery offer',
        )
        block = offered['delivery_handoff']
        dispatch = offered.get('task_dispatch') or {}
        check(
            'PICKUP_STARTED raises offer without auto-submitting',
            block['offer']['chain'] == '洗衣房洗衣'
            and dispatch.get('state') in ('idle', 'cancelled', 'completed'),
            f"dispatch={dispatch.get('state')}",
        )

        status, result = https_json(
            'POST', '/api/delivery-handoff/pickup-chain/accept'
        )
        dispatch = result.get('task_dispatch') or {}
        expected_pickup = chain_step_count('洗衣房洗衣')
        check(
            'accept starts the laundry chain immediately',
            status == 200 and result.get('success') is True
            and dispatch.get('total') == expected_pickup
            and (result.get('delivery_handoff') or {}).get(
                'offer', {}
            ).get('state') == 'accepted',
            f"total={dispatch.get('total')} expected={expected_pickup}",
        )
        https_json('POST', '/api/tasks/cancel')

        # A second order rejected with X leaves the view clean.
        set_order(
            order_id='SMOKE-ORD-10', room='2728', status='PICKUP_STARTED',
            arrived=False, task_type='PICKUP_FROM_ROOM',
            task_id='SMOKE-ORD-10-PICKUP_FROM_ROOM', door_id=None,
        )
        wait_for(
            lambda: offered_state('SMOKE-ORD-10-PICKUP_FROM_ROOM'),
            timeout=15,
            description='second delivery offer',
        )
        status, result = https_json(
            'POST', '/api/delivery-handoff/pickup-chain/reject'
        )
        _s, payload = https_json('GET', '/api/navigation/state')
        dispatch = (payload or {}).get('task_dispatch') or {}
        check(
            'reject closes the offer without starting a chain',
            status == 200
            and ((payload or {}).get('delivery_handoff') or {}).get(
                'offer', {}
            ).get('state') == 'rejected'
            and dispatch.get('state') in ('idle', 'cancelled', 'completed'),
            f"dispatch={dispatch.get('state')}",
        )

        # wash-complete route (marks washed; no summon).
        set_order(order_id='SMOKE-ORD-9', room='2727', status='WASHING')
        wait_for(
            bridge_handoff('washing'),
            timeout=15,
            description='washing state',
        )
        status, result = https_json(
            'POST', '/api/delivery-handoff/wash-complete'
        )
        check(
            'wash-complete route marks the order washed',
            status == 200 and result.get('success') is True
            and MOCK_STATE['wash_bodies'] == [{'order_id': 'SMOKE-ORD-9'}]
            and (result.get('delivery_handoff') or {}).get(
                'wash_complete_sent_at'
            ) is not None
            and (result.get('delivery_handoff') or {}).get(
                'dry_complete_sent_at'
            ) is None,
            json.dumps(result.get('message', ''), ensure_ascii=False),
        )

        # dry-complete before wash would 409 on a fresh order; here the
        # combined return-chain start runs wash+summon path after the wash.
        status, result = https_json(
            'POST', '/api/delivery-handoff/return-chain/start'
        )
        dispatch_total = (result.get('task_dispatch') or {}).get('total')
        expected_return = chain_step_count('洗衣房取衣')
        check(
            'return-chain start summons robot and submits the chain',
            status == 200 and result.get('success') is True
            and dispatch_total == expected_return
            and MOCK_STATE['dry_bodies'] == [{'order_id': 'SMOKE-ORD-9'}]
            and (result.get('delivery_handoff') or {}).get(
                'dry_complete_sent_at'
            ) is not None,
            f'total={dispatch_total} expected={expected_return}',
        )
        https_json('POST', '/api/tasks/cancel')

        # Arrival + open through the bridge with the order identity.
        set_order(
            order_id='SMOKE-ORD-9', room='2727', status='PICKUP_AT_LAUNDRY',
            arrived=True, task_type='PICKUP_FROM_ROOM',
            task_id='SMOKE-ORD-9-PICKUP_FROM_ROOM', door_id='5',
        )
        handoff = wait_for(
            bridge_handoff('ready_to_open'),
            timeout=15,
            description='ready_to_open',
        )
        check(
            'ready_to_open exposes server identity',
            handoff.get('can_open') is True
            and handoff.get('door_id') == '5'
            and handoff.get('order_id') == 'SMOKE-ORD-9'
            and handoff.get('status_code') == 'PICKUP_AT_LAUNDRY',
            json.dumps(handoff, ensure_ascii=False),
        )

        status, result = https_json('POST', '/api/delivery-handoff/open')
        check(
            'open through bridge uses order identity',
            status == 200 and result.get('success') is True
            and MOCK_STATE['open_bodies'] == [{
                'order_id': 'SMOKE-ORD-9',
                'task_id': 'SMOKE-ORD-9-PICKUP_FROM_ROOM',
                'door_id': '5',
            }],
            json.dumps(result, ensure_ascii=False),
        )
        handoff = wait_for(
            bridge_handoff('opened'),
            timeout=10,
            description='opened state visible',
        )
        check(
            'opened state with timestamp',
            handoff.get('state') == 'opened'
            and handoff.get('opened_at') is not None,
            json.dumps(handoff, ensure_ascii=False),
        )

        with MOCK_LOCK:
            gets = MOCK_STATE['get_count']
        check('polling actually ran against mock', gets >= 3, f'gets={gets}')
    finally:
        CONFIG_PATH.write_text(json.dumps(EMPTY_CONFIG, indent=2),
                               encoding='utf-8')
        bridge.terminate()
        try:
            bridge.wait(timeout=8)
        except subprocess.TimeoutExpired:
            bridge.kill()
        log_handle.close()
        server.shutdown()
        print(f'bridge log: {log_handle.name}')

    failures = [name for name, ok in results if not ok]
    print(f'\n{len(results) - len(failures)}/{len(results)} smoke checks passed')
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
