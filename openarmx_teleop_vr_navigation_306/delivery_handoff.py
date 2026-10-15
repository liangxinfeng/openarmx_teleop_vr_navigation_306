"""Serial polling client for the hotel delivery-robot humanoid handoff API.

Robot 283 only talks to the hotel ops repo (``/api/ops``) and never to the
delivery-robot vendor cloud.  The interface contract comes from the
humanoid-handoff document: the server alone decides arrival and cabin
assignment, so this client never re-derives or guesses them.

Design invariants:
- Polling is strictly serial: one daemon thread runs a loop of
  ``GET -> handle -> wait``; a request is never issued while the previous
  one is still in flight.
- Network failure is a distinct ``unknown`` state, never ``arrived=false``.
- ``task_id`` is the handoff identity: any change clears the previous
  task's door/open/error state.
- ``open`` is only allowed while ``arrived`` and both ``task_id`` and
  ``door_id`` are present, using exactly the values returned by the last
  successful GET.
"""

import ipaddress
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HANDOFF_PATH = '/api/ops/hotels/{hotel_id}/humanoid-handoff'
HOTEL_ID_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,64}')
MAX_RESPONSE_BYTES = 65536
DISABLED_POLL_SECONDS = 5.0

# Human-friendly text for the documented server error codes; the raw detail
# always goes to the log, VR only sees this short line.
HANDOFF_ERROR_MESSAGES = {
    'INVALID_REQUEST': '请求参数无效',
    'INVALID_HOTEL': '酒店标识无效',
    'ORDER_NOT_FOUND': '订单不存在或已结束',
    'HUMANOID_HANDOFF_TASK_ID_MISMATCH': '任务已变化，请稍后重试',
    'HUMANOID_HANDOFF_NOT_ARRIVED': '配送机器人尚未到达',
    'HUMANOID_HANDOFF_DOOR_ID_MISSING': '舱位尚未就绪，请稍后重试',
    'HUMANOID_HANDOFF_DOOR_ID_MISMATCH': '舱位校验失败，请稍后重试',
    'HUMANOID_HANDOFF_NOT_OPENED': '舱门尚未打开',
    'HUMANOID_HANDOFF_ALREADY_CLOSED': '舱门已关闭，任务可能已结束',
    'WASH_COMPLETE_NOT_READY': '当前订单不能标记洗衣完成',
    'DRY_COMPLETE_NOT_READY': '尚未洗衣完成，不能呼叫取衣（请先标记洗衣完成）',
    'YUNJI_CABIN_DISABLED': '云迹舱门控制未启用',
    'YUNJI_DOOR_REQUEST_FAILED': '云迹开关门请求失败',
    'YUNJI_DOOR_TIMEOUT': '云迹开关门请求超时',
    'LAUNDRY_COMPLETE_NOT_READY': '现在还不能标记洗衣完成（无活动订单或订单不在洗涤中）',
}

STATE_LABELS = {
    'disabled': '未配置',
    'no_task': '等待任务',
    'washing': '洗涤中',
    'delivering': '配送中 · 未到达',
    'arrived_wait_door': '已到达 · 等待舱位',
    'ready_to_open': '可开舱',
    'opening': '开门中…',
    'opened': '舱门已打开 · 请尽快取衣',
    'closing': '正在请求关门',
    'closed': '关门已受理',
    'unknown': '状态未知',
}

SHORT_LABELS = {
    'disabled': '未配置',
    'no_task': '等待任务',
    'washing': '洗涤中',
    'delivering': '未到达',
    'arrived_wait_door': '等待舱位',
    'ready_to_open': '可开舱',
    'opening': '开门中',
    'opened': '已开舱',
    'closing': '正在关门',
    'closed': '关门已受理',
    'unknown': '状态未知',
}

# Fine-grained order phases reported by the ops server.  Only the outbound
# leg is surfaced in VR (operator decision): WASHING means the pickup chain
# is finished, and the return leg is driven by the laundry-complete signal.
OUTBOUND_STATUS_LABELS = {
    'PICKUP_STARTED': '已接单 · 前往客房',
    'PICKUP_AT_ROOM': '客房放衣中',
    'PICKUP_TO_LAUNDRY': '前来洗衣房',
    'PICKUP_AT_LAUNDRY': '已到达洗衣房',
}
STATUS_CODE_PATTERN = re.compile(r'[A-Z][A-Z_]{0,31}')


