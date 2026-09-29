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
from rclpy.parameter import Parameter
from rcl_interfaces.srv import SetParametersAtomically
from rclpy.executors import ExternalShutdownException
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger, SetBool
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
from openarmx_teleop_vr_306_v4.gripper_aperture import checked_width, load_width
from .delivery_handoff import DeliveryHandoffMonitor
from .simple_task_catalog import load_catalog, lowering_cm, head_pitch_deg
from .session_controls import task_menu_catalog, compose_menu_tasks, pose_values
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
            return f'出发喽，前往{location}。'
        return f'出发喽，去{location}，任务是{action}。'
    if event == 'ready':
        if showroom_single:
            return '准备就绪，开工啦。'
        return f'准备就绪，开始{action}。'
    if event == 'arrived':
        if showroom_single:
            return f'{location}到了，准备开工。'
        return f'{location}到了，准备{action}。'
    if event == 'completed':
        if showroom_single:
            return f'{location}任务完成啦。'
        return f'{action}完成啦。'
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
            '/home/ubuntu/ros2_ws/src/openarmx_teleop_vr_navigation_306/config/task_actions.txt'
        )
        self.declare_parameter(
            'task_catalog_path', str(
                source_task_catalog
                if source_task_catalog.is_file()
                else package_share / 'config' / 'task_actions.txt'
            )
        )
        self.declare_parameter(
            'task_tts_topic', '/openarmx_teleop_vr_navigation_306/task_speech'
        )
        self.declare_parameter(
            'showroom_mode_topic',
            '/openarmx_teleop_vr_navigation_306/showroom_mode',
        )
        source_delivery_config = Path(
            '/home/ubuntu/ros2_ws/src/openarmx_teleop_vr_navigation_306'
            '/config/delivery_handoff.json'
        )
        self.declare_parameter(
            'delivery_handoff_config_path', str(
                source_delivery_config
                if source_delivery_config.is_file()
                else package_share / 'config' / 'delivery_handoff.json'
            )
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
        self._exit_in_progress = False
        self._drive_session_generation = 0
        self._drive_sample = None
        self._drive_time = 0.0
        self._drive_was_active = False
        self._drive_current = [0.0, 0.0, 0.0]
        self._last_drive_tick = time.monotonic()
        self._drive_settle_until = 0.0
        self._pause_requested_for_drive = False
        self._drive_pause_retry_at = 0.0
        self._navigation_paused_for_drive = False
        self._resume_requested_for_drive = False
        self._task_intervention = False
        self._navigation_websocket = None
        self._drive_websocket = None
        self._gripper_force_ratio = 0.35
        self._gripper_width_client = self.create_client(
            SetParametersAtomically,
            '/independent_vr_mapper_306_v4/set_parameters_atomically',
        )
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
        self._task_catalog_error = ''
        self._task_catalog_revision = None
        try:
            task_catalog = load_catalog(self._task_catalog_path)
            catalog_stat = self._task_catalog_path.stat()
            self._task_catalog_revision = (catalog_stat.st_mtime_ns, catalog_stat.st_size)
        except (OSError, TypeError, ValueError) as error:
            task_catalog = {}
            self._task_catalog_error = str(error)
            self.get_logger().error(f'任务库未加载：{error}；VR遥操仍可使用')
        self._task_workflow = TaskWorkflow(task_catalog)
        self._task_templates = task_chain_templates(task_catalog)
        showroom = task_catalog.get('showroom_mode', {})
        self._showroom_mode_enabled = False
        self._voice_enabled = False
        self._speech_health = {'state': 'disabled'}
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
        self._task_height_target_m = None
        self._task_transit_pose_error = ''
        self._task_height_deadline = 0.0
        self._task_height_stable_since = 0.0
        self._task_head_pitch_deg = None
        self._task_head_request_id = ''
        self._manual_pose_status = {'state': 'idle', 'message': ''}
        self._pose_is_manual = False
        # Delivery-robot handoff polling runs on its own serial thread; the
        # config file is hot-reloaded so the site can point 283 at the hotel
        # ops server without a rebuild or restart.
        self._delivery_handoff = DeliveryHandoffMonitor(
            Path(
                str(self.get_parameter('delivery_handoff_config_path').value)
            ).expanduser().resolve(),
            self.get_logger(),
        )
        self._delivery_handoff.start()
        # Keep the concurrent delivery-order task-dispatch integration.
        self._delivery_seen_orders = set()
        self._delivery_offer_lock = threading.Lock()
        self._delivery_chain_note = None
        # Outbound orders surface a task offer card (A accept / X reject);
        # the operator decides, nothing auto-submits.
        self._delivery_offer = None

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
        self._task_head_publisher = self.create_publisher(
            String, '/openarmx_teleop_vr_306_v4/task_head_target', 1
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
        self._voice_state_publisher = self.create_publisher(
            String, '/openarmx_teleop_vr_navigation_306/voice_state', transient_qos)
        self.create_subscription(String, '/openarmx_teleop_vr_navigation_306/speech_health',
            self._on_speech_health, transient_qos)
        self.create_timer(1.0, self._publish_voice_state)
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
        self._stop_ready_client = self.create_client(Trigger, '/navigation_stop_ready')
        self._operator_arrival_client = self.create_client(NavigateToWaypoint, '/operator_confirm_arrival')
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
        self.create_timer(1.0, self._delivery_chain_tick)

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
            'ui_version': 'yundie-306-navigation-283-delivery-menu-v2026092303',
            'panel_toggle_button': 'Y',
            'confirm_button': 'A',
            'relocalize_button': 'X',
            'drive_control': 'left_thumbstick_toggle',
            'state_url': '/api/navigation/state',
            'map_url': '/api/navigation/map.png',
            'websocket_path': '/navigation/ws',
            'drive_websocket_path': '/navigation/drive',
            'gripper_force_ratio': self._gripper_force_ratio,
            'gripper_closed_width_cm': load_width(),
            'gripper_force_minimum_ratio': float(
                self.get_parameter('gripper_force_minimum_ratio').value
            ),
            'gripper_force_maximum_current': float(
                self.get_parameter('gripper_force_maximum_current').value
            ),
            'gripper_force_step': 0.1,
            'showroom_mode_enabled': self._showroom_mode_enabled,
            'voice_enabled': self._voice_enabled,
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
            task = self._task_workflow.navigation_arrived(waypoint, operator=state == 'operator_arrived')
            if task is not None:
                # One concise arrival cue replaces the former two-message
                # sequence and its artificial silence.  Body-height adjustment
                # continues quietly in parallel.
                self._speak_task(task_voice_text(
                    'arrived', task, self._showroom_single_task_active
                ))
                transit_error = getattr(self, '_task_transit_pose_error', '')
                if transit_error:
                    self._task_workflow.height_failed(transit_error)
                elif self._task_height_target_m is None:
                    # Recheck feedback at arrival, without restarting adjustment in flight.
                    self._set_task_height(task['body_lowering_cm'], task.get('head_pitch_deg', 30),
                                          reverse_squat=task.get('reverse_squat', False))
        elif outcome == 'failed':
            current = self._task_workflow.current()
            self._task_workflow.navigation_failed(
                f"前往{waypoint or (current or {}).get('location', '目标地点')}失败"
            )

    def _speak_task(self, text):
        NavigationVrWebBridge._publish_voice_state(self)
        if not getattr(self, '_voice_enabled', False) or not text or not rclpy.ok():
            return
        self._task_tts_publisher.publish(String(data=json.dumps({
            'status': 'play',
            'text': str(text),
        }, ensure_ascii=False, separators=(',', ':'))))

    def _publish_showroom_mode(self):
        self._showroom_mode_publisher.publish(
            Bool(data=bool(self._showroom_mode_enabled))
        )
        self._publish_voice_state()

    def _on_speech_health(self, message):
        self._speech_health = self._status_payload(message)

    def _publish_voice_state(self):
        if not hasattr(self, '_voice_state_publisher'):
            return
        state = self._task_workflow.snapshot()['state']
        self._voice_state_publisher.publish(String(data=json.dumps({
            'enabled': self._voice_enabled, 'showroom': self._showroom_mode_enabled,
            'task_active': state not in {'idle', 'completed', 'cancelled'},
        })))

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
        catalog = load_catalog(self._task_catalog_path)
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

    def _refresh_idle_task_catalog(self):
        if self._task_workflow.snapshot()['state'] not in {'idle', 'completed', 'cancelled', 'failed'}:
            return
        try:
            self._reload_task_catalog_if_changed()
            self._task_catalog_error = ''
        except (OSError, TypeError, ValueError) as error:
            self._task_catalog_error = str(error)

    @staticmethod
    def _task_measured_lowering(status, fresh, signed=False):
        """Mapper status wraps controller feedback under backend, not at root."""
        if not fresh or not isinstance(status, dict):
            return None
        backend = status.get('backend', status)
        if not isinstance(backend, dict):
            return None
        height = backend.get('body_height', {})
        if not isinstance(height, dict) or height.get('feedback_fresh') is not True:
            return None
        try:
            measured = float(height.get('measured_signed_lowering_m', height['measured_lowering_m'])
                             if signed else height['measured_lowering_m'])
            feedback_age = time.monotonic() - float(height['feedback_time_monotonic'])
        except (KeyError, TypeError, ValueError):
            return None
        # Both nodes run on the robot; a fresh mapper packet must not make a
        # cached, stopped controller appear to have fresh encoder feedback.
        return measured if (math.isfinite(measured) and (-0.53 if signed else 0) <= measured <= 0.56
                            and 0 <= feedback_age <= 0.5) else None

    def _publish_task_head(self, enabled):
        if getattr(self, '_task_head_pitch_deg', None) is None:
            return
        self._task_head_publisher.publish(String(data=json.dumps({
            'enabled': bool(enabled), 'request_id': self._task_head_request_id,
            'head_pitch_deg': self._task_head_pitch_deg,
        })))
        if not enabled:
            self._task_head_pitch_deg = None

    @staticmethod
    def _task_head_ready(status, fresh, angle, request_id=None):
        if angle is None:
            return True
        # Use the same encoder timestamp validation as body height, never a
        # command/reference value or merely a fresh websocket packet.
        if NavigationVrWebBridge._task_measured_lowering(status, fresh) is None:
            return False
        backend = status.get('backend', status)
        try:
            if request_id is not None:
                goal = backend['task_head_goal']
                if goal['request_id'] != request_id:
                    return False
                requested = float(goal['requested_deg'])
                effective = float(goal['effective_deg'])
                maximum = float(goal['safe_max_deg'])
                if (not all(math.isfinite(v) for v in (requested, effective, maximum))
                        or abs(requested-angle) > 1e-6
                        or not 0 <= effective <= min(angle, maximum) + 1e-6):
                    return False
                angle = effective
            pitch = float(backend['measured_neck_deg'][1])
            return math.isfinite(pitch) and abs(pitch + angle) <= 1.0
        except (KeyError, IndexError, TypeError, ValueError):
            return False

    def _set_task_height(self, height_cm, head_angle=None, manual=False, reverse_squat=False):
        target = lowering_cm(height_cm) / 100.0
        if not isinstance(reverse_squat, bool) or (reverse_squat and target > 0.53):
            raise ValueError('反方向下蹲最多53厘米')
        if reverse_squat:
            target = -target
        angle = None if head_angle is None else head_pitch_deg(head_angle)
        with self._task_height_lock:
            self._pose_is_manual = manual
            if manual:
                self._manual_pose_status = {'state': 'adjusting', 'message': '正在调整高度和头部'}
            self._task_height_target_m = target
            self._task_head_pitch_deg = angle
            self._task_head_request_id = str(time.monotonic_ns())
            status, _age, fresh = self._teleop_status_snapshot()
            current = NavigationVrWebBridge._task_measured_lowering(status, fresh, signed=True)
            travel = abs(current - target) if current is not None else 1.09
            # 283 encoder traces show ~0.025 m/s of physical lowering,
            # despite the faster reference rate. Budget at 0.020 m/s plus
            # settling time: 20 cm gets 13 s, not the old premature 7 s.
            # This remains bounded (31 s for the full 56 cm), and actual
            # feedback still decides completion; no timeout means success.
            self._task_height_deadline = (
                time.monotonic() + max(self._task_height_timeout_seconds, travel / 0.02 + 3.0,
                                       15.0 if angle is not None else 0.0)
            )
            self._task_height_stable_since = 0.0
        # Send the first target in the arrival callback, not after speech.
        self._task_height_tick()

    def _release_task_height_for_manual_control(self):
        """Hand task-height ownership to X/Y without stranding the mission."""
        with self._task_height_lock:
            NavigationVrWebBridge._publish_task_head(self, False)
            self._task_height_target_m = None
            self._task_height_deadline = 0.0
            self._task_height_stable_since = 0.0
            if getattr(self, '_pose_is_manual', False):
                self._pose_is_manual = False
                self._manual_pose_status = {'state': 'cancelled', 'message': '已停止位置调整'}
                return False
        task = self._task_workflow.height_adjusted()
        if task is not None:
            self.get_logger().info(
                'manual body-height control superseded the task preset; '
                'operator action stage released'
            )
        return task is not None

    def _task_height_tick(self):
        if self._exit_in_progress:
            return
        with self._task_height_lock:
            if (self._task_height_target_m is not None and not getattr(self, '_pose_is_manual', False)
                    and self._task_workflow.snapshot()['state'] not in {'navigating', 'adjusting_height'}):
                NavigationVrWebBridge._publish_task_head(self, False)
                self._task_height_target_m = None
                self._body_height_publisher.publish(String(data='{"enabled":false,"direction":0.0}'))
                return
            level = self._task_height_target_m
            now = time.monotonic()
            active = level is not None and now < self._task_height_deadline
            reached = False
            head_cancelled = False
            head_cancel_reason = ''
            if active:
                status, _age, fresh = self._teleop_status_snapshot()
                status = status if isinstance(status, dict) else {}
                angle = getattr(self, '_task_head_pitch_deg', None)
                backend = status.get('backend', status)
                if not isinstance(backend, dict):
                    backend = {}
                head_cancelled = angle is not None and fresh and (
                    backend.get('head_follow_enabled', False)
                    or backend.get('task_head_cancelled_id') == self._task_head_request_id)
                if head_cancelled:
                    head_cancel_reason = ('head_tracking' if backend.get('head_follow_enabled', False)
                                          else backend.get('task_head_cancel_reason', ''))
                if getattr(self, '_pose_is_manual', False) and (
                        status.get('active_arms') or self._navigation_is_active()):
                    head_cancelled = True
                    head_cancel_reason = 'other_motion'
                lowering = NavigationVrWebBridge._task_measured_lowering(status, fresh, signed=True)
                try:
                    target = level
                    close = fresh and abs(float(lowering) - target) <= 0.008
                    close = close and NavigationVrWebBridge._task_head_ready(
                        status, fresh, angle, getattr(self, '_task_head_request_id', None))
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
                if head_cancelled:
                    active = False
            if level is not None and (not active or reached):
                self._task_height_target_m = None
                active = False
            timed_out = level is not None and not active and not reached
        if not rclpy.ok() or level is None:
            return
        payload = (
            {'enabled': True, 'direction': 0.0, 'target_lowering_m': abs(level),
             **({'reverse_squat': True} if level < 0 else {})}
            if active else {'enabled': False, 'direction': 0.0}
        )
        self._body_height_publisher.publish(String(data=json.dumps(
            payload, separators=(',', ':')
        )))
        head_angle = getattr(self, '_task_head_pitch_deg', None)
        NavigationVrWebBridge._publish_task_head(self, active)
        if timed_out:
            # Never claim the requested height was reached on a timeout.
            status, _age, fresh = self._teleop_status_snapshot()
            measured = NavigationVrWebBridge._task_measured_lowering(status, fresh, signed=True)
            actual = '反馈过期/不可用' if measured is None else f"实测{'反向' if measured < 0 else ''}下降{abs(measured) * 100:.1f}cm"
            if head_cancelled:
                details = {
                    'head_tracking': '头部跟随已开启，请锁定头部后重试',
                    'head_mode_changed': '已手动切换头部控制，请锁定头部后重试',
                    'reset': '正在执行复位，请复位完成后重试',
                    'enabling': '使能尚未完成，请完成使能后重试',
                    'estop': '急停已触发，请检查现场和机器人状态',
                    'hardware_disabled': '机器人未使能，请检查使能状态',
                    'invalid_neck_limits': '颈部限位配置无效，请检查模型配置',
                    'stopped': '当前调头请求已被停止，请检查后重试',
                    'other_motion': '已开始手臂跟随或导航，手动位置调整已停止',
                }
                message = '任务姿态调整已停止：' + details.get(head_cancel_reason, '头部控制状态发生变化，请检查后重试')
            elif head_angle is not None and not NavigationVrWebBridge._task_head_ready(
                    status, fresh, head_angle, self._task_head_request_id):
                message = f'头部未到位：目标俯视{head_angle:g}°；请检查使能、头部锁定及关节反馈后重试'
            else:
                message = f"高度未到位：目标{'反向' if level < 0 else ''}下降{abs(level) * 100:g}cm，{actual}；请检查后重试"
            if getattr(self, '_pose_is_manual', False):
                self._manual_pose_status = {'state': 'failed', 'message': message}
                self._pose_is_manual = False
            else:
                if self._task_workflow.snapshot()['state'] == 'navigating':
                    self._task_transit_pose_error = message
                else:
                    self._task_workflow.height_failed(message)
        elif not active:
            if getattr(self, '_pose_is_manual', False):
                self._manual_pose_status = {'state': 'ready', 'message': '高度和头部已到位'}
                self._pose_is_manual = False
            else:
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
        if self._exit_in_progress:
            return
        deadman = bool(payload.get('deadman', False))
        if getattr(self, '_task_intervention', False):
            deadman = False
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
                self._drive_session_generation = getattr(self, '_drive_session_generation', 0) + 1
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
            # Only deliberate X/Y input may supersede the automatic height
            # target. Idle chassis packets used to cancel it every VR frame.
            with self._task_height_lock:
                if self._task_height_target_m is not None:
                    return
            self._body_height_publisher.publish(String(data=json.dumps({
                'enabled': height_enabled,
                'direction': height_direction,
            }, separators=(',', ':'))))

    @staticmethod
    def _gripper_vendor_force(ratio):
        # The vendor interface normally accepts the full 0..10 A range
        # (the published normalized value is multiplied by 10 A).  Hardware
        # testing showed little useful feel difference once the cap reaches
        # roughly 0.4 A for the current scenarios, so expose only four fixed
        # levels: off, low, standard, and the original 10 A high ceiling.
        return {
            0.0: 0.0,
            0.25: 0.025,
            0.35: 0.035,
            1.0: 1.0,
        }[ratio]

    def _set_gripper_force_ratio(self, payload):
        ratio = float(payload.get('ratio'))
        minimum = float(self.get_parameter('gripper_force_minimum_ratio').value)
        if not math.isfinite(ratio) or ratio < minimum or ratio > 1.0:
            raise ValueError(f'gripper force ratio must be within [{minimum}, 1.0]')
        ratio = round(ratio, 2)
        if ratio not in (0.0, 0.25, 0.35, 1.0):
            raise ValueError('gripper force must be one of: 0%, low, standard, high')
        if (
            self._left_gripper_force_publisher.get_subscription_count() < 1
            or self._right_gripper_force_publisher.get_subscription_count() < 1
        ):
            raise RuntimeError('厂商夹爪力度接口未连接，设置未发送')
        # The vendor callback expects the key ``force``.  It multiplies this
        # normalized value by the configured 10 A gripper-current ceiling.
        # The old ``ratio`` key was silently replaced by the vendor default
        # 0.5, which made every menu setting apply the same 5 A limit.
        vendor_force = self._gripper_vendor_force(ratio)
        message = String(data=json.dumps({'force': vendor_force}, separators=(',', ':')))
        self._left_gripper_force_publisher.publish(message)
        self._right_gripper_force_publisher.publish(message)
        self._gripper_force_ratio = ratio
        return ratio

    async def _api_gripper_width(self, request):
        try:
            width = checked_width((await request.json()).get('width_cm'))
            if not self._gripper_width_client.service_is_ready():
                raise RuntimeError('夹爪映射服务未就绪，宽度未更改')
            command = SetParametersAtomically.Request(parameters=[
                Parameter('gripper_closed_width_cm', value=width).to_parameter_msg(),
            ])
            response = await self._await_service_response(self._gripper_width_client, command, 3.0)
            if not response.result.successful:
                raise RuntimeError(response.result.reason)
            return web.json_response({'success': True, 'width_cm': width})
        except (ValueError, TypeError, RuntimeError, TimeoutError, OSError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=400)

    async def _api_gripper_force(self, request):
        # Old cached pages must not silently keep changing motor current.
        return web.json_response({'success': False, 'message': '菜单已改为闭合宽度，请刷新VR页面'}, status=410)

    def _publish_stop(self):
        self._drive_current = [0.0, 0.0, 0.0]
        self._drive_publisher.publish(Twist())

    def _publish_manual_override(self, active):
        self._manual_override_publisher.publish(Bool(data=bool(active)))

    @staticmethod
    def _approach(current, target, maximum_step):
        return current + max(-maximum_step, min(maximum_step, target - current))

    def _pause_for_drive(self):
        if self._exit_in_progress or self._pause_requested_for_drive:
            return
        if time.monotonic() < getattr(self, '_drive_pause_retry_at', 0.0):
            return
        if not self._pause_navigation_client.service_is_ready():
            return
        self._pause_requested_for_drive = True
        generation = self._drive_session_generation
        future = self._pause_navigation_client.call_async(Trigger.Request())

        def complete(done):
            success = False
            try:
                result = done.result()
                success = bool(result and result.success)
            except Exception as error:
                self.get_logger().warning(f'Navigation pause for manual drive failed: {error}')
            with self._drive_lock:
                if generation != self._drive_session_generation:
                    return
                self._navigation_paused_for_drive = success
                if not success and self._navigation_is_active():
                    self._pause_requested_for_drive = False
                    self._drive_pause_retry_at = time.monotonic() + .5
            if success:
                self.get_logger().info('Navigation paused for manual base takeover')

        future.add_done_callback(complete)

    async def _operator_payload(self, request):
        payload = await request.json()
        with self._client_lock:
            if (self._exit_in_progress or self._control_peer is None
                    or request.remote != self._control_peer
                    or payload.get('client_id') != self._control_client_id):
                raise RuntimeError('请由当前接管的头显操作')
        return payload

    def _drive_link_alive(self):
        return any(sock is not None and not sock.closed
                   for sock in (self._drive_websocket, self._navigation_websocket))

    async def _wait_navigation_stop(self, generation):
        deadline = time.monotonic() + 5.0
        message = '等待导航停止'
        while time.monotonic() < deadline:
            if (self._exit_in_progress or generation != self._drive_session_generation
                    or not self._drive_link_alive()):
                raise RuntimeError('接管状态或连接已改变，已取消操作')
            ready, message = await self._call_service(self._stop_ready_client, Trigger.Request(), timeout=1.0)
            if generation != self._drive_session_generation or not self._drive_link_alive():
                raise RuntimeError('连接或接管状态已改变，已取消操作')
            if ready:
                return
            await asyncio.sleep(.1)
        raise RuntimeError(message)

    async def _api_release_drive(self, request):
        try:
            await self._operator_payload(request)
            if self._task_intervention or self._resume_requested_for_drive:
                raise RuntimeError('任务操作正在处理中')
            self._resume_requested_for_drive = True
            owns_resume = True
            generation = self._drive_session_generation
            self._set_drive_sample({'deadman': False})
            self._publish_stop()
            self._publish_manual_override(False)
            await self._wait_navigation_stop(generation)
            if not self._navigation_paused_for_drive:
                return web.json_response({'success': True, 'message': '底盘接管已退出，无待恢复导航'})
            with self._nav_lock:
                destination = str(self._navigation_status.get('active_waypoint', ''))
            success, message = await self._call_service(self._resume_navigation_client, Trigger.Request())
            if not success:
                raise RuntimeError(message)
            self._navigation_paused_for_drive = False
            self._pause_requested_for_drive = False
            self._task_workflow.navigation_resumed(destination)
            return web.json_response({'success': True, 'message': '已退出底盘接管，继续前往原航点'})
        except (RuntimeError, ValueError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=409)
        finally:
            if 'owns_resume' in locals():
                self._resume_requested_for_drive = False

    def _drive_tick(self):
        if self._exit_in_progress or self._control_peer is None:
            if self._drive_was_active or any(abs(value) > 1e-6 for value in self._drive_current):
                self._publish_stop()
            self._drive_was_active = False
            return
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
            if self._drive_was_active and self._navigation_is_active() and not self._navigation_paused_for_drive:
                # Lost input cannot give an unpaused old action control back.
                self._publish_manual_override(True)
                self._publish_stop()
                self._pause_for_drive()
                return
            if self._drive_was_active or any(abs(value) > 1e-6 for value in self._drive_current):
                self._publish_stop()
            self._publish_manual_override(False)
            self._drive_was_active = False
            return

        # Claim arbitration before publishing the first non-zero velocity.
        self._publish_manual_override(True)
        self._pause_for_drive()
        if self._navigation_is_active() and not self._navigation_paused_for_drive:
            self._publish_stop()
            self._drive_was_active = True
            return  # Never leave an unpaused autonomous action behind a takeover.

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

    async def _websocket_handler(self, request):
        if self._exit_in_progress:
            raise web.HTTPConflict(text='VR session is stopping safely; retry shortly')
        return await super()._websocket_handler(request)

    async def _api_exit_vr(self, request):
        payload = await request.json()
        with self._client_lock:
            owns_session = (self._control_peer == request.remote
                            and self._control_client_id == payload.get('client_id'))
        if not owns_session:
            raise web.HTTPConflict(text='Only the active headset can exit this session')
        if self._exit_in_progress:
            raise web.HTTPConflict(text='VR exit is already in progress')
        self._exit_in_progress = True
        self._voice_enabled = False
        self._publish_voice_state()
        try:
            self._task_workflow.cancel('VR会话已退出')
            self._release_task_height_for_manual_control()
            self._body_height_publisher.publish(String(data='{"enabled":false,"direction":0.0}'))
            self._showroom_single_task_active = False
            with self._drive_lock:
                self._drive_session_generation += 1
                self._drive_sample = None
                self._navigation_paused_for_drive = False
                self._pause_requested_for_drive = False
                self._resume_requested_for_drive = False
            self._publish_stop()
            navigation_error = None
            if self._navigation_is_active() or self._cancel_navigation_client.service_is_ready():
                success, message = await self._call_service(self._cancel_navigation_client, Trigger.Request())
                if not success and self._navigation_is_active():
                    navigation_error = message
            success, message = await self._call_service(self._hardware_client, SetBool.Request(data=False), timeout=6.0)
            if not success or navigation_error:
                return web.json_response({'success': False, 'message': navigation_error or message}, status=409)
            # Keep ownership reserved while every old data transport closes.
            for socket in (self._navigation_websocket, self._drive_websocket,
                           self._precision_cloud_websocket):
                if socket is not None and not socket.closed:
                    await socket.close(code=1000, message=b'explicit VR exit')
            await self._close_camera_webrtc_peer()
            await self._close_webrtc_peer()
            socket = self._control_websocket
            if socket is not None and not socket.closed:
                await socket.close(code=1000, message=b'explicit VR exit')
            with self._client_lock:
                self._connected_clients = 0
                self._control_peer = None
                self._control_client_id = None
                self._control_websocket = None
                self._control_session = 0
            self._clear_packet_buffer()
            self._publish_stop()
            self._publish_manual_override(False)
            return web.json_response({'success': True, 'message': '已停止任务并释放VR接管'})
        finally:
            self._exit_in_progress = False

    async def _api_hardware_control(self, request):
        payload = await request.json()
        if self._exit_in_progress:
            raise web.HTTPConflict(text='VR session is stopping safely')
        if isinstance(payload, dict) and payload.get('enabled') is True:
            with self._client_lock:
                owns_session = (self._control_peer == request.remote
                                and self._control_client_id == payload.get('client_id'))
            if not owns_session:
                return web.json_response({'success': False, 'message': '当前网页尚未取得VR数据连接，请等待连接后再使能'}, status=409)
        return await super()._api_hardware_control(request)

    async def _navigation_websocket_handler(self, request):
        if self._exit_in_progress:
            raise web.HTTPConflict(text='VR session is stopping')
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
                        # Deprecated cached client: never change force here.
                        continue
                except (TypeError, ValueError):
                    continue
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            if self._navigation_websocket is websocket:
                self._navigation_websocket = None
                if self._drive_websocket is None or self._drive_websocket.closed:
                    self._drive_session_generation += 1
                    self._set_drive_sample({'deadman': False})
        return websocket

    async def _drive_websocket_handler(self, request):
        if self._exit_in_progress:
            raise web.HTTPConflict(text='VR session is stopping')
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
                self._drive_session_generation += 1
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
        self._refresh_idle_task_catalog()
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
        payload['task_catalog_error'] = self._task_catalog_error
        payload['showroom_mode_enabled'] = self._showroom_mode_enabled
        payload['voice_enabled'] = self._voice_enabled
        payload['speech_health'] = self._speech_health
        status, _age, fresh = self._teleop_status_snapshot()
        status = status if isinstance(status, dict) else {}
        measured = self._task_measured_lowering(status, fresh)
        backend = status.get('backend') or {}
        try:
            pitch = -float(backend['measured_neck_deg'][1])
            pitch = round(pitch, 1) if measured is not None and math.isfinite(pitch) else None
        except (KeyError, IndexError, TypeError, ValueError):
            pitch = None
        payload['body_pose'] = {
            'lowering_cm': None if measured is None else round(measured * 100, 1),
            'head_pitch_deg': pitch,
            'preset': dict(self._manual_pose_status),
        }
        payload['delivery_handoff'] = self._delivery_state_payload()
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
        self._task_transit_pose_error = ''
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
        if self._task_workflow.snapshot()['state'] == 'navigating':
            self._set_task_height(task['body_lowering_cm'], task.get('head_pitch_deg', 30),
                                  reverse_squat=task.get('reverse_squat', False))
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

    async def _api_task_menu_catalog(self, request):
        try:
            return web.json_response({'success': True, **task_menu_catalog(self._task_catalog_path)})
        except (OSError, ValueError, TypeError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=422)

    async def _api_custom_task_start(self, request):
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError('任务请求格式错误')
            with self._client_lock:
                if (self._control_peer is None or self._control_peer != request.remote
                        or self._control_client_id != payload.get('client_id')):
                    raise RuntimeError('请由当前已使能的头显确认任务')
            status, _age, fresh = self._teleop_status_snapshot()
            status = status if isinstance(status, dict) else {}
            if self._exit_in_progress or not fresh or not (status.get('backend') or {}).get('hardware_ready'):
                raise RuntimeError('请先完成VR使能')
            if self._task_workflow.snapshot()['state'] not in {'idle','completed','cancelled','failed'}:
                raise RuntimeError('请先完成或退出当前任务串')
            if self._task_height_target_m is not None:
                raise RuntimeError('请等待姿态调整完成')
            self._refresh_idle_task_catalog()
            if self._task_catalog_error: raise ValueError(self._task_catalog_error)
            tasks = compose_menu_tasks(task_menu_catalog(self._task_catalog_path), payload)
            with self._nav_lock:
                missing = sorted({t['location'] for t in tasks} - set(self._waypoints))
            if missing: raise ValueError('地图缺少航点：'+'、'.join(missing))
            self._task_workflow.submit('VR自定义任务流程', tasks)
            self._showroom_single_task_active = False
            return await self._api_tasks_confirm(None)
        except (OSError, ValueError, TypeError) as error:
            return web.json_response({'success':False,'message':str(error)},status=422)
        except RuntimeError as error:
            return web.json_response({'success':False,'message':str(error)},status=409)

    async def _api_pose_preset(self, request):
        try:
            payload = await request.json()
            height, head = pose_values(payload)
            with self._client_lock:
                if (self._control_peer is None or self._control_peer != request.remote
                        or self._control_client_id != payload.get('client_id')):
                    raise RuntimeError('只能由当前已使能的头显设置位置')
            status, _age, fresh = self._teleop_status_snapshot()
            status = status if isinstance(status, dict) else {}
            backend = status.get('backend') or {}
            if self._exit_in_progress or not fresh or not backend.get('hardware_ready'):
                raise RuntimeError('请先完成VR使能，并确认关节反馈正常')
            if backend.get('head_follow_enabled'):
                raise RuntimeError('请先按B锁定头部，再应用位置')
            if status.get('active_arms'):
                raise RuntimeError('请先松开腰键，停止手臂跟随')
            if self._navigation_is_active():
                raise RuntimeError('请先停止导航，再调整位置')
            with self._drive_lock:
                if self._drive_sample and self._drive_sample.get('deadman'):
                    raise RuntimeError('请先取消底盘接管，再应用位置；X/Y手动升降仍可使用')
            if self._task_height_target_m is not None:
                raise RuntimeError('正在调整位置，请等待完成或停止后再设置')
            self._set_task_height(height, head, manual=True)
            return web.json_response({'success':True,'message':'开始调整；可在菜单中停止'})
        except (KeyError, ValueError, TypeError) as error:
            return web.json_response({'success':False,'message':str(error)},status=422)
        except RuntimeError as error:
            return web.json_response({'success':False,'message':str(error)},status=409)

    async def _api_pose_stop(self, request):
        if self._pose_is_manual:
            self._release_task_height_for_manual_control()
            self._body_height_publisher.publish(String(data='{"enabled":false,"direction":0}'))
        return web.json_response({'success':True,'message':'位置调整已停止'})

    async def _api_voice_enabled(self, request):
        payload = await request.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('enabled'), bool):
            return web.json_response({'success':False,'message':'语音开关格式错误'},status=422)
        self._voice_enabled = payload['enabled']
        self._publish_voice_state()
        return web.json_response({'success':True,'enabled':self._voice_enabled,
            'message':'语音播报已开启，正在准备引擎' if self._voice_enabled else '语音播报已关闭'})

    async def _api_tasks_template_start(self, request):
        try:
            self._refresh_idle_task_catalog()
            if self._task_catalog_error: raise ValueError(self._task_catalog_error)
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
            self._speak_task(f"收到，{template['name']}，这就出发。")
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
                    'body_lowering_cm': 0.0, 'automatic': False,
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
                    self._speak_task('本轮任务完成，收工啦。')
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
        # Old cached pages mapped long B directly to skip. Never retain that trap.
        del request
        return web.json_response({'success': False, 'message': '跳过任务已移至菜单，请刷新VR网页'}, status=409)

    async def _api_task_intervention(self, request, skip=False):
        try:
            payload = await self._operator_payload(request)
            snapshot = self._task_workflow.snapshot()
            allowed = {'navigating','navigation_failed'}
            if skip:
                allowed.update({'adjusting_height','awaiting_action','failed'})
            if (snapshot['state'] not in allowed or not snapshot['current']
                    or payload.get('task_token') != snapshot['task_token']):
                raise RuntimeError('当前任务已改变，请重新确认')
            if skip and payload.get('confirmed') is not True:
                raise RuntimeError('跳过任务需要二次确认')
            if self._task_intervention or self._resume_requested_for_drive:
                raise RuntimeError('上一项任务操作仍在处理')
            self._task_intervention = True
            self._drive_session_generation += 1
            generation = self._drive_session_generation
            self._navigation_paused_for_drive = False
            self._pause_requested_for_drive = False
            self._set_drive_sample({'deadman': False})
            self._publish_stop()
            self._publish_manual_override(False)
            if snapshot['state'] in {'navigating','navigation_failed'} or self._navigation_is_active():
                ok, message = await self._call_service(self._pause_navigation_client, Trigger.Request())
                if not ok:
                    raise RuntimeError(message)
            await self._wait_navigation_stop(generation)
            latest = self._task_workflow.snapshot()
            if latest['task_token'] != snapshot['task_token'] or latest['state'] not in allowed:
                raise RuntimeError('任务状态已改变，本次确认已取消')
            if skip:
                # The old target must not survive as a resumable paused goal.
                if snapshot['state'] in {'navigating','navigation_failed'}:
                    ok, message = await self._call_service(self._cancel_navigation_client, Trigger.Request())
                    if not ok:
                        raise RuntimeError(message)
                self._release_task_height_for_manual_control()
                result, skipped, upcoming = self._task_workflow.skip_current()
                if result == 'next':
                    await self._start_current_task_navigation()
                else:
                    self._showroom_single_task_active = False
                message = f"已跳过{skipped['location']}" + ('，前往下一任务' if result == 'next' else '，本轮结束')
            else:
                goal = NavigateToWaypoint.Request()
                goal.name = snapshot['current']['location']
                ok, message = await self._call_service(self._operator_arrival_client, goal)
                if not ok:
                    raise RuntimeError(message)
            return web.json_response({'success': True, 'message': message,
                                      'task_dispatch': self._task_workflow.snapshot()})
        except (RuntimeError, ValueError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=409)
        finally:
            # Do not release another request's lock when this one was rejected.
            if 'generation' in locals():
                self._task_intervention = False

    async def _api_tasks_operator_arrival(self, request):
        return await self._api_task_intervention(request)

    async def _api_tasks_skip_current(self, request):
        return await self._api_task_intervention(request, skip=True)

    async def _api_tasks_cancel(self, request):
        del request
        self._drive_session_generation += 1
        self._navigation_paused_for_drive = False
        self._pause_requested_for_drive = False
        if self._cancel_navigation_client.service_is_ready():
            await self._call_service(self._cancel_navigation_client, Trigger.Request())
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
        self._drive_session_generation += 1
        self._navigation_paused_for_drive = False
        self._pause_requested_for_drive = False
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
                    f"已中断前往{task['location']}；长按A重试，长按B人工确认到达"
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

    def _submit_delivery_chain(self, chain_name, order_key, confirmed_waypoints=None):
        """Submit a named task chain from the live catalog as pending
        confirmation (VR banner + A accept).  Returns the chain_trigger
        note dict; raises RuntimeError/ValueError with the user-facing
        reason when submission is not possible."""
        self._refresh_idle_task_catalog()
        if self._task_catalog_error:
            raise ValueError(self._task_catalog_error)
        template = next(
            (item for item in self._task_templates
             if item['name'] == chain_name), None,
        )
        if template is None:
            raise ValueError(f'任务串不存在：{chain_name}')
        if confirmed_waypoints is not None:
            missing = sorted({item['location'] for item in template['tasks']} - set(confirmed_waypoints))
            if missing:
                raise ValueError('航点库缺少地点：' + '、'.join(missing))
        self._task_workflow.submit(
            f'配送机器人联动：{template["name"]}', template['tasks']
        )
        if confirmed_waypoints is not None:
            # One operator gesture, one synchronous submit+confirm transition.
            self._task_workflow.confirm(confirmed_waypoints)
        self._showroom_single_task_active = False
        self._speak_task(f'收到，{template["name"]}，这就出发。')
        note = {
            'event': 'submitted', 'chain': chain_name, 'order': order_key,
            'total': int(template.get('task_count', len(template['tasks']))),
            'at': time.time(),
        }
        self.get_logger().info(
            f'delivery chain {chain_name} submitted for order {order_key}'
        )
        return note

    def _delivery_chain_tick(self):
        """Offer outbound jobs without replacing a pending decision or mission.

        Decisions survive polling/status changes and browser reconnections
        during this backend session. No motion is initiated by this timer.
        """
        snapshot = self._delivery_handoff.snapshot()
        chain_name = str(snapshot.get('pickup_task_chain') or '').strip()
        age = snapshot.get('last_success_age')
        if not chain_name or age is None or age > 15 or snapshot.get('state') == 'unknown':
            return
        orders = snapshot.get('orders') or []
        outbound = {'PICKUP_STARTED', 'PICKUP_AT_ROOM', 'PICKUP_TO_LAUNDRY', 'PICKUP_AT_LAUNDRY'}
        with self._delivery_offer_lock:
            offer = self._delivery_offer or {}
            if offer.get('state') == 'offered':
                if any(str(o.get('order_id') or o.get('task_id') or '') == offer['order']
                       and o.get('status_code') in outbound for o in orders):
                    return
                self._delivery_offer = {**offer, 'state': 'expired'}
            elif offer.get('state') == 'accepting':
                return
            # Inspect every order, not only the primary door-opening order.
            for order in orders:
                key = str(order.get('order_id') or order.get('task_id') or '')
                if (not key or key in self._delivery_seen_orders
                        or order.get('status_code') not in outbound):
                    continue
                self._delivery_seen_orders.add(key)
                self._delivery_offer = {
                    'state': 'offered', 'order': key, 'chain': chain_name,
                    'room_no': order.get('room'), 'status': order.get('status_code'),
                    'at': time.time(),
                }
                break

    def _delivery_state_payload(self):
        """Monitor snapshot plus the offer/note bookkeeping for the UI."""
        payload = self._delivery_handoff.snapshot()
        with self._delivery_offer_lock:
            if self._delivery_offer is not None:
                payload['offer'] = dict(self._delivery_offer)
        if self._delivery_chain_note is not None:
            payload['chain_trigger'] = self._delivery_chain_note
        return payload

    async def _api_delivery_pickup_chain_accept(self, request):
        """The delivery-card long hold is the only confirmation step."""
        try:
            payload = await self._operator_payload(request)
            self._delivery_chain_tick()
            with self._delivery_offer_lock:
                offer = dict(self._delivery_offer or {})
                if offer.get('state') != 'offered' or payload.get('order') != offer.get('order'):
                    raise RuntimeError('配送任务已变化，请查看最新卡片')
                state = self._task_workflow.snapshot().get('state')
                if state not in {'idle', 'cancelled', 'completed'} or self._navigation_is_active():
                    raise RuntimeError('请先结束当前任务或导航，再接受配送任务')
                with self._drive_lock:
                    if self._drive_sample and self._drive_sample.get('deadman'):
                        raise RuntimeError('请先退出底盘接管')
                snapshot = self._delivery_handoff.snapshot()
                age = snapshot.get('last_success_age')
                if age is None or age > 15 or snapshot.get('state') == 'unknown':
                    raise RuntimeError('配送状态连接未就绪，请稍后再试')
                with self._nav_lock:
                    if self._pose is None or str(self._relocalization_status.get('state', '')).lower() in {
                            'failed', 'failure', 'aborted', 'rejected'}:
                        raise RuntimeError('机器人定位尚未就绪，请先完成定位，再长按A')
                    waypoints = list(self._waypoints)
                # Reserve before any async navigation call; repeated A cannot resubmit.
                self._delivery_offer = {**offer, 'state': 'accepting'}
            self._submit_delivery_chain(
                str(offer.get('chain') or ''), offer.get('order'), confirmed_waypoints=waypoints
            )
        except (RuntimeError, ValueError) as error:
            with self._delivery_offer_lock:
                if (self._delivery_offer or {}).get('state') == 'accepting':
                    self._delivery_offer = offer
            return web.json_response({
                'success': False, 'message': str(error),
                'delivery_handoff': self._delivery_state_payload(),
            }, status=409)
        with self._delivery_offer_lock:
            self._delivery_offer = {**offer, 'state': 'accepted', 'at': time.time()}
        try:
            message = await self._start_current_task_navigation()
            success = True
        except (RuntimeError, ValueError) as error:
            message = str(error)
            success = False
            self.get_logger().warning(f'Accepted delivery chain failed to start: {message}')
        return web.json_response({
            'success': success,
            'message': message,
            'delivery_handoff': self._delivery_state_payload(),
            'task_dispatch': self._task_workflow.snapshot(),
        }, status=200 if success else 409)

    async def _api_delivery_pickup_chain_reject(self, request):
        """Reject the offered delivery task: back to a clean view for this
        order; it can still be started manually from the template menu."""
        try:
            payload = await self._operator_payload(request)
            with self._delivery_offer_lock:
                offer = self._delivery_offer or {}
                if offer.get('state') != 'offered' or payload.get('order') != offer.get('order'):
                    raise RuntimeError('配送任务已变化，请查看最新卡片')
                self._delivery_offer = {**offer, 'state': 'rejected', 'at': time.time()}
        except (RuntimeError, ValueError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=409)
        return web.json_response({
            'success': True,
            'message': '已忽略此配送任务',
            'delivery_handoff': self._delivery_state_payload(),
        })

    async def _api_delivery_return_chain_start(self, request):
        """Summon the delivery robot for the return leg and start the
        configured return task chain (pending VR accept).

        Fail-closed ordering: the dry-complete signal (which queues the
        RETURN_TO_ROOM trip) must succeed first; the chain is only
        submitted when the robot is actually coming, so the operator is
        never sent to the loading point to wait for a robot that will not
        arrive."""
        del request
        snapshot = self._delivery_handoff.snapshot()
        chain_name = str(snapshot.get('return_task_chain') or '').strip()
        success, message = await asyncio.to_thread(
            self._delivery_handoff.request_dry_complete
        )
        if not success:
            return web.json_response({
                'success': False,
                'message': f'未能通知配送机器人：{message}',
                'delivery_handoff': self._delivery_handoff.snapshot(),
            }, status=409)
        if not chain_name:
            return web.json_response({
                'success': False,
                'message': '未配置回程任务串（return_task_chain）',
                'delivery_handoff': self._delivery_handoff.snapshot(),
            }, status=409)
        try:
            self._delivery_chain_note = self._submit_delivery_chain(
                chain_name, str(snapshot.get('order_id') or 'return')
            )
        except (RuntimeError, ValueError) as error:
            self._delivery_chain_note = {
                'event': 'failed', 'chain': chain_name,
                'message': str(error), 'at': time.time(),
            }
            self.get_logger().warning(
                f'delivery return chain start failed: {error}'
            )
            return web.json_response({
                'success': False,
                'message': f'配送机器人已呼出，但任务串未能启动：{error}',
                'delivery_handoff': self._delivery_handoff.snapshot(),
            }, status=409)
        return web.json_response({
            'success': True,
            'message': f'已通知配送机器人，收到'
                       f'{self._delivery_chain_note["total"]}项任务，按 A 接受',
            'delivery_handoff': self._delivery_handoff.snapshot(),
            'task_dispatch': self._task_workflow.snapshot(),
        })

    async def _api_delivery_handoff_open(self, request, action='open'):
        try:
            payload = await self._operator_payload(request)
            expected = {k: str(payload.get(k) or '') for k in ('order_id', 'task_id', 'door_id')}
            if not all(expected.values()):
                raise RuntimeError('配送舱位未就绪，请刷新状态后重试')
            if action == 'close' and payload.get('confirmed') is not True:
                raise RuntimeError('请确认手臂及衣物已移开舱门，再确认关门')
        except (ValueError, RuntimeError) as error:
            return web.json_response({'success': False, 'message': str(error)}, status=409)
        # The blocking POST runs in a worker thread so the web loop keeps
        # serving VR clients; the monitor's OPENING flag plus its lock
        # collapse duplicate triggers into a single in-flight request.
        success, message = await asyncio.to_thread(
            (self._delivery_handoff.request_close if action == 'close'
             else self._delivery_handoff.request_open), expected=expected
        )
        return web.json_response({
            'success': success,
            'message': message,
            'delivery_handoff': self._delivery_handoff.snapshot(),
        }, status=200 if success else 409)

    async def _api_delivery_handoff_close(self, request):
        return await self._api_delivery_handoff_open(request, action='close')

    async def _api_delivery_handoff_wash_complete(self, request):
        del request
        # Marks the washed order done (frees the slot, queues admin drying);
        # never summons the robot.  Idempotent server-side.
        success, message = await asyncio.to_thread(
            self._delivery_handoff.request_wash_complete
        )
        return web.json_response({
            'success': success,
            'message': message,
            'delivery_handoff': self._delivery_handoff.snapshot(),
        }, status=200 if success else 409)

    async def _api_delivery_handoff_dry_complete(self, request):
        del request
        # Writes the dry timestamp and queues the RETURN_TO_ROOM trip — this
        # is the call that summons the delivery robot.  Idempotent server-side.
        success, message = await asyncio.to_thread(
            self._delivery_handoff.request_dry_complete
        )
        return web.json_response({
            'success': success,
            'message': message,
            'delivery_handoff': self._delivery_handoff.snapshot(),
        }, status=200 if success else 409)

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
        application.router.add_post('/api/gripper/width', self._api_gripper_width)
        application.router.add_post(
            '/api/delivery-handoff/open', self._api_delivery_handoff_open
        )
        application.router.add_post(
            '/api/delivery-handoff/close', self._api_delivery_handoff_close
        )
        application.router.add_post(
            '/api/delivery-handoff/wash-complete',
            self._api_delivery_handoff_wash_complete,
        )
        application.router.add_post(
            '/api/delivery-handoff/dry-complete',
            self._api_delivery_handoff_dry_complete,
        )
        application.router.add_post(
            '/api/delivery-handoff/return-chain/start',
            self._api_delivery_return_chain_start,
        )
        application.router.add_post(
            '/api/delivery-handoff/pickup-chain/accept',
            self._api_delivery_pickup_chain_accept,
        )
        application.router.add_post(
            '/api/delivery-handoff/pickup-chain/reject',
            self._api_delivery_pickup_chain_reject,
        )
        application.router.add_post('/api/navigation/waypoint', self._api_navigation_waypoint)
        application.router.add_post(
            '/api/navigation/waypoint/save', self._api_navigation_waypoint_save
        )
        application.router.add_post(
            '/api/navigation/waypoint/delete', self._api_navigation_waypoint_delete
        )
        application.router.add_post('/api/navigation/stop', self._api_navigation_stop)
        application.router.add_post('/api/navigation/release-drive', self._api_release_drive)
        application.router.add_post('/api/tasks/operator-arrival', self._api_tasks_operator_arrival)
        application.router.add_post('/api/tasks/skip-current', self._api_tasks_skip_current)
        application.router.add_post('/api/navigation/relocalize', self._api_navigation_relocalize)
        application.router.add_post(
            '/api/navigation/manual-relocalize', self._api_navigation_manual_relocalize
        )
        application.router.add_post('/api/navigation/map/switch', self._api_navigation_map_switch)
        application.router.add_post('/api/tasks/submit', self._api_tasks_submit)
        application.router.add_get('/api/tasks/menu-catalog', self._api_task_menu_catalog)
        application.router.add_post('/api/tasks/custom/start', self._api_custom_task_start)
        application.router.add_post('/api/pose/preset', self._api_pose_preset)
        application.router.add_post('/api/pose/stop', self._api_pose_stop)
        application.router.add_post('/api/navigation/voice-enabled', self._api_voice_enabled)
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
        application.router.add_post('/api/session/exit', self._api_exit_vr)
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
        self._delivery_handoff.stop()
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
