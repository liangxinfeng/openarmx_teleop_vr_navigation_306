"""Extend the proven 306 WebXR bridge with a small, fail-safe navigation UI."""

import asyncio
import json
import math
from pathlib import Path
import struct
import threading
import time
import zlib

from aiohttp import WSMsgType, web
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
from nav_msgs.msg import OccupancyGrid
from nav2_msgs.srv import LoadMap
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger
from visualization_msgs.msg import Marker, MarkerArray
import yaml

from control_center_navigation.srv import (
    DeleteWaypoint,
    ListWaypoints,
    NavigateToWaypoint,
    SaveWaypoint,
    SwitchWaypointMap,
)
from openarmx_teleop_vr_306_v4.vr_web_bridge import VrWebBridge
from .depth_cloud_stream import DepthCloudStream
from .task_workflow import (
    TaskWorkflow,
    navigation_terminal_outcome,
    task_chain_templates,
)


def _png_chunk(kind, payload):
    return (
        struct.pack('>I', len(payload)) + kind + payload
        + struct.pack('>I', zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def occupancy_grid_png(width, height, values):
    """Encode a map as an 8-bit grayscale PNG without an image dependency."""
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0 or len(values) != width * height:
        raise ValueError('invalid occupancy grid')
    rows = []
    # OccupancyGrid starts at the map's lower-left; PNG starts at upper-left.
    for row_index in range(height - 1, -1, -1):
        start = row_index * width
        row = bytearray(width + 1)
        for column, occupancy in enumerate(values[start:start + width], 1):
            value = int(occupancy)
            # A dark VR palette prevents free space from becoming a glaring
            # white rectangle while keeping walls easy to distinguish.
            row[column] = 90 if value < 0 else 45 + 190 * min(100, value) // 100
        rows.append(bytes(row))
    header = struct.pack('>IIBBBBB', width, height, 8, 0, 0, 0, 0)
    return (
        b'\x89PNG\r\n\x1a\n'
        + _png_chunk(b'IHDR', header)
        + _png_chunk(b'IDAT', zlib.compress(b''.join(rows), 5))
        + _png_chunk(b'IEND', b'')
    )


def task_voice_text(event, task, showroom_single=False):
    """Build concise speech without leaking the showroom placeholder task."""
    location = str((task or {}).get('location', '')).strip()
    action = str((task or {}).get('task', '')).strip()
    if event == 'departure':
        if showroom_single:
            return f'前往{location}执行任务。'
        return f'开始导航，前往{location}执行{action}。'
    if event == 'ready':
        if showroom_single:
            return '已确认周围环境安全，开始执行任务。'
        return f'已确认周围环境安全，开始执行任务：{action}。'
    if event == 'arrived':
        if showroom_single:
            return f'已到达{location}，已确认周围环境安全，开始执行任务。'
        return f'已到达{location}，已确认周围环境安全，开始执行任务：{action}。'
    if event == 'completed':
        if showroom_single:
            return f'{location}任务已完成。'
        return f'{action}已完成。'
    raise ValueError(f'unsupported task voice event: {event}')


def quaternion_yaw(quaternion):
    return math.atan2(
        2.0 * (quaternion.w * quaternion.z + quaternion.x * quaternion.y),
        1.0 - 2.0 * (quaternion.y * quaternion.y + quaternion.z * quaternion.z),
    )


def shaped_axis(value, deadzone=0.10):
    value = max(-1.0, min(1.0, float(value)))
    magnitude = abs(value)
    if magnitude <= deadzone:
        return 0.0
    normalized = (magnitude - deadzone) / (1.0 - deadzone)
    return math.copysign(normalized ** 1.35, value)


class NavigationVrWebBridge(VrWebBridge):
    """One HTTPS endpoint for arms, RGB, map, waypoints and base driving."""

    def __init__(self):
        # VrWebBridge starts its web thread in __init__.  Gate the overridden
        # server until every navigation callback and route is ready.
        self._navigation_ready = threading.Event()
        super().__init__()

        self.declare_parameter('manual_cmd_vel_topic', '/manual_cmd_vel')
        self.declare_parameter('depth_cloud_minimum_m', 0.20)
        self.declare_parameter('depth_cloud_maximum_m', 5.0)
        # Preserve the official stable-UV/aligned-RGB565 design, with a denser
        # 26k WebXR profile requested for small-object inspection. Browser
        # delivery stays at 30 FPS to keep arm-control traffic deterministic.
        self.declare_parameter('precision_cloud_enabled', True)
        self.declare_parameter('precision_cloud_point_count', 18000)
        self.declare_parameter('precision_cloud_maximum_fps', 30.0)
        self.declare_parameter('manual_override_topic', '/manual_cmd_vel_active')
        self.declare_parameter('map_topic', '/map')
        self.declare_parameter('initial_pose_topic', '/initialpose')
        self.declare_parameter('amcl_pose_topic', '/amcl_pose')
        self.declare_parameter('navigation_status_topic', '/waypoints/navigation_status')
        self.declare_parameter('relocalization_status_topic', '/global_relocalization/status')
        self.declare_parameter('waypoint_marker_topic', '/waypoints/markers')
        self.declare_parameter(
            'corridor_projection_topic', '/vr_navigation/corridor_projection'
        )
        control_center_source = Path(
            '/home/ubuntu/ros2_ws/src/autolife_robot_control_center'
        )
        navigation_source = control_center_source / 'control_center_navigation'
        self.declare_parameter(
            'map_library_directories',
            [
                str(control_center_source / 'multi_lidar_slam' / 'maps'),
                str(navigation_source / 'maps'),
            ],
        )
        self.declare_parameter(
            'active_map_config', str(navigation_source / 'config' / 'active_map.yaml')
        )
        self.declare_parameter(
            'relocalization_service',
            '/cartographer_global_relocalization/relocalize',
        )
        self.declare_parameter('maximum_forward_speed', 0.35)
        self.declare_parameter('maximum_lateral_speed', 0.25)
        self.declare_parameter('maximum_yaw_speed', 0.80)
        self.declare_parameter('linear_acceleration', 1.2)
        self.declare_parameter('yaw_acceleration', 2.4)
        self.declare_parameter('drive_watchdog_seconds', 0.36)
        self.declare_parameter('drive_takeover_settle_seconds', 0.12)
        self.declare_parameter(
            'body_height_command_topic',
            '/openarmx_teleop_vr_306_v4/body_height_command',
        )
        self.declare_parameter(
            'left_gripper_force_ratio_topic',
            '/control_set_left_gripper_force_ratio_0_307',
        )
        self.declare_parameter(
            'right_gripper_force_ratio_topic',
            '/control_set_right_gripper_force_ratio_0_307',
        )
        self.declare_parameter('gripper_force_minimum_ratio', 0.0)
        self.declare_parameter('gripper_force_maximum_current', 10.0)
        # Keep the arrival "thinking" cue, but do not leave the operator
        # waiting after the robot reaches the task point.  Height feedback may
        # complete sooner; 1.25 s is only the fail-open upper bound.
        self.declare_parameter('task_height_timeout_seconds', 1.25)
        self.declare_parameter('task_height_stable_seconds', 0.125)
        package_share = Path(
            get_package_share_directory('openarmx_teleop_vr_navigation_306')
        )
        source_task_catalog = Path(
            '/home/ubuntu/ros2_ws/src/openarmx_teleop_vr_navigation_306/config/task_actions.json'
        )
        self.declare_parameter(
            'task_catalog_path', str(
                source_task_catalog
                if source_task_catalog.is_file()
                else package_share / 'config' / 'task_actions.json'
            )
        )
        self.declare_parameter(
            'task_tts_topic', '/openarmx_teleop_vr_navigation_306/task_speech'
        )
        self.declare_parameter(
            'showroom_mode_topic',
            '/openarmx_teleop_vr_navigation_306/showroom_mode',
        )

        self._nav_lock = threading.Lock()
        self._map_png = None
        self._map_version = 0
        self._map_meta = None
        self._pose = None
        self._pose_time = 0.0
        self._navigation_status = {'state': 'waiting', 'detail': '等待导航状态'}
        self._relocalization_status = {'state': 'waiting', 'detail': '等待全局重定位'}
        self._waypoints = []
        self._waypoint_poses = {}
        self._waypoint_query_pending = False
        self._waypoint_query_generation = 0

        self._drive_lock = threading.Lock()
        self._drive_sample = None
        self._drive_time = 0.0
        self._drive_was_active = False
        self._drive_current = [0.0, 0.0, 0.0]
        self._last_drive_tick = time.monotonic()
        self._drive_settle_until = 0.0
        self._pause_requested_for_drive = False
        self._navigation_paused_for_drive = False
        self._resume_requested_for_drive = False
        self._drive_resume_retry_at = 0.0
        self._navigation_websocket = None
        self._drive_websocket = None
        self._gripper_force_ratio = 1.0
        self._corridor_lock = threading.Lock()
        self._corridor_payload = None
        self._corridor_received_at = 0.0
        self._monitor_lock = threading.Lock()
        self._monitor_view = {
            'layout_version': 2,
            'canvas_width': 1200,
            'canvas_height': 900,
            'title': '云蝶 V29',
            'navigation_visible': False,
            'menu_visible': False,
            'camera_mode': 'unknown',
            'camera_available': False,
            'depth_renderer': 'disabled',
            'depth_valid_points': 0,
            'depth_point_capacity': 0,
            'depth_last_error': '',
            'head_following': False,
            'base_control_latched': False,
            'selected_label': '',
            'selected_waypoint': '',
            'menu_title': '航点与操作',
            'menu_rows': [],
            'naming_active': False,
            'naming_draft': '',
            'name_key_index': 0,
            'manual_relocalization': None,
            'vr_camera_fps': 0.0,
        }
        self._monitor_view_received_at = 0.0
        self._map_library_directories = [
            Path(value).expanduser().resolve()
            for value in self.get_parameter('map_library_directories').value
        ]
        self._active_map_config = Path(
            str(self.get_parameter('active_map_config').value)
        ).expanduser().resolve()
        self._map_switch_in_progress = False
        self._task_catalog_path = Path(
            str(self.get_parameter('task_catalog_path').value)
        ).expanduser().resolve()
        task_catalog = json.loads(self._task_catalog_path.read_text(encoding='utf-8'))
        catalog_stat = self._task_catalog_path.stat()
        self._task_catalog_revision = (
            catalog_stat.st_mtime_ns, catalog_stat.st_size
        )
        self._task_workflow = TaskWorkflow(task_catalog)
        self._task_templates = task_chain_templates(task_catalog)
        showroom = task_catalog.get('showroom_mode', {})
        self._showroom_mode_enabled = False
        self._showroom_single_task_active = False
        self._showroom_waypoint_task = str(
            showroom.get('single_waypoint_task', '展厅定点服务')
        ).strip() or '展厅定点服务'
        self._showroom_height_level = max(
            1, min(5, int(showroom.get('body_height_level', 5)))
        )
        self._task_height_timeout_seconds = max(
            0.5, float(self.get_parameter('task_height_timeout_seconds').value)
        )
        self._task_height_stable_seconds = max(
            0.05, float(self.get_parameter('task_height_stable_seconds').value)
        )
        self._task_height_lock = threading.Lock()
        self._task_height_target_level = None
        self._task_height_deadline = 0.0
        self._task_height_stable_since = 0.0

        transient_qos = QoSProfile(depth=1)
        transient_qos.reliability = ReliabilityPolicy.RELIABLE
        transient_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        status_qos = QoSProfile(depth=10)
        status_qos.reliability = ReliabilityPolicy.RELIABLE
        status_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        latest_qos = QoSProfile(depth=1)
        latest_qos.reliability = ReliabilityPolicy.BEST_EFFORT
        latest_qos.durability = DurabilityPolicy.VOLATILE

        self._initial_pose_publisher = self.create_publisher(
            PoseWithCovarianceStamped,
            str(self.get_parameter('initial_pose_topic').value),
            transient_qos,
        )
        self._drive_publisher = self.create_publisher(
            Twist, str(self.get_parameter('manual_cmd_vel_topic').value), 10
        )
        self._manual_override_publisher = self.create_publisher(
            Bool, str(self.get_parameter('manual_override_topic').value), 10
        )
        self._body_height_publisher = self.create_publisher(
            String,
            str(self.get_parameter('body_height_command_topic').value),
            10,
        )
        self._left_gripper_force_publisher = self.create_publisher(
            String,
            str(self.get_parameter('left_gripper_force_ratio_topic').value),
            10,
        )
        self._right_gripper_force_publisher = self.create_publisher(
            String,
            str(self.get_parameter('right_gripper_force_ratio_topic').value),
            10,
        )
        self._task_tts_publisher = self.create_publisher(
            String, str(self.get_parameter('task_tts_topic').value), 10
        )
        self._showroom_mode_publisher = self.create_publisher(
            Bool,
            str(self.get_parameter('showroom_mode_topic').value),
            status_qos,
        )
        self._publish_showroom_mode()
        self.create_subscription(
            OccupancyGrid,
            str(self.get_parameter('map_topic').value),
            self._on_map,
            transient_qos,
        )
        self.create_subscription(
            PoseWithCovarianceStamped,
            str(self.get_parameter('amcl_pose_topic').value),
            self._on_pose,
            10,
        )
        self.create_subscription(
            String,
            str(self.get_parameter('navigation_status_topic').value),
            self._on_navigation_status,
            status_qos,
        )
        self.create_subscription(
            String,
            str(self.get_parameter('relocalization_status_topic').value),
            self._on_relocalization_status,
            status_qos,
        )
        self.create_subscription(
            MarkerArray,
            str(self.get_parameter('waypoint_marker_topic').value),
            self._on_waypoint_markers,
            transient_qos,
        )
        self.create_subscription(
            String,
            str(self.get_parameter('corridor_projection_topic').value),
            self._on_corridor_projection,
            latest_qos,
        )
        self._list_waypoints_client = self.create_client(ListWaypoints, '/list_waypoints')
        self._navigate_waypoint_client = self.create_client(
            NavigateToWaypoint, '/navigate_to_waypoint'
        )
        self._save_waypoint_client = self.create_client(SaveWaypoint, '/save_waypoint')
        self._delete_waypoint_client = self.create_client(
            DeleteWaypoint, '/delete_waypoint'
        )
        self._pause_navigation_client = self.create_client(Trigger, '/pause_navigation')
        self._cancel_navigation_client = self.create_client(Trigger, '/cancel_navigation')
        self._resume_navigation_client = self.create_client(Trigger, '/resume_navigation')
        self._relocalize_client = self.create_client(
            Trigger, str(self.get_parameter('relocalization_service').value)
        )
        self._load_map_client = self.create_client(LoadMap, '/map_server/load_map')
        self._switch_waypoint_map_client = self.create_client(
            SwitchWaypointMap, '/switch_waypoint_map'
        )
        self.create_timer(0.02, self._drive_tick)
        self.create_timer(1.0, self._request_waypoint_list)
        self.create_timer(0.05, self._task_height_tick)

        self._base_web_dir = self._web_dir
        self._web_dir = (
            Path(get_package_share_directory('openarmx_teleop_vr_navigation_306')) / 'web'
        ).resolve()
        self._precision_cloud_stream = None
        self._precision_cloud_websocket = None
        if bool(self.get_parameter('precision_cloud_enabled').value):
            self._precision_cloud_stream = DepthCloudStream(
                maximum_fps=float(
                    self.get_parameter('precision_cloud_maximum_fps').value
                ),
                sample_count=int(
                    self.get_parameter('precision_cloud_point_count').value
                ),
                include_color=True,
            )
        self._navigation_ready.set()
        self.get_logger().info(
            'VR navigation bridge ready: left stick=XY, right stick=turn, '
            'left stick click toggles base control ownership'
        )

    def _run_server(self):
        if not self._navigation_ready.wait(timeout=10.0):
            self._server_error = 'navigation bridge initialization timed out'
            return
        super()._run_server()

    async def _api_config(self, request):
        base_response = await super()._api_config(request)
        payload = json.loads(base_response.text)
        payload['navigation'] = {
            'enabled': True,
            'ui_version': 'yundie-306-navigation-50-map-70-v48-task-retry',
            'panel_toggle_button': 'Y',
            'confirm_button': 'A',
            'relocalize_button': 'X',
            'drive_control': 'left_thumbstick_toggle',
            'state_url': '/api/navigation/state',
            'map_url': '/api/navigation/map.png',
            'websocket_path': '/navigation/ws',
            'drive_websocket_path': '/navigation/drive',
            'gripper_force_ratio': self._gripper_force_ratio,
            'gripper_force_minimum_ratio': float(
                self.get_parameter('gripper_force_minimum_ratio').value
            ),
            'gripper_force_maximum_current': float(
                self.get_parameter('gripper_force_maximum_current').value
            ),
            'gripper_force_step': 0.1,
            'showroom_mode_enabled': self._showroom_mode_enabled,
            'precision_cloud': {
                'enabled': self._precision_cloud_stream is not None,
                'websocket_path': '/navigation/precision-cloud',
                'point_count': int(
                    self.get_parameter('precision_cloud_point_count').value
                ),
                'maximum_fps': float(
                    self.get_parameter('precision_cloud_maximum_fps').value
                ),
                'minimum_m': float(
                    self.get_parameter('depth_cloud_minimum_m').value
                ),
                'maximum_m': float(
                    self.get_parameter('depth_cloud_maximum_m').value
                ),
                'flow_control': 'render_ack_latest_v1',
                'producer_status': (
                    self._precision_cloud_stream.status()
                    if self._precision_cloud_stream is not None else None
                ),
            },
        }
        return web.json_response(payload)

    def _on_corridor_projection(self, message):
        try:
            payload = json.loads(message.data)
            if not isinstance(payload, dict) or payload.get('type') != 'corridor_projection':
                raise ValueError('unexpected corridor payload type')
            sequence = int(payload.get('sequence', -1))
            if sequence < 0:
                raise ValueError('missing corridor sequence')
            # Bound a malformed producer before it reaches the browser.
            for key in ('left', 'centre', 'right'):
                points = payload.get(key, [])
                if not isinstance(points, list) or len(points) > 40:
                    raise ValueError(f'invalid corridor {key} points')
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self.get_logger().warning(f'ignored invalid corridor projection: {error}')
            return
        with self._corridor_lock:
            self._corridor_payload = payload
            self._corridor_received_at = time.monotonic()

    async def _corridor_websocket_sender(self, websocket):
        last_sequence = None
        stale_sent = False
        while not websocket.closed:
            with self._corridor_lock:
                payload = self._corridor_payload
                age = time.monotonic() - self._corridor_received_at
            if payload is not None and age <= 0.35:
                sequence = int(payload.get('sequence', -1))
                if sequence != last_sequence:
                    await websocket.send_str(json.dumps(payload, separators=(',', ':')))
                    last_sequence = sequence
                    stale_sent = False
            elif not stale_sent:
                await websocket.send_str(json.dumps({
                    'type': 'corridor_projection',
                    'sequence': -1,
                    'valid': False,
                    'reason': 'corridor projection stale',
                }, separators=(',', ':')))
                stale_sent = True
            await asyncio.sleep(1.0 / 30.0)

    async def _index(self, request):
        del request
        return web.FileResponse(
            self._web_dir / 'index.html',
            headers={
                'Cache-Control': 'no-store, no-cache, must-revalidate',
                'Clear-Site-Data': '"cache"',
            },
        )

    async def _asset(self, request):
        relative = Path(request.match_info['asset'])
        if relative.is_absolute() or '..' in relative.parts:
            raise web.HTTPForbidden()
        local_file = self._web_dir / relative
        if local_file.is_file():
            return web.FileResponse(
                local_file,
                headers={'Cache-Control': 'no-store, no-cache, must-revalidate'},
            )
        base_file = self._base_web_dir / relative
        if base_file.is_file():
            return web.FileResponse(
                base_file,
                headers={'Cache-Control': 'no-store, no-cache, must-revalidate'},
            )
        raise web.HTTPNotFound()

    def _on_map(self, message):
        try:
            png = occupancy_grid_png(message.info.width, message.info.height, message.data)
            origin = message.info.origin
            meta = {
                'width': int(message.info.width),
                'height': int(message.info.height),
                'resolution': float(message.info.resolution),
                'origin_x': float(origin.position.x),
                'origin_y': float(origin.position.y),
                'origin_yaw': quaternion_yaw(origin.orientation),
                'frame_id': message.header.frame_id or 'map',
            }
        except Exception as error:
            self.get_logger().error(f'Failed to encode navigation map: {error}')
            return
        with self._nav_lock:
            self._map_png = png
            self._map_meta = meta
            self._map_version += 1

    def _on_pose(self, message):
        pose = message.pose.pose
        with self._nav_lock:
            self._pose = {
                'x': float(pose.position.x),
                'y': float(pose.position.y),
                'yaw': quaternion_yaw(pose.orientation),
            }
            self._pose_time = time.monotonic()

    @staticmethod
    def _status_payload(message):
        try:
            payload = json.loads(message.data)
            return payload if isinstance(payload, dict) else {'state': str(payload)}
        except (TypeError, ValueError):
            return {'state': 'update', 'detail': str(message.data)}

    def _on_navigation_status(self, message):
        status = self._status_payload(message)
        with self._nav_lock:
            self._navigation_status = status
        state = str(status.get('state', '')).lower()
        waypoint = str(status.get('active_waypoint', '')).strip()
        outcome = navigation_terminal_outcome(state)
        if outcome == 'succeeded':
            task = self._task_workflow.navigation_arrived(waypoint)
            if task is not None:
                # One concise arrival cue replaces the former two-message
                # sequence and its artificial silence.  Body-height adjustment
                # continues quietly in parallel.
                self._speak_task(task_voice_text(
                    'arrived', task, self._showroom_single_task_active
                ))
                self._set_task_height_level(task['body_height_level'])
        elif outcome == 'failed':
            current = self._task_workflow.current()
            self._task_workflow.navigation_failed(
                f"前往{waypoint or (current or {}).get('location', '目标地点')}失败"
            )

    def _speak_task(self, text):
        if not self._showroom_mode_enabled or not text or not rclpy.ok():
            return
        self._task_tts_publisher.publish(String(data=json.dumps({
            'status': 'play',
            'text': str(text),
        }, ensure_ascii=False, separators=(',', ':'))))

    def _publish_showroom_mode(self):
        self._showroom_mode_publisher.publish(
            Bool(data=bool(self._showroom_mode_enabled))
        )

    def _reload_task_catalog_if_changed(self):
        """Refresh task capabilities before accepting a newly submitted plan.

        The voice server already hot-reloads hints.  Reloading the execution
        workflow here keeps validation in lockstep, so adding/removing a config
        location or task does not require rebuilding or restarting this package.
        """
        stat = self._task_catalog_path.stat()
        revision = (stat.st_mtime_ns, stat.st_size)
        if revision == self._task_catalog_revision:
            return False
        catalog = json.loads(self._task_catalog_path.read_text(encoding='utf-8'))
        workflow = TaskWorkflow(catalog)
        templates = task_chain_templates(catalog)
        showroom = catalog.get('showroom_mode', {})
        self._task_workflow = workflow
        self._task_templates = templates
        self._showroom_waypoint_task = str(
            showroom.get('single_waypoint_task', '展厅定点服务')
        ).strip() or '展厅定点服务'
        self._showroom_height_level = max(
            1, min(5, int(showroom.get('body_height_level', 5)))
        )
        self._task_catalog_revision = revision
        capability_count = sum(
            len(entries) for entries in catalog.get('task_locations', {}).values()
            if isinstance(entries, list)
        )
        self.get_logger().info(
            f'task catalog hot-reloaded: {capability_count} capabilities'
        )
        return True

    def _set_task_height_level(self, level):
        with self._task_height_lock:
            self._task_height_target_level = max(1, min(5, int(level)))
            self._task_height_deadline = (
                time.monotonic() + self._task_height_timeout_seconds
            )
            self._task_height_stable_since = 0.0

    def _release_task_height_for_manual_control(self):
        """Hand task-height ownership to X/Y without stranding the mission."""
        with self._task_height_lock:
            self._task_height_target_level = None
            self._task_height_deadline = 0.0
            self._task_height_stable_since = 0.0
        task = self._task_workflow.height_adjusted()
        if task is not None:
            self.get_logger().info(
                'manual body-height control superseded the task preset; '
                'operator action stage released'
            )
        return task is not None

    def _task_height_tick(self):
        with self._task_height_lock:
            level = self._task_height_target_level
            now = time.monotonic()
            active = level is not None and now < self._task_height_deadline
            reached = False
            if active:
                status, _age, fresh = self._teleop_status_snapshot()
                lowering = (status or {}).get('body_height', {}).get('lowering_m')
                try:
                    target = 0.56 * (5 - level) / 4.0
                    close = fresh and abs(float(lowering) - target) <= 0.008
                except (TypeError, ValueError):
                    close = False
                if close:
                    if self._task_height_stable_since <= 0.0:
                        self._task_height_stable_since = now
                    reached = (
                        now - self._task_height_stable_since
                        >= self._task_height_stable_seconds
                    )
                else:
                    self._task_height_stable_since = 0.0
            if level is not None and (not active or reached):
                self._task_height_target_level = None
                active = False
            timed_out = level is not None and not active and not reached
        if not rclpy.ok() or level is None:
            return
        payload = (
            {'enabled': True, 'direction': 0.0, 'target_level': level}
            if active else {'enabled': False, 'direction': 0.0}
        )
        self._body_height_publisher.publish(String(data=json.dumps(
            payload, separators=(',', ':')
        )))
        if timed_out:
            # Height adjustment is an operator-assistance step, not a reason to
            # strand the whole mission.  Continue into the action stage when
            # feedback is unavailable or the target is not reached in time.
            self._task_workflow.height_adjusted()
        elif not active:
            self._task_workflow.height_adjusted()

    def _on_relocalization_status(self, message):
        payload = self._status_payload(message)
        with self._nav_lock:
            self._relocalization_status = payload
        state = str(payload.get('state', '')).strip().lower()
        if state in {'failed', 'failure', 'aborted', 'rejected'}:
            self._task_workflow.manual_relocalization_required(
                '自动重定位失败，请在导航界面进行手动重定位后再次确认任务'
            )

    @staticmethod
    def _waypoint_pose_from_marker(marker):
        if marker.action != Marker.ADD or marker.ns != 'waypoint_headings':
            return None
        name = str(marker.text).strip()
        if not name:
            return None
        x = float(marker.pose.position.x)
        y = float(marker.pose.position.y)
        yaw = quaternion_yaw(marker.pose.orientation)
        if not all(math.isfinite(value) for value in (x, y, yaw)):
            return None
        return name, {
            'name': name,
            'x': x,
            'y': y,
            'yaw': yaw,
            'frame_id': marker.header.frame_id or 'map',
        }

    def _on_waypoint_markers(self, message):
        poses = {}
        clear_requested = False
        for marker in message.markers:
            if marker.action == Marker.DELETEALL:
                clear_requested = True
                continue
            parsed = self._waypoint_pose_from_marker(marker)
            if parsed is not None:
                name, pose = parsed
                poses[name] = pose
        with self._nav_lock:
            if clear_requested:
                self._waypoint_poses.clear()
            if poses:
                # The waypoint manager republishes the full MarkerArray after
                # every map/waypoint change, so replacing prevents stale poses.
                self._waypoint_poses = poses

    def _request_waypoint_list(self, force=False):
        if not self._list_waypoints_client.service_is_ready():
            return
        if self._waypoint_query_pending and not force:
            return
        self._waypoint_query_generation += 1
        generation = self._waypoint_query_generation
        self._waypoint_query_pending = True
        future = self._list_waypoints_client.call_async(ListWaypoints.Request())

        def complete(result_future):
            if generation != self._waypoint_query_generation:
                return
            self._waypoint_query_pending = False
            try:
                result = result_future.result()
                if result.success:
                    with self._nav_lock:
                        self._waypoints = list(result.names)
            except Exception as error:
                self.get_logger().warning(f'Waypoint list request failed: {error}')

        future.add_done_callback(complete)

    def _navigation_is_active(self):
        with self._nav_lock:
            state = str(self._navigation_status.get('state', '')).lower()
        if navigation_terminal_outcome(state) is not None:
            return False
        return state not in {
            '', 'idle', 'waiting', 'paused', 'succeeded', 'failed', 'rejected',
            'canceled', 'cancelled', 'sequence_succeeded', 'map_ready',
        }

    def _set_drive_sample(self, payload):
        deadman = bool(payload.get('deadman', False))
        sample = {
            'deadman': deadman,
            'forward': shaped_axis(payload.get('forward', 0.0)),
            'lateral': shaped_axis(payload.get('lateral', 0.0)),
            'turn': shaped_axis(payload.get('turn', 0.0)),
        }
        now = time.monotonic()
        with self._drive_lock:
            was_deadman = bool(self._drive_sample and self._drive_sample.get('deadman'))
            self._drive_sample = sample
            self._drive_time = now
            if deadman and not was_deadman:
                settle = (
                    float(self.get_parameter('drive_takeover_settle_seconds').value)
                    if self._navigation_is_active()
                    else 0.0
                )
                self._drive_settle_until = now + max(0.0, settle)
                self._pause_requested_for_drive = False
        height_enabled = bool(deadman and payload.get('body_height_enabled', False))
        try:
            height_direction = float(payload.get('body_height_direction', 0.0))
        except (TypeError, ValueError):
            height_direction = 0.0
        height_direction = max(-1.0, min(1.0, height_direction))
        if not height_enabled:
            height_direction = 0.0
        elif abs(height_direction) > 1.0e-6:
            # A deliberate X/Y command always takes ownership from an
            # automatic task-height preset. Advancing the workflow here is
            # essential: clearing only the target leaves it permanently in
            # ``adjusting_height``, which hides the hold-A completion prompt.
            self._release_task_height_for_manual_control()
        if rclpy.ok():
            self._body_height_publisher.publish(String(data=json.dumps({
                'enabled': height_enabled,
                'direction': height_direction,
            }, separators=(',', ':'))))

    def _set_gripper_force_ratio(self, payload):
        ratio = float(payload.get('ratio'))
        minimum = float(self.get_parameter('gripper_force_minimum_ratio').value)
        if not math.isfinite(ratio) or ratio < minimum or ratio > 1.0:
            raise ValueError(f'gripper force ratio must be within [{minimum}, 1.0]')
        ratio = round(ratio * 10.0) / 10.0
        if (
            self._left_gripper_force_publisher.get_subscription_count() < 1
            or self._right_gripper_force_publisher.get_subscription_count() < 1
        ):
            raise RuntimeError('厂商夹爪力度接口未连接，设置未发送')
        # The vendor callback expects the key ``force``.  It multiplies this
        # normalized value by the configured 10 A gripper-current ceiling.
        # The old ``ratio`` key was silently replaced by the vendor default
        # 0.5, which made every menu setting apply the same 5 A limit.
        message = String(data=json.dumps({'force': ratio}, separators=(',', ':')))
        self._left_gripper_force_publisher.publish(message)
        self._right_gripper_force_publisher.publish(message)
        self._gripper_force_ratio = ratio
        return ratio

    async def _api_gripper_force(self, request):
        """Apply gripper force through a request/response path.

        The VR menu used to send this setting only through the optional
        navigation WebSocket.  When that socket was reconnecting the local UI
        still changed, although no ROS command reached either gripper.  Keep
        the WebSocket handler for cached clients, but give current clients an
        independently acknowledged control path.
        """
        try:
            ratio = self._set_gripper_force_ratio(await request.json())
            return web.json_response({
                'success': True,
                'ratio': ratio,
                'current_limit': round(
                    ratio * float(
                        self.get_parameter('gripper_force_maximum_current').value
                    ), 2
                ),
                'message': f'夹爪力度已应用：{round(ratio * 100):d}%',
            })
        except (RuntimeError, TypeError, ValueError) as error:
            return web.json_response(
                {'success': False, 'message': str(error)}, status=400
            )

    def _publish_stop(self):
        self._drive_current = [0.0, 0.0, 0.0]
        self._drive_publisher.publish(Twist())

    def _publish_manual_override(self, active):
        self._manual_override_publisher.publish(Bool(data=bool(active)))

    @staticmethod
    def _approach(current, target, maximum_step):
        return current + max(-maximum_step, min(maximum_step, target - current))

    def _pause_for_drive(self):
        if self._pause_requested_for_drive:
            return
        if not self._pause_navigation_client.service_is_ready():
            return
        self._pause_requested_for_drive = True
        future = self._pause_navigation_client.call_async(Trigger.Request())

        def complete(done):
            success = False
            try:
                result = done.result()
                success = bool(result and result.success)
            except Exception as error:
                self.get_logger().warning(f'Navigation pause for manual drive failed: {error}')
            with self._drive_lock:
                self._navigation_paused_for_drive = success
                if not success:
                    self._pause_requested_for_drive = False
            if success:
                self.get_logger().info('Navigation paused for manual base takeover')

        future.add_done_callback(complete)

    def _resume_after_drive(self, now):
        with self._drive_lock:
            paused = self._navigation_paused_for_drive
            pending = self._resume_requested_for_drive
            retry_at = self._drive_resume_retry_at
        if not paused or pending or now < retry_at:
            return
        if not self._resume_navigation_client.service_is_ready():
            return
        with self._drive_lock:
            self._resume_requested_for_drive = True
        future = self._resume_navigation_client.call_async(Trigger.Request())

        def complete(done):
            success = False
            message = ''
            try:
                result = done.result()
                success = bool(result and result.success)
                message = str(result.message if result else '')
            except Exception as error:
                message = str(error)
            with self._drive_lock:
                self._resume_requested_for_drive = False
                if success:
                    self._navigation_paused_for_drive = False
                    self._pause_requested_for_drive = False
                else:
                    self._drive_resume_retry_at = time.monotonic() + 0.5
            if success:
                self.get_logger().info('Navigation resumed after manual base takeover')
            else:
                self.get_logger().warning(
                    f'Navigation resume after manual drive failed: {message}'
                )

        future.add_done_callback(complete)

    def _drive_tick(self):
        now = time.monotonic()
        dt = max(0.001, min(0.05, now - self._last_drive_tick))
        self._last_drive_tick = now
        with self._drive_lock:
            sample = dict(self._drive_sample) if self._drive_sample else None
            sample_age = now - self._drive_time
            settle_until = self._drive_settle_until

        active = bool(
            sample
            and sample.get('deadman')
            and sample_age <= float(self.get_parameter('drive_watchdog_seconds').value)
        )
        if not active:
            if self._drive_was_active or any(abs(value) > 1e-6 for value in self._drive_current):
                self._publish_stop()
            self._publish_manual_override(False)
            self._drive_was_active = False
            self._resume_after_drive(now)
            return

        # Claim arbitration before publishing the first non-zero velocity.
        self._publish_manual_override(True)

        if now < settle_until:
            self._pause_for_drive()
            if self._drive_was_active:
                self._publish_stop()
            self._drive_was_active = True
            return

        target = [
            sample['forward'] * float(self.get_parameter('maximum_forward_speed').value),
            sample['lateral'] * float(self.get_parameter('maximum_lateral_speed').value),
            sample['turn'] * float(self.get_parameter('maximum_yaw_speed').value),
        ]
        linear_step = float(self.get_parameter('linear_acceleration').value) * dt
        yaw_step = float(self.get_parameter('yaw_acceleration').value) * dt
        self._drive_current[0] = self._approach(self._drive_current[0], target[0], linear_step)
        self._drive_current[1] = self._approach(self._drive_current[1], target[1], linear_step)
        self._drive_current[2] = self._approach(self._drive_current[2], target[2], yaw_step)
        command = Twist()
        command.linear.x, command.linear.y, command.angular.z = self._drive_current
        self._drive_publisher.publish(command)
        self._drive_was_active = True

    async def _navigation_websocket_handler(self, request):
        peer = request.remote or 'unknown'
        with self._client_lock:
            if self._control_peer is not None and self._control_peer != peer:
                raise web.HTTPConflict(text='navigation control belongs to the active VR client')
        websocket = web.WebSocketResponse(heartbeat=10.0, max_msg_size=4096)
        previous = self._navigation_websocket
        self._navigation_websocket = websocket
        await websocket.prepare(request)
        if previous is not None and previous is not websocket:
            try:
                await previous.close(code=1001, message=b'replaced by current navigation session')
            except Exception:
                pass
        sender = asyncio.create_task(self._corridor_websocket_sender(websocket))
        try:
            async for message in websocket:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(message.data)
                    if isinstance(payload, dict) and payload.get('type') == 'base_drive':
                        # Backward compatibility for an old cached page. The
                        # dedicated worker connection owns drive data whenever
                        # it is present.
                        if self._drive_websocket is None or self._drive_websocket.closed:
                            self._update_monitor_view(payload.get('view'))
                            self._set_drive_sample(payload)
                    elif isinstance(payload, dict) and payload.get('type') == 'monitor_view':
                        self._update_monitor_view(payload.get('view'))
                    elif isinstance(payload, dict) and payload.get('type') == 'gripper_force':
                        ratio = self._set_gripper_force_ratio(payload)
                        await websocket.send_str(json.dumps({
                            'type': 'gripper_force_state',
                            'ratio': ratio,
                        }, separators=(',', ':')))
                except (TypeError, ValueError):
                    continue
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            if self._navigation_websocket is websocket:
                self._navigation_websocket = None
                if self._drive_websocket is None or self._drive_websocket.closed:
                    self._set_drive_sample({'deadman': False})
        return websocket

    async def _drive_websocket_handler(self, request):
        peer = request.remote or 'unknown'
        with self._client_lock:
            if self._control_peer is not None and self._control_peer != peer:
                raise web.HTTPConflict(text='drive control belongs to the active VR client')
        websocket = web.WebSocketResponse(heartbeat=5.0, max_msg_size=2048)
        previous = self._drive_websocket
        self._drive_websocket = websocket
        await websocket.prepare(request)
        if previous is not None and previous is not websocket:
            try:
                await previous.close(code=1001, message=b'replaced by latest drive link')
            except Exception:
                pass
        try:
            async for message in websocket:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(message.data)
                    if not isinstance(payload, dict) or payload.get('type') != 'base_drive':
                        continue
                    self._update_monitor_view(payload.get('view'))
                    self._set_drive_sample(payload)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
        finally:
            if self._drive_websocket is websocket:
                self._drive_websocket = None
                self._set_drive_sample({'deadman': False})
        return websocket

    async def _precision_cloud_websocket_handler(self, request):
        """Send official-style stable depth plus aligned RGB565 samples."""
        return await self._point_cloud_websocket_handler(
            request, self._precision_cloud_stream, 'precision cloud'
        )

    async def _point_cloud_websocket_handler(self, request, source, label):
        if source is None:
            raise web.HTTPNotFound(text=f'{label} is unavailable')
        peer = request.remote or 'unknown'
        with self._client_lock:
            if self._control_peer is not None and self._control_peer != peer:
                raise web.HTTPConflict(
                    text='depth cloud belongs to the active VR client'
                )
        # 18k depth16 + RGB565 samples use 72,004 bytes including sequence.
        # Compression is deliberately disabled: depth and colour fields have
        # low redundancy, and per-message deflate causes latency spikes.
        websocket = web.WebSocketResponse(
            heartbeat=10.0, max_msg_size=256 * 1024, compress=False
        )
        await websocket.prepare(request)
        previous = self._precision_cloud_websocket
        self._precision_cloud_websocket = websocket
        if previous is not None and previous is not websocket:
            try:
                await previous.close(
                    code=1001, message=b'replaced by latest depth link'
                )
            except Exception:
                pass
        source.acquire()
        sequence = -1
        configuration_key = ''
        try:
            while not websocket.closed and not self._stop_requested.is_set():
                # Nonblocking snapshot: no per-frame executor job and no stale
                # worker accumulation after a browser reconnect.
                payload, next_sequence, configuration = source.snapshot(
                    sequence, 0.0
                )
                if configuration is not None:
                    configuration['minimum_m'] = float(
                        self.get_parameter('depth_cloud_minimum_m').value
                    )
                    configuration['maximum_m'] = float(
                        self.get_parameter('depth_cloud_maximum_m').value
                    )
                    key = json.dumps(configuration, sort_keys=True)
                    if key != configuration_key:
                        await websocket.send_str(json.dumps(
                            configuration, separators=(',', ':')
                        ))
                        configuration_key = key
                if payload is None or next_sequence == sequence:
                    await asyncio.sleep(0.005)
                    continue
                sequence = int(next_sequence)
                await websocket.send_bytes(
                    struct.pack('<I', sequence) + payload
                )
                if not await self._wait_for_point_cloud_ack(
                        websocket, sequence):
                    break
        except (
                ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                asyncio.CancelledError, RuntimeError):
            pass
        finally:
            source.release()
            if self._precision_cloud_websocket is websocket:
                self._precision_cloud_websocket = None
            if not websocket.closed:
                await websocket.close()
        return websocket

    async def _wait_for_point_cloud_ack(self, websocket, sent_sequence):
        """Permit one in-flight cloud frame and discard obsolete samples."""
        deadline = time.monotonic() + 5.0
        while (
                not websocket.closed
                and not self._stop_requested.is_set()
                and time.monotonic() < deadline):
            try:
                message = await websocket.receive(timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if message.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                    if (
                            payload.get('type') == 'depth_frame_ack'
                            and int(payload.get('sequence', -1))
                            == int(sent_sequence)):
                        return True
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    continue
            elif message.type in {
                    WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                return False
        return False

    def _update_monitor_view(self, payload):
        if not isinstance(payload, dict):
            return
        camera_mode = str(payload.get('camera_mode', 'unknown'))
        if camera_mode not in {
                'passthrough', 'rgbd', 'precision_cloud'}:
            camera_mode = 'unknown'
        selected_label = str(payload.get('selected_label', '')).strip()[:96]
        menu_rows = []
        raw_menu_rows = payload.get('menu_rows', [])
        if not isinstance(raw_menu_rows, list):
            raw_menu_rows = []
        for row in raw_menu_rows[:5]:
            if not isinstance(row, dict):
                continue
            menu_rows.append({
                'label': str(row.get('label', '')).strip()[:96],
                'selected': bool(row.get('selected', False)),
            })
        manual = payload.get('manual_relocalization')
        safe_manual = None
        if isinstance(manual, dict):
            try:
                x = float(manual.get('x'))
                y = float(manual.get('y'))
                yaw = float(manual.get('yaw', 0.0))
                if all(math.isfinite(value) for value in (x, y, yaw)):
                    safe_manual = {
                        'stage': (
                            'heading' if str(manual.get('stage')) == 'heading'
                            else 'position'
                        ),
                        'x': x,
                        'y': y,
                        'yaw': yaw,
                    }
            except (TypeError, ValueError):
                pass
        try:
            vr_camera_fps = max(0.0, min(120.0, float(payload.get('vr_camera_fps', 0.0))))
        except (TypeError, ValueError):
            vr_camera_fps = 0.0
        try:
            name_key_index = max(0, min(63, int(payload.get('name_key_index', 0))))
        except (TypeError, ValueError):
            name_key_index = 0
        try:
            depth_valid_points = max(
                0, min(120000, int(payload.get('depth_valid_points', 0)))
            )
            depth_point_capacity = max(
                0, min(120000, int(payload.get('depth_point_capacity', 0)))
            )
        except (TypeError, ValueError):
            depth_valid_points = 0
            depth_point_capacity = 0
        view = {
            'layout_version': 2,
            'canvas_width': 1200,
            'canvas_height': 900,
            'title': str(payload.get('title', '云蝶 V29')).strip()[:32],
            'navigation_visible': bool(payload.get('navigation_visible', False)),
            'menu_visible': bool(payload.get('menu_visible', False)),
            'camera_mode': camera_mode,
            'camera_available': bool(payload.get('camera_available', False)),
            'depth_renderer': str(
                payload.get('depth_renderer', 'disabled')
            ).strip()[:48],
            'depth_valid_points': depth_valid_points,
            'depth_point_capacity': depth_point_capacity,
            'depth_last_error': str(
                payload.get('depth_last_error', '')
            ).strip()[:240],
            'head_following': bool(payload.get('head_following', False)),
            'base_control_latched': bool(payload.get('base_control_latched', False)),
            'selected_label': selected_label,
            'selected_waypoint': str(payload.get('selected_waypoint', '')).strip()[:96],
            'menu_title': str(payload.get('menu_title', '航点与操作')).strip()[:32],
            'menu_rows': menu_rows,
            'naming_active': bool(payload.get('naming_active', False)),
            'naming_draft': str(payload.get('naming_draft', '')).strip()[:64],
            'name_key_index': name_key_index,
            'manual_relocalization': safe_manual,
            'vr_camera_fps': round(vr_camera_fps, 1),
        }
        with self._monitor_lock:
            self._monitor_view = view
            self._monitor_view_received_at = time.monotonic()

    async def _api_monitor_state(self, request):
        del request
        with self._monitor_lock:
            view = dict(self._monitor_view)
            age = (
                None if self._monitor_view_received_at <= 0.0
                else time.monotonic() - self._monitor_view_received_at
            )
        return web.json_response({
            'view': view,
            'age': None if age is None else round(age, 3),
            'fresh': bool(age is not None and age <= 1.0),
            'read_only': True,
        })

    async def _monitor_websocket_handler(self, request):
        # Spectators get only the already-produced corridor stream.  They do
        # not acquire VR ownership and cannot send base or arm commands.
        websocket = web.WebSocketResponse(heartbeat=10.0, max_msg_size=1024)
        await websocket.prepare(request)
        sender = asyncio.create_task(self._corridor_websocket_sender(websocket))
        try:
            async for _message in websocket:
                pass
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        return websocket

    async def _monitor_index(self, request):
        del request
        return web.FileResponse(
            self._web_dir / 'monitor.html',
            headers={'Cache-Control': 'no-store, no-cache, must-revalidate'},
        )

    async def _audience_index(self, request):
        """Serve the public, read-only autonomous-task presentation."""
        del request
        return web.FileResponse(
            self._web_dir / 'audience.html',
            headers={'Cache-Control': 'no-store, no-cache, must-revalidate'},
        )

    async def _call_service(self, client, request, timeout=4.0):
        if not client.service_is_ready():
            return False, '对应导航服务尚未就绪'
        future = client.call_async(request)
        deadline = time.monotonic() + timeout
        while not future.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if not future.done():
            return False, '导航服务响应超时'
        try:
            response = future.result()
            return bool(response.success), str(response.message)
        except Exception as error:
            return False, str(error)

    async def _await_service_response(self, client, request, timeout=6.0):
        if not client.service_is_ready():
            raise RuntimeError('对应地图服务尚未就绪')
        future = client.call_async(request)
        deadline = time.monotonic() + timeout
        while not future.done() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if not future.done():
            raise TimeoutError('地图服务响应超时')
        response = future.result()
        if response is None:
            raise RuntimeError('地图服务没有返回结果')
        return response

    def _active_map_path(self):
        try:
            content = yaml.safe_load(
                self._active_map_config.read_text(encoding='utf-8')
            ) or {}
            raw = Path(str(content.get('active_map', ''))).expanduser()
            if not raw.is_absolute():
                raw = self._active_map_config.parent.parent / raw
            resolved = raw.resolve()
            return resolved if resolved.is_file() else None
        except Exception:
            return None

    def _map_catalog(self):
        active = self._active_map_path()
        entries = []
        seen = set()
        for directory in self._map_library_directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob('*.yaml')):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                try:
                    metadata = yaml.safe_load(resolved.read_text(encoding='utf-8')) or {}
                    if not isinstance(metadata, dict) or not metadata.get('image'):
                        continue
                except Exception:
                    continue
                is_active = active is not None and resolved == active
                entries.append({
                    'name': resolved.stem,
                    'label': f'当前地图 · {resolved.stem}' if is_active else resolved.stem,
                    'path': str(resolved),
                    'active': is_active,
                })
                seen.add(resolved)
        return entries

    def _write_active_map_config(self, map_path, waypoints_file):
        map_path = Path(map_path).resolve()
        waypoint_path = Path(waypoints_file).resolve()
        source = self._active_map_config.parent.parent.resolve()
        try:
            stored_map = str(map_path.relative_to(source))
        except ValueError:
            stored_map = str(map_path)
        try:
            stored_waypoints = str(waypoint_path.relative_to(source))
        except ValueError:
            stored_waypoints = str(waypoint_path)
        content = {
            'version': 1,
            'active_map': stored_map,
            'waypoints_file': stored_waypoints,
        }
        temporary = self._active_map_config.with_name(
            f'.{self._active_map_config.name}.{time.time_ns()}.tmp'
        )
        temporary.write_text(
            yaml.safe_dump(content, allow_unicode=True, sort_keys=False),
            encoding='utf-8',
        )
        temporary.replace(self._active_map_config)

    async def _api_navigation_state(self, request):
        del request
        with self._nav_lock:
            map_meta = dict(self._map_meta) if self._map_meta else None
            pose = dict(self._pose) if self._pose else None
            pose_age = None if self._pose_time <= 0 else time.monotonic() - self._pose_time
            payload = {
                'map': map_meta,
                'map_version': self._map_version,
                'map_ready': self._map_png is not None,
                'pose': pose,
                'pose_age': None if pose_age is None else round(pose_age, 3),
                'pose_fresh': bool(pose_age is not None and pose_age < 1.0),
                'waypoints': list(self._waypoints),
                'waypoint_poses': [
                    dict(self._waypoint_poses[name])
                    for name in self._waypoints
                    if name in self._waypoint_poses
                ],
                'navigation': dict(self._navigation_status),
                'relocalization': dict(self._relocalization_status),
                'maps': self._map_catalog(),
            }
        payload['task_dispatch'] = self._task_workflow.snapshot()
        payload['task_templates'] = self._task_templates
        payload['showroom_mode_enabled'] = self._showroom_mode_enabled
        payload['services'] = {
            'waypoints': self._list_waypoints_client.service_is_ready(),
            'navigate': self._navigate_waypoint_client.service_is_ready(),
            'save_waypoint': self._save_waypoint_client.service_is_ready(),
            'delete_waypoint': self._delete_waypoint_client.service_is_ready(),
            'pause': self._pause_navigation_client.service_is_ready(),
            'cancel': self._cancel_navigation_client.service_is_ready(),
            'resume': self._resume_navigation_client.service_is_ready(),
            'relocalize': self._relocalize_client.service_is_ready(),
            'load_map': self._load_map_client.service_is_ready(),
            'switch_waypoint_map': self._switch_waypoint_map_client.service_is_ready(),
        }
        return web.json_response(payload)

    async def _start_current_task_navigation(self):
        task = self._task_workflow.current()
        if task is None:
            raise RuntimeError('没有当前任务')
        self._task_workflow.navigation_started()
        self._speak_task(task_voice_text(
            'departure', task, self._showroom_single_task_active
        ))
        request_message = NavigateToWaypoint.Request()
        request_message.name = task['location']
        success, message = await self._call_service(
            self._navigate_waypoint_client, request_message
        )
        if not success:
            with self._nav_lock:
                pose_age = (
                    None if self._pose_time <= 0.0
                    else time.monotonic() - self._pose_time
                )
                relocalization_state = str(
                    self._relocalization_status.get('state', '')
                ).strip().lower()
            if pose_age is None or pose_age >= 1.0:
                manual_required = relocalization_state in {
                    'failed', 'failure', 'aborted', 'rejected'
                }
                retry_message = (
                    '自动重定位失败，请在导航界面进行手动重定位后再次确认任务'
                    if manual_required
                    else '机器人定位尚未就绪，请等待自动重定位完成后再次确认任务'
                )
                self._task_workflow.manual_relocalization_required(retry_message)
            else:
                self._task_workflow.navigation_failed(message)
            raise RuntimeError(message)
        return message

    async def _api_tasks_submit(self, request):
        try:
            payload = await request.json()
            state = str(self._task_workflow.snapshot().get('state', 'idle'))
            replace_existing = bool(payload.get('replace_existing', False))
            active_states = {
                'preparing', 'navigating', 'navigation_failed',
                'adjusting_height', 'awaiting_action'
            }
            if replace_existing and state in active_states:
                # New speech supersedes the old mission. Stop the active goal
                # first so the robot cannot continue toward an obsolete task
                # while every UI switches to the new version atomically.
                if (
                    self._navigation_is_active()
                    and self._pause_navigation_client.service_is_ready()
                ):
                    await self._call_service(
                        self._pause_navigation_client, Trigger.Request()
                    )
                self._task_workflow.cancel()
            if state not in active_states or replace_existing:
                self._reload_task_catalog_if_changed()
            snapshot = self._task_workflow.submit(
                payload.get('source_text', ''), payload.get('tasks', [])
            )
            self._showroom_single_task_active = False
            return web.json_response({'success': True, 'task_dispatch': snapshot})
        except RuntimeError as error:
            return web.json_response({'success': False, 'message': str(error)}, status=409)
        except (TypeError, ValueError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=422)

    async def _replace_active_task(self):
        state = str(self._task_workflow.snapshot().get('state', 'idle'))
        if state not in {
            'preparing', 'navigating', 'navigation_failed',
            'adjusting_height', 'awaiting_action'
        }:
            return
        if self._navigation_is_active() and self._pause_navigation_client.service_is_ready():
            await self._call_service(self._pause_navigation_client, Trigger.Request())
        self._task_workflow.cancel('已由新的本地任务流程替换')

    async def _api_tasks_template_start(self, request):
        try:
            payload = await request.json()
            identifier = str(payload.get('id', '')).strip()
            template = next(
                (item for item in self._task_templates if item['id'] == identifier), None
            )
            if template is None:
                raise ValueError('请选择有效的预设任务流程')
            await self._replace_active_task()
            self._task_workflow.submit(
                f"VR预设流程：{template['name']}", template['tasks']
            )
            self._showroom_single_task_active = False
            self._speak_task(f"已启动{template['name']}，共{template['task_count']}项任务。")
            return await self._api_tasks_confirm(None)
        except ValueError as error:
            return web.json_response({'success': False, 'message': str(error)}, status=422)
        except RuntimeError as error:
            return web.json_response({'success': False, 'message': str(error)}, status=409)

    async def _api_showroom_waypoint_start(self, request):
        try:
            if not self._showroom_mode_enabled:
                raise RuntimeError('展厅模式未开启')
            payload = await request.json()
            name = str(payload.get('name', '')).strip()
            with self._nav_lock:
                known = name in self._waypoints
            if not name or not known:
                raise ValueError('请选择有效航点')
            await self._replace_active_task()
            self._task_workflow.submit(
                f"展厅模式单点任务：{name}",
                [{
                    'location': name,
                    'task': self._showroom_waypoint_task,
                    'body_height_level': self._showroom_height_level,
                }],
                allow_unlisted=True,
            )
            self._showroom_single_task_active = True
            return await self._api_tasks_confirm(None)
        except ValueError as error:
            return web.json_response({'success': False, 'message': str(error)}, status=422)
        except RuntimeError as error:
            return web.json_response({'success': False, 'message': str(error)}, status=409)

    async def _api_showroom_mode(self, request):
        try:
            payload = await request.json()
            self._showroom_mode_enabled = bool(payload.get('enabled', False))
            self._publish_showroom_mode()
            state = '开启' if self._showroom_mode_enabled else '关闭'
            return web.json_response({
                'success': True,
                'enabled': self._showroom_mode_enabled,
                'message': f'展厅模式已{state}',
            })
        except (TypeError, ValueError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=400)

    async def _api_tasks_confirm(self, request):
        del request
        try:
            with self._nav_lock:
                waypoints = list(self._waypoints)
                pose_available = self._pose is not None
                relocalization_state = str(
                    self._relocalization_status.get('state', '')
                ).strip().lower()
            manual_required = relocalization_state in {
                'failed', 'failure', 'aborted', 'rejected'
            }
            if not pose_available or manual_required:
                message = (
                    '自动重定位失败，请在导航界面进行手动重定位后再次确认任务'
                    if manual_required
                    else '机器人定位尚未就绪，请等待自动重定位完成后再次确认任务'
                )
                self._task_workflow.manual_relocalization_required(message)
                return web.json_response({
                    'success': False,
                    'message': message,
                    'manual_relocalization_required': manual_required,
                    'task_dispatch': self._task_workflow.snapshot(),
                }, status=409)
            self._task_workflow.confirm(waypoints)
            message = await self._start_current_task_navigation()
            return web.json_response({
                'success': True,
                'message': message,
                'task_dispatch': self._task_workflow.snapshot(),
            })
        except (RuntimeError, ValueError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=409)

    async def _api_tasks_complete(self, request):
        del request
        try:
            result, completed, upcoming = self._task_workflow.complete_action()
            showroom_single = self._showroom_single_task_active
            self._speak_task(task_voice_text(
                'completed', completed, showroom_single
            ))
            if result == 'next':
                await asyncio.sleep(0.25)
                await self._start_current_task_navigation()
            else:
                if not showroom_single:
                    self._speak_task('全部任务已完成。')
                self._showroom_single_task_active = False
            return web.json_response({
                'success': True,
                'result': result,
                'upcoming': upcoming,
                'task_dispatch': self._task_workflow.snapshot(),
            })
        except (RuntimeError, ValueError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=409)

    async def _api_tasks_retry_navigation_failure(self, request):
        del request
        try:
            if self._navigation_is_active():
                stop_client = (
                    self._cancel_navigation_client
                    if self._cancel_navigation_client.service_is_ready()
                    else self._pause_navigation_client
                )
                success, message = await self._call_service(
                    stop_client, Trigger.Request()
                )
                if not success:
                    raise RuntimeError(message or '当前导航尚未安全停止，暂不能重试')
            current = self._task_workflow.retry_navigation_failure()
            await asyncio.sleep(0.10)
            await self._start_current_task_navigation()
            return web.json_response({
                'success': True,
                'message': f"正在重新前往{current['location']}",
                'current': current,
                'task_dispatch': self._task_workflow.snapshot(),
            })
        except (RuntimeError, ValueError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=409)

    async def _api_tasks_skip_navigation_failure(self, request):
        del request
        try:
            result, skipped, upcoming = self._task_workflow.skip_navigation_failure()
            if result == 'next':
                await asyncio.sleep(0.25)
                await self._start_current_task_navigation()
            else:
                self._showroom_single_task_active = False
            return web.json_response({
                'success': True,
                'message': (
                    f"已跳过{skipped['location']}，正在前往下一任务"
                    if result == 'next' else '最后一个未到达点位已跳过'
                ),
                'result': result,
                'upcoming': upcoming,
                'task_dispatch': self._task_workflow.snapshot(),
            })
        except (RuntimeError, ValueError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=409)

    async def _api_tasks_cancel(self, request):
        del request
        if self._navigation_is_active() and self._pause_navigation_client.service_is_ready():
            await self._call_service(self._pause_navigation_client, Trigger.Request())
        self._task_workflow.cancel()
        self._showroom_single_task_active = False
        self._speak_task('任务串已取消。')
        return web.json_response({
            'success': True, 'task_dispatch': self._task_workflow.snapshot()
        })

    async def _api_navigation_map(self, request):
        del request
        with self._nav_lock:
            png = self._map_png
            version = self._map_version
        if png is None:
            raise web.HTTPServiceUnavailable(text='map has not been received')
        return web.Response(
            body=png,
            content_type='image/png',
            headers={'Cache-Control': 'no-store', 'X-Map-Version': str(version)},
        )

    async def _api_navigation_waypoint(self, request):
        payload = await request.json()
        name = str(payload.get('name', '')).strip()
        with self._nav_lock:
            known = name in self._waypoints
        if not name or not known:
            raise web.HTTPBadRequest(text='请选择有效航点')
        request_message = NavigateToWaypoint.Request()
        request_message.name = name
        success, message = await self._call_service(
            self._navigate_waypoint_client, request_message
        )
        return web.json_response({'success': success, 'message': message}, status=200 if success else 503)

    def _next_vr_waypoint_name(self):
        with self._nav_lock:
            existing = set(self._waypoints)
        for index in range(1, 1000):
            candidate = f'VR航点_{index:02d}'
            if candidate not in existing:
                return candidate
        raise RuntimeError('可用的 VR 航点编号已用完')

    async def _api_navigation_waypoint_save(self, request):
        payload = await request.json()
        name = str(payload.get('name', '')).strip() or self._next_vr_waypoint_name()
        if len(name) > 64 or any(character in name for character in ('/', '\\')):
            raise web.HTTPBadRequest(text='航点名称不合法')
        with self._nav_lock:
            duplicate = name in self._waypoints
        if duplicate:
            return web.json_response(
                {'success': False, 'message': f'航点“{name}”已经存在，请换一个名称'},
                status=409,
            )
        request_message = SaveWaypoint.Request()
        request_message.name = name
        success, message = await self._call_service(
            self._save_waypoint_client, request_message
        )
        if success:
            self._request_waypoint_list(force=True)
        return web.json_response(
            {'success': success, 'message': message, 'name': name},
            status=200 if success else 503,
        )

    async def _api_navigation_waypoint_delete(self, request):
        payload = await request.json()
        name = str(payload.get('name', '')).strip()
        with self._nav_lock:
            known = name in self._waypoints
        if not name or not known:
            raise web.HTTPBadRequest(text='请选择有效航点')
        request_message = DeleteWaypoint.Request()
        request_message.name = name
        success, message = await self._call_service(
            self._delete_waypoint_client, request_message
        )
        if success:
            self._request_waypoint_list(force=True)
        return web.json_response(
            {'success': success, 'message': message, 'name': name},
            status=200 if success else 503,
        )

    async def _api_navigation_stop(self, request):
        del request
        task_state = str(self._task_workflow.snapshot().get('state', 'idle'))
        stop_client = (
            self._cancel_navigation_client
            if self._cancel_navigation_client.service_is_ready()
            else self._pause_navigation_client
        )
        success, message = await self._call_service(
            stop_client, Trigger.Request()
        )
        self._set_drive_sample({'deadman': False})
        if success and task_state in {'navigating', 'navigation_failed'}:
            task = self._task_workflow.operator_stopped_navigation()
            if task is not None:
                message = (
                    f"已中断前往{task['location']}；长按A重试，长按B跳过"
                )
        return web.json_response({
            'success': success,
            'message': message,
            'task_dispatch': self._task_workflow.snapshot(),
        }, status=200 if success else 503)

    async def _api_navigation_relocalize(self, request):
        del request
        success, message = await self._call_service(
            self._relocalize_client, Trigger.Request(), timeout=5.0
        )
        return web.json_response({'success': success, 'message': message}, status=200 if success else 503)

    async def _api_navigation_manual_relocalize(self, request):
        payload = await request.json()
        try:
            x = float(payload.get('x'))
            y = float(payload.get('y'))
            yaw = float(payload.get('yaw'))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text='x, y and yaw must be finite numbers')
        if not all(math.isfinite(value) for value in (x, y, yaw)):
            raise web.HTTPBadRequest(text='x, y and yaw must be finite numbers')

        with self._nav_lock:
            meta = dict(self._map_meta) if self._map_meta else None
        if meta is None:
            raise web.HTTPServiceUnavailable(text='map has not been received')

        resolution = float(meta['resolution'])
        origin_yaw = float(meta.get('origin_yaw', 0.0))
        dx = x - float(meta['origin_x'])
        dy = y - float(meta['origin_y'])
        cosine = math.cos(origin_yaw)
        sine = math.sin(origin_yaw)
        map_x = (cosine * dx + sine * dy) / resolution
        map_y = (-sine * dx + cosine * dy) / resolution
        if not (
            0.0 <= map_x < float(meta['width'])
            and 0.0 <= map_y < float(meta['height'])
        ):
            raise web.HTTPBadRequest(text='manual localization pose is outside the active map')

        self._set_drive_sample({'deadman': False})
        if self._pause_navigation_client.service_is_ready():
            await self._call_service(
                self._pause_navigation_client,
                Trigger.Request(),
                timeout=2.0,
            )

        message = PoseWithCovarianceStamped()
        message.header.frame_id = str(meta.get('frame_id') or 'map')
        message.header.stamp = self.get_clock().now().to_msg()
        message.pose.pose.position.x = x
        message.pose.pose.position.y = y
        yaw = math.atan2(math.sin(yaw), math.cos(yaw))
        message.pose.pose.orientation.z = math.sin(yaw * 0.5)
        message.pose.pose.orientation.w = math.cos(yaw * 0.5)
        message.pose.covariance[0] = 0.25
        message.pose.covariance[7] = 0.25
        message.pose.covariance[35] = 0.20
        for _ in range(3):
            self._initial_pose_publisher.publish(message)
        with self._nav_lock:
            self._relocalization_status = {
                'state': 'succeeded',
                'detail': 'manual localization pose accepted',
            }
        self.get_logger().info(
            f'VR manual localization published: x={x:.3f}, y={y:.3f}, yaw={yaw:.3f}'
        )
        return web.json_response({
            'success': True,
            'message': 'Manual localization pose published; waiting for localization convergence',
            'x': x,
            'y': y,
            'yaw': yaw,
        })

    async def _api_navigation_map_switch(self, request):
        if self._map_switch_in_progress:
            return web.json_response(
                {'success': False, 'message': '另一张地图正在切换，请稍候'}, status=409
            )
        payload = await request.json()
        requested_path = Path(str(payload.get('path', ''))).expanduser().resolve()
        catalog = {Path(entry['path']).resolve(): entry for entry in self._map_catalog()}
        if requested_path not in catalog:
            raise web.HTTPBadRequest(text='所选地图不在导航地图库中')
        entry = catalog[requested_path]
        if entry['active']:
            return web.json_response({
                'success': True,
                'message': f'当前已经是地图：{entry["name"]}',
                'map': entry,
            })
        with self._nav_lock:
            navigation_state = str(self._navigation_status.get('state', '')).lower()
        if navigation_state in {'navigating', 'running', 'active', 'paused', 'executing'}:
            return web.json_response(
                {'success': False, 'message': '导航仍在运行，请先停止导航再切换地图'},
                status=409,
            )
        self._map_switch_in_progress = True
        try:
            load_request = LoadMap.Request()
            load_request.map_url = str(requested_path)
            load_response = await self._await_service_response(
                self._load_map_client, load_request, timeout=7.0
            )
            if int(load_response.result) != 0:
                raise RuntimeError(f'地图加载失败，错误码 {int(load_response.result)}')

            switch_request = SwitchWaypointMap.Request()
            switch_request.map_yaml = str(requested_path)
            switch_response = await self._await_service_response(
                self._switch_waypoint_map_client, switch_request, timeout=5.0
            )
            if not switch_response.success:
                raise RuntimeError(str(switch_response.message))
            self._write_active_map_config(
                requested_path, switch_response.waypoints_file
            )
            with self._nav_lock:
                self._waypoints = list(switch_response.names)
                self._waypoint_poses = {}
            self._request_waypoint_list(force=True)
            # Match the control-center sequence: let the new transient map and
            # waypoint markers settle before relocalization starts.
            await asyncio.sleep(1.2)
            relocalized, relocalize_message = await self._call_service(
                self._relocalize_client, Trigger.Request(), timeout=5.0
            )
            message = f'已切换地图 {entry["name"]}，并加载对应航点库'
            if relocalized:
                message += '；全局重定位已启动'
            else:
                message += f'；请手动启动全局重定位（{relocalize_message}）'
            return web.json_response({
                'success': True,
                'message': message,
                'map': entry,
                'waypoints': list(switch_response.names),
            })
        except Exception as error:
            return web.json_response(
                {'success': False, 'message': str(error)}, status=503
            )
        finally:
            self._map_switch_in_progress = False

    async def _serve(self):
        application = web.Application(client_max_size=64 * 1024)
        application.router.add_get('/api/config', self._api_config)
        application.router.add_get('/api/status', self._api_status)
        application.router.add_get('/api/camera/status', self._api_camera_status)
        application.router.add_post('/api/camera/client-status', self._api_camera_client_status)
        application.router.add_get('/api/camera/stream.mjpg', self._api_camera_stream)
        application.router.add_get('/api/camera/frame.jpg', self._api_camera_frame)
        application.router.add_post('/api/hardware-control', self._api_hardware_control)
        application.router.add_post('/api/waist-follow', self._api_waist_follow)
        application.router.add_post('/api/head-follow', self._api_head_follow)
        application.router.add_post('/api/desktop-mode', self._api_desktop_mode)
        application.router.add_post('/api/realtime/offer', self._api_webrtc_offer)
        application.router.add_post('/api/camera/webrtc/offer', self._api_camera_webrtc_offer)
        application.router.add_get('/ws', self._websocket_handler)
        application.router.add_get('/navigation/ws', self._navigation_websocket_handler)
        application.router.add_get('/navigation/drive', self._drive_websocket_handler)
        application.router.add_get(
            '/navigation/precision-cloud',
            self._precision_cloud_websocket_handler,
        )
        application.router.add_get('/monitor/ws', self._monitor_websocket_handler)
        application.router.add_get('/api/monitor/state', self._api_monitor_state)
        application.router.add_get('/api/navigation/state', self._api_navigation_state)
        application.router.add_get('/api/navigation/map.png', self._api_navigation_map)
        application.router.add_post('/api/gripper/force', self._api_gripper_force)
        application.router.add_post('/api/navigation/waypoint', self._api_navigation_waypoint)
        application.router.add_post(
            '/api/navigation/waypoint/save', self._api_navigation_waypoint_save
        )
        application.router.add_post(
            '/api/navigation/waypoint/delete', self._api_navigation_waypoint_delete
        )
        application.router.add_post('/api/navigation/stop', self._api_navigation_stop)
        application.router.add_post('/api/navigation/relocalize', self._api_navigation_relocalize)
        application.router.add_post(
            '/api/navigation/manual-relocalize', self._api_navigation_manual_relocalize
        )
        application.router.add_post('/api/navigation/map/switch', self._api_navigation_map_switch)
        application.router.add_post('/api/tasks/submit', self._api_tasks_submit)
        application.router.add_post(
            '/api/tasks/template/start', self._api_tasks_template_start
        )
        application.router.add_post(
            '/api/tasks/showroom-waypoint/start', self._api_showroom_waypoint_start
        )
        application.router.add_post(
            '/api/navigation/showroom-mode', self._api_showroom_mode
        )
        application.router.add_post('/api/tasks/confirm', self._api_tasks_confirm)
        application.router.add_post('/api/tasks/complete', self._api_tasks_complete)
        application.router.add_post(
            '/api/tasks/retry-navigation-failure',
            self._api_tasks_retry_navigation_failure,
        )
        application.router.add_post(
            '/api/tasks/skip-navigation-failure',
            self._api_tasks_skip_navigation_failure,
        )
        application.router.add_post('/api/tasks/cancel', self._api_tasks_cancel)
        application.router.add_get('/monitor', self._monitor_index)
        application.router.add_get('/audience', self._audience_index)
        application.router.add_get('/show', self._audience_index)
        application.router.add_get('/', self._index)
        application.router.add_get('/{asset:.*}', self._asset)
        runner = web.AppRunner(application, access_log=None)
        await runner.setup()
        site = web.TCPSite(
            runner,
            str(self.get_parameter('host').value),
            int(self.get_parameter('https_port').value),
            ssl_context=self._ssl_context,
        )
        await site.start()
        try:
            while not self._stop_requested.is_set():
                await asyncio.sleep(0.10)
        finally:
            self._set_drive_sample({'deadman': False})
            if rclpy.ok():
                self._publish_stop()
            await self._close_camera_webrtc_peer()
            await self._close_webrtc_peer()
            await runner.cleanup()

    def destroy_node(self):
        self._set_drive_sample({'deadman': False})
        if rclpy.ok():
            self._publish_stop()
        if self._precision_cloud_stream is not None:
            self._precision_cloud_stream.close()
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = NavigationVrWebBridge()
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