class HandoffConfigError(ValueError):
    """The delivery handoff config file is missing or invalid."""


def _target_url(base_url, hotel_id, allow_lan_addresses):
    """Build and safety-check the humanoid handoff endpoint URL.

    Only http/https is allowed.  Localhost, loopback, private and reserved
    addresses are rejected unless the operator explicitly set
    ``allow_lan_addresses`` for an on-site ops server.  ``hotel_id`` is
    whitelist-matched so it can never inject path segments.
    """
    parsed = urllib.parse.urlsplit(str(base_url).strip())
    if parsed.scheme not in ('http', 'https'):
        raise HandoffConfigError('base_url 仅支持 http/https')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HandoffConfigError('base_url 不允许携带凭据、查询串或片段')
    host = str(parsed.hostname or '').strip().lower()
    if not host:
        raise HandoffConfigError('base_url 缺少主机名')
    if host == 'localhost':
        raise HandoffConfigError('base_url 不允许指向 localhost')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        lan_allowed = bool(allow_lan_addresses) and (
            address.is_private or address.is_loopback
        )
        if not lan_allowed:
            raise HandoffConfigError(
                'base_url 指向环回/私有/保留地址；如确属现场内网服务，'
                '请在配置中显式设置 allow_lan_addresses=true'
            )
    hotel_id = str(hotel_id).strip()
    if not HOTEL_ID_PATTERN.fullmatch(hotel_id):
        raise HandoffConfigError('hotel_id 只允许字母、数字、下划线和中划线')
    base_path = parsed.path.rstrip('/')
    return (
        f'{parsed.scheme}://{parsed.netloc}{base_path}'
        f'{HANDOFF_PATH.format(hotel_id=hotel_id)}'
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a request can never leave the configured host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DeliveryHandoffMonitor:
    """Owns the serial GET polling loop and the handoff state machine."""

    def __init__(self, config_path, logger):
        self._config_path = Path(config_path)
        self._logger = logger
        self._build_opener('')
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        # Config state (written by the polling thread only, after validation).
        self._config_revision = None
        self._config_error = ''
        self._poll_interval = 3.0
        self._request_timeout = 5.0
        self._url = None
        self._wash_url = None
        self._dry_url = None
        # Handoff state (guarded by _lock): the "primary" order drives the
        # single-operator UI; every active order is kept in self._orders.
        self._state = 'disabled'
        self._arrived = None
        self._task_id = None
        self._door_id = None
        self._room_no = None
        self._status_code = None
        self._order_id = None
        self._task_type = None
        self._orders = []
        self._detail = ''
        self._opened_at = None
        self._open_task_id = None
        self._closed_task_id = None
        self._door_inflight = None
        self._dry_complete_sent_at = None
        self._dry_complete_order = None
        self._wash_complete_sent_at = None
        self._pickup_task_chain = '洗衣房洗衣'
        self._return_task_chain = '洗衣房取衣'
        self._last_success_monotonic = None
        self._last_error_text = ''

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _build_opener(self, proxy_url):
        """Build the request opener with an explicit proxy policy.

        Ambient ``http_proxy``/``https_proxy`` from the launching shell is
        always ignored: a stale local proxy (the robot once ran one on
        127.0.0.1:17890) would silently break every handoff request.  The
        only supported proxy is the operator-configured ``proxy_url``.
        """
        proxy_url = str(proxy_url or '').strip()
        if proxy_url:
            parsed = urllib.parse.urlsplit(proxy_url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise HandoffConfigError('proxy_url 仅支持 http/https')
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({
                    'http': proxy_url, 'https': proxy_url,
                }),
                _NoRedirectHandler,
            )
        else:
            self._opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _NoRedirectHandler,
            )

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name='delivery-handoff-poller', daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Serial polling loop
    # ------------------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            url, interval, timeout = self._load_config_if_changed()
            if url is None:
                with self._lock:
                    if self._state not in ('disabled',):
                        self._log_info(
                            f'handoff polling disabled: {self._config_error or "未配置"}'
                        )
                    self._state = 'disabled'
                    self._detail = self._config_error or '未配置 base_url/hotel_id'
                    self._arrived = None
                    self._task_id = None
                    self._door_id = None
                    self._order_id = None
                    self._orders = []
                    self._opened_at = None
                    self._dry_complete_sent_at = None
                    self._dry_complete_order = None
                    self._wash_complete_sent_at = None
                    self._last_success_monotonic = None
                self._stop.wait(DISABLED_POLL_SECONDS)
                continue
            try:
                payload = self._request_json('GET', url, timeout, None)
                self._apply_get_payload(payload)
                self._last_success_monotonic = time.monotonic()
                self._last_error_text = ''
            except urllib.error.HTTPError as error:
                detail = self._error_detail(error)
                self._apply_transport_error(f'HTTP {error.code} {detail}'.strip())
            except urllib.error.URLError as error:
                reason = getattr(error, 'reason', error)
                self._apply_transport_error(f'连接失败：{reason}')
            except TimeoutError:
                self._apply_transport_error('请求超时')
            except (ValueError, json.JSONDecodeError) as error:
                self._apply_transport_error(f'响应解析失败：{error}')
            except Exception as error:  # defensive: polling must survive
                self._apply_transport_error(f'未知错误：{error}')
            self._stop.wait(interval)

    def _load_config_if_changed(self):
        """Hot-reload the JSON config; returns (url, interval, timeout).

        url is None when the feature is disabled.  Runs inside the polling
        thread only, so config fields need no extra locking.
        """
        try:
            stat = self._config_path.stat()
            revision = (stat.st_mtime_ns, stat.st_size)
            if revision == self._config_revision:
                return self._url, self._poll_interval, self._request_timeout
            raw = json.loads(self._config_path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                raise ValueError('配置根节点必须是对象')
            allow_lan = bool(raw.get('allow_lan_addresses', False))
            base_url = str(raw.get('base_url', '')).strip()
            hotel_id = str(raw.get('hotel_id', '')).strip()
            interval = min(30.0, max(1.0, float(raw.get('poll_interval_seconds', 3.0))))
            timeout = min(30.0, max(1.0, float(raw.get('request_timeout_seconds', 5.0))))
            if not base_url or not hotel_id:
                raise ValueError('base_url / hotel_id 尚未配置')
            url = _target_url(base_url, hotel_id, allow_lan)
            self._build_opener(raw.get('proxy_url', ''))
            hotel_base = url[: -len('/humanoid-handoff')]
            self._wash_url = hotel_base + '/wash-complete'
            self._dry_url = hotel_base + '/dry-complete'
            chain_name = str(raw.get('pickup_task_chain', '洗衣房洗衣')).strip()
            return_chain = str(
                raw.get('return_task_chain', '洗衣房取衣')
            ).strip()
            with self._lock:
                self._pickup_task_chain = chain_name[:64]
                self._return_task_chain = return_chain[:64]
            self._config_error = ''
            self._poll_interval = interval
            self._request_timeout = timeout
            self._url = url
            self._config_revision = revision
            self._log_info(
                f'handoff config loaded: poll={interval:.1f}s timeout={timeout:.1f}s '
                f'hotel={hotel_id}'
            )
        except (OSError, ValueError, json.JSONDecodeError, HandoffConfigError) as error:
            if str(error) != self._config_error:
                self._log_warn(f'handoff config 无效，功能停用：{error}')
            self._config_error = str(error)
            self._url = None
            self._wash_url = None
            self._dry_url = None
        return self._url, self._poll_interval, self._request_timeout

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _request_json(self, method, url, timeout, body):
        data = None
        headers = {'Accept': 'application/json'}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode(
                'utf-8'
            )
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self._opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError('响应体过大')
        text = raw.decode('utf-8', errors='replace').strip()
        if not text:
            raise ValueError('响应体为空')
        return json.loads(text)

    @staticmethod
    def _error_detail(error):
        try:
            raw = error.read(MAX_RESPONSE_BYTES + 1)
            payload = json.loads(raw.decode('utf-8', errors='replace'))
            code = str(payload.get('code', '')).strip()
            message = str(payload.get('message', '')).strip()
            return f'{code} {message}'.strip()
        except Exception:
            return ''

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _derived_state(self):
        if self._arrived is True:
            if self._task_id and self._door_id:
                if self._task_id == self._closed_task_id:
                    return 'closed'
                if self._task_id == self._open_task_id:
                    return 'opened'
                return 'ready_to_open'
            return 'arrived_wait_door'
        if self._task_id:
            return 'delivering'
        if self._order_id and self._status_code == 'WASHING':
            return 'washing'
        return 'no_task'

    @staticmethod
    def _parse_order(item):
        if not isinstance(item, dict):
            return None
        status_code = str(item.get('status') or '').strip().upper()
        status_code = (
            status_code if STATUS_CODE_PATTERN.fullmatch(status_code) else None
        )
        arrived = item.get('arrived')
        task_type = str(item.get('task_type') or '').strip() or None
        return {
            'order_id': str(item.get('order_id') or '').strip() or None,
            'room': str(item.get('room') or '').strip() or None,
            'status_code': status_code,
            'status_label': OUTBOUND_STATUS_LABELS.get(status_code),
            'arrived': arrived if isinstance(arrived, bool) else False,
            'task_type': task_type,
            'task_id': str(item.get('task_id') or '').strip() or None,
            'door_id': str(item.get('door_id') or '').strip() or None,
            'opened': bool(item.get('opened')),
            'closed': bool(item.get('closed')),
        }

    @staticmethod
    def _primary_rank(order):
        """Lower wins: actionable arrival first, then any active trip, then
        the washing order awaiting wash/dry-complete."""
        if order['arrived'] and order['task_id'] and order['door_id']:
            return 0
        if order['arrived'] and order['task_id']:
            return 1
        if order['task_id']:
            return 2
        if order['status_code'] == 'WASHING':
            return 3
        return 4

    def _apply_get_payload(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('GET 响应必须是对象')
        if 'handoffs' in payload:
            raw_orders = payload.get('handoffs')
        elif 'arrived' in payload:
            raw_orders = [payload]  # legacy single-object response
        else:
            raise ValueError('GET 响应缺少 handoffs 数组')
        if not isinstance(raw_orders, list):
            raise ValueError('GET 响应 handoffs 必须是数组')
        orders = [
            order for order in (
                self._parse_order(item) for item in raw_orders[:4]
            ) if order is not None
        ]
        # min() keeps the first minimal element, so equal ranks stay in the
        # server's stable ordered_at ordering.
        primary = min(orders, key=self._primary_rank) if orders else None
        new_task = primary['task_id'] if primary else None
        new_order = primary['order_id'] if primary else None
        with self._lock:
            if new_order != self._order_id or new_task != self._task_id:
                if self._order_id is not None or self._task_id is not None:
                    self._log_info(
                        f'handoff primary changed: order={self._order_id} '
                        f'task={self._task_id} -> order={new_order} '
                        f'task={new_task}'
                    )
                # Door/room/sticky-open belong to the previous primary task.
                self._door_id = None
                self._room_no = None
                self._opened_at = None
                self._open_task_id = None
                self._closed_task_id = None
                self._detail = ''
                # The waiting window after dry-complete belongs to its order:
                # a fresh outbound cycle or the summoned order leaving the
                # active list closes it.
                live_orders = {order['order_id'] for order in orders}
                if (new_task or '').endswith('-PICKUP_FROM_ROOM') or (
                    self._dry_complete_order
                    and self._dry_complete_order not in live_orders
                ):
                    self._dry_complete_sent_at = None
                    self._dry_complete_order = None
            previous = self._state
            self._orders = orders
            self._arrived = primary['arrived'] if primary else False
            self._task_id = new_task
            self._door_id = primary['door_id'] if primary else None
            self._room_no = primary['room'] if primary else None
            self._status_code = primary['status_code'] if primary else None
            self._order_id = new_order
            self._task_type = primary['task_type'] if primary else None
            # Server-side automatic actions belong to this exact trip.
            if primary and primary['closed']:
                self._closed_task_id = new_task
            if primary and primary['opened']:
                self._open_task_id = new_task
            # A fresh authoritative answer supersedes any stale error text
            # (config notices, open failures, transport errors).
            self._detail = ''
            if self._door_inflight and self._door_inflight[:3] == (new_order, new_task, self._door_id):
                self._state = 'opening' if self._door_inflight[3] == 'open' else 'closing'
            else:
                self._state = self._derived_state()
            self._announce(previous, self._state)

    def _apply_transport_error(self, text):
        with self._lock:
            previous = self._state
            self._state = 'unknown'
            self._detail = text
            if text != self._last_error_text:
                self._log_warn(f'handoff 状态获取失败：{text}')
                self._last_error_text = text
            self._announce(previous, self._state)

    def _announce(self, previous, current):
        if previous == current:
            return
        self._log_info(
            f'handoff state {previous} -> {current} '
            f'(task={self._task_id} door={self._door_id})'
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def snapshot(self):
        with self._lock:
            opened_seconds_ago = (
                None if self._opened_at is None else max(0.0, time.time() - self._opened_at)
            )
            last_success_age = (
                None if self._last_success_monotonic is None
                else max(0.0, time.monotonic() - self._last_success_monotonic)
            )
            state = self._state
            return {
                'state': state,
                'label': STATE_LABELS.get(state, state),
                'short_label': SHORT_LABELS.get(state, state),
                'detail': self._detail,
                'arrived': self._arrived,
                'task_id': self._task_id,
                'door_id': self._door_id,
                'room_no': self._room_no,
                'task_type': self._task_type,
                'orders': [dict(order) for order in self._orders],
                'can_open': (
                    state == 'ready_to_open'
                    and bool(self._arrived and self._task_id and self._door_id)
                ),
                'can_close': (
                    state == 'opened' and self._door_inflight is None
                    and bool(self._arrived and self._order_id and self._task_id and self._door_id)
                    and last_success_age is not None and last_success_age <= 15
                ),
                'poll_interval_seconds': round(self._poll_interval, 2),
                'last_success_age': (
                    None if last_success_age is None else round(last_success_age, 2)
                ),
                'opened_at': self._opened_at,
                'opened_seconds_ago': (
                    None if opened_seconds_ago is None else round(opened_seconds_ago, 1)
                ),
                'wash_complete_sent_at': self._wash_complete_sent_at,
                'dry_complete_sent_at': self._dry_complete_sent_at,
                'dry_complete_order': self._dry_complete_order,
                'status_code': self._status_code,
                'status_label': OUTBOUND_STATUS_LABELS.get(self._status_code),
                'order_id': self._order_id,
                'pickup_task_chain': self._pickup_task_chain,
                'return_task_chain': self._return_task_chain,
            }

    def request_open(self, expected=None):
        return self._request_door('open', expected)

    def request_close(self, expected=None):
        return self._request_door('close', expected)

    def _request_door(self, action, expected):
        """One serialized open/close transaction bound to a specific trip."""
        with self._lock:
            if expected is not None:
                current = {'order_id': self._order_id, 'task_id': self._task_id, 'door_id': self._door_id}
                if any(str(current[k] or '') != str(expected.get(k) or '') for k in current):
                    return False, '订单或舱位已变化，请查看最新到达卡片'
                if (self._state == 'unknown' or self._last_success_monotonic is None
                        or time.monotonic() - self._last_success_monotonic > 15):
                    return False, '配送状态已过期，请等待重新连接'
            if self._door_inflight is not None:
                return False, '舱门请求正在进行中，请稍候'
            if self._task_id and self._closed_task_id == self._task_id:
                return (True, '该程关门已受理') if action == 'close' else (False, '该程已关门，不能再开')
            if action == 'open' and self._state == 'opened':
                return True, '舱门已处于打开状态'
            if not (self._arrived and self._task_id and self._door_id):
                return False, f'当前状态不允许开舱（{STATE_LABELS.get(self._state, self._state)}）'
            if action == 'close' and self._open_task_id != self._task_id:
                return False, '尚未开门，不能关门'
            self._state = 'opening' if action == 'open' else 'closing'
            order_id = self._order_id
            task_id = self._task_id
            door_id = str(self._door_id)
            url = self._url
            timeout = self._request_timeout
            self._door_inflight = (order_id, task_id, door_id, action)
        try:
            return self._send_door_command(action, order_id, task_id, door_id, url, timeout)
        finally:
            with self._lock:
                self._door_inflight = None

    def _send_door_command(self, action, order_id, task_id, door_id, url, timeout):
        verb = '开门' if action == 'open' else '关门'
        if url is None or not order_id:
            with self._lock:
                self._state = self._derived_state()
            return False, '交接服务未配置或没有活动订单'
        self._log_info(
            f'handoff {action} requested: order={order_id} task={task_id} door={door_id}'
        )
        try:
            payload = self._request_json(
                'POST', f'{url}/{action}', timeout, {
                    'order_id': order_id,
                    'task_id': task_id,
                    'door_id': door_id,
                }
            )
        except urllib.error.HTTPError as error:
            detail = self._error_detail(error)
            return self._door_failed(error.code, detail, task_id, door_id, verb)
        except urllib.error.URLError as error:
            reason = getattr(error, 'reason', error)
            return self._door_failed(None, f'连接失败：{reason}', task_id, door_id, verb)
        except TimeoutError:
            return self._door_failed(None, '请求超时', task_id, door_id, verb)
        except (ValueError, json.JSONDecodeError) as error:
            return self._door_failed(None, f'响应解析失败：{error}', task_id, door_id, verb)
        if action == 'close' and (
                not isinstance(payload, dict) or payload.get('action') != 'close'
                or payload.get('result') != 'applied' or payload.get('task_id') != task_id
                or str(payload.get('door_id')) != door_id):
            return self._door_failed(None, '关门响应与请求不一致，请查询状态', task_id, door_id, verb)
        opened_door = str((payload or {}).get('door_id') or door_id)
        with self._lock:
            previous = self._state
            if task_id != self._task_id or order_id != self._order_id or door_id != str(self._door_id):
                # Order/task identity changed while the POST was in flight;
                # the result belongs to the previous order and must not stick.
                self._log_warn(
                    f'handoff {action} landed on changed order: sent={order_id}/{task_id} '
                    f'current={self._order_id}/{self._task_id}; ignoring success'
                )
                self._state = self._derived_state()
                self._announce(previous, self._state)
                return False, f'订单已变化，{verb}结果已忽略，请确认后重试'
            self._state = 'opened' if action == 'open' else 'closed'
            if action == 'open':
                self._open_task_id = task_id
                self._opened_at = time.time()
            else:
                self._closed_task_id = task_id
            self._detail = ''
            self._announce(previous, self._state)
        self._log_info(
            f'handoff door {action} applied: task={task_id} door={opened_door}'
        )
        return (True, f'舱门 {door_id} 已打开，请尽快完成取衣（约90秒后自动关门）') if action == 'open' else (
            True, f'舱门 {door_id} 关门已受理，配送机器人可能继续行程，请保持避让')

    def _door_failed(self, http_status, detail, task_id, door_id, verb):
        """Log the full failure and map it to a short user-facing message."""
        code = ''
        message = str(detail or '').strip()
        for known in HANDOFF_ERROR_MESSAGES:
            if message.startswith(known):
                code = known
                message = message[len(known):].strip() or message
                break
        self._log_warn(
            f'handoff {verb} failed: http={http_status} code={code} detail={detail} '
            f'task={task_id} door={door_id}'
        )
        user_message = HANDOFF_ERROR_MESSAGES.get(code)
        if user_message is None:
            if http_status is None:
                user_message = f'{verb}请求未确认，请刷新舱门状态后重试'
            else:
                user_message = f'{verb}失败（HTTP {http_status}）'
        with self._lock:
            previous = self._state
            if task_id == self._task_id:
                self._state = self._derived_state()
                self._detail = user_message
            else:
                self._state = self._derived_state()
            self._announce(previous, self._state)
        return False, user_message

    def _action_order_id(self):
        """Pick which order a wash/dry-complete call targets.

        Prefers the order sitting in WASHING (the one the operator just
        washed), then any order already on its return leg (idempotent
        retry), and finally the primary order.
        """
        with self._lock:
            orders = [dict(order) for order in self._orders]
            primary = self._order_id
            dry_order = self._dry_complete_order
        for status in ('WASHING',):
            for order in orders:
                if order['status_code'] == status and order['order_id']:
                    return order['order_id']
        for order in orders:
            if str(order['status_code'] or '').startswith('RETURN') \
                    and order['order_id']:
                return order['order_id']
        return dry_order or primary

    def _post_order_complete(self, url, order_id, timeout):
        """POST {order_id} to a wash/dry-complete endpoint; maps errors."""
        try:
            self._request_json('POST', url, timeout, {'order_id': order_id})
        except urllib.error.HTTPError as error:
            detail = self._error_detail(error)
            message = str(detail or '').strip()
            code = next(
                (known for known in HANDOFF_ERROR_MESSAGES
                 if message.startswith(known)),
                '',
            )
            self._log_warn(
                f'handoff order action failed: http={error.code} '
                f'code={code} detail={detail} order={order_id}'
            )
            user_message = HANDOFF_ERROR_MESSAGES.get(code)
            if user_message is None:
                user_message = f'操作失败（HTTP {error.code}）'
            return False, user_message
        except urllib.error.URLError as error:
            reason = getattr(error, 'reason', error)
            self._log_warn(f'handoff order action failed: {reason}')
            return False, f'操作失败：{reason}'
        except TimeoutError:
            self._log_warn('handoff order action failed: timeout')
            return False, '操作超时，请重试'
        except (ValueError, json.JSONDecodeError) as error:
            self._log_warn(f'handoff order action failed: {error}')
            return False, f'响应解析失败：{error}'
        return True, ''

    def request_wash_complete(self):
        """POST wash-complete for the order being washed.

        Marks the order washed (frees the slot for the next guest order and
        queues the admin drying voice).  Does NOT summon the delivery robot.
        """
        with self._lock:
            url = self._wash_url
            timeout = self._request_timeout
        if url is None:
            return False, '交接服务未配置'
        order_id = self._action_order_id()
        if not order_id:
            return False, '没有可标记洗衣完成的订单'
        self._log_info(f'handoff wash-complete requested: order={order_id}')
        success, message = self._post_order_complete(url, order_id, timeout)
        if not success:
            return False, message
        with self._lock:
            self._wash_complete_sent_at = time.time()
        self._log_info(f'handoff wash-complete accepted: order={order_id}')
        return True, '洗衣完成已标记，管理员将前来烘干，可接待下一单'

    def request_dry_complete(self):
        """POST dry-complete for the order being washed.

        Writes the dry timestamp and queues the RETURN_TO_ROOM trip — this
        is the call that summons the delivery robot.  Idempotent server
        side; the return task_id is learned from subsequent GETs.
        """
        with self._lock:
            url = self._dry_url
            timeout = self._request_timeout
        if url is None:
            return False, '交接服务未配置'
        order_id = self._action_order_id()
        if not order_id:
            return False, '没有可呼叫取衣的订单'
        self._log_info(f'handoff dry-complete requested: order={order_id}')
        success, message = self._post_order_complete(url, order_id, timeout)
        if not success:
            return False, message
        with self._lock:
            self._dry_complete_sent_at = time.time()
            self._dry_complete_order = order_id
        self._log_info(f'handoff dry-complete accepted: order={order_id}')
        return True, '已通知配送机器人前来取衣，等待其到达'

    # ------------------------------------------------------------------

    def _log_info(self, text):
        try:
            self._logger.info(text)
        except Exception:
            pass

    def _log_warn(self, text):
        try:
            self._logger.warning(text)
        except Exception:
            pass
