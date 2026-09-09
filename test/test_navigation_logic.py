from pathlib import Path
from threading import Lock
from types import SimpleNamespace

from openarmx_teleop_vr_navigation_306.navigation_web_bridge import (
    NavigationVrWebBridge,
    occupancy_grid_png,
    shaped_axis,
    task_voice_text,
)
from openarmx_teleop_vr_navigation_306.corridor_projection_node import (
    directional_obstacle_clearances,
    integrate_body_twist,
    laser_points_in_base,
    planar_corridor,
    project_points,
)
from openarmx_teleop_vr_navigation_306.task_workflow import TaskWorkflow


def test_map_encoder_returns_valid_png():
    encoded = occupancy_grid_png(2, 2, [0, 100, -1, 50])
    assert encoded.startswith(b'\x89PNG\r\n\x1a\n')
    assert encoded.endswith(b'IEND\xaeB`\x82')


def test_drive_axis_has_deadzone_and_preserves_direction():
    assert shaped_axis(0.09) == 0.0
    assert shaped_axis(-0.09) == 0.0
    assert 0.0 < shaped_axis(0.7) < 1.0
    assert -1.0 < shaped_axis(-0.7) < 0.0


def test_manual_height_override_releases_operator_action_stage():
    workflow = TaskWorkflow({
        'task_locations': {
            '耗材区': [{'task': '洗手液归位', 'body_height_level': 5}],
        },
    })
    workflow.submit('测试固定流程', [
        {'location': '耗材区', 'task': '洗手液归位'},
    ])
    workflow.confirm(['耗材区'])
    workflow.navigation_started()
    workflow.navigation_arrived('耗材区')
    assert workflow.snapshot()['state'] == 'adjusting_height'

    bridge = SimpleNamespace(
        _task_height_lock=Lock(),
        _task_height_target_level=5,
        _task_height_deadline=123.0,
        _task_height_stable_since=45.0,
        _task_workflow=workflow,
        get_logger=lambda: SimpleNamespace(info=lambda _message: None),
    )
    released = NavigationVrWebBridge._release_task_height_for_manual_control(bridge)

    assert released is True
    assert bridge._task_height_target_level is None
    assert bridge._task_height_deadline == 0.0
    assert bridge._task_height_stable_since == 0.0
    assert workflow.snapshot()['state'] == 'awaiting_action'


def test_waypoint_heading_marker_exposes_pose_for_vr_preview():
    marker = SimpleNamespace(
        action=0,
        ns='waypoint_headings',
        text='吧台',
        header=SimpleNamespace(frame_id='map'),
        pose=SimpleNamespace(
            position=SimpleNamespace(x=1.25, y=-0.5),
            orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
        ),
    )
    name, pose = NavigationVrWebBridge._waypoint_pose_from_marker(marker)
    assert name == '吧台'
    assert pose == {'name': '吧台', 'x': 1.25, 'y': -0.5, 'yaw': 0.0, 'frame_id': 'map'}


def test_vr_controls_include_deadman_and_navigation_actions():
    source = (Path(__file__).parents[1] / 'web' / 'nav_app.js').read_text(encoding='utf-8')
    assert "type: 'base_drive'" in source
    assert 'sendBaseDrive(false)' in source
    assert "'/api/navigation/waypoint'" in source
    assert "'/api/navigation/relocalize'" in source
    assert "'/api/navigation/stop'" in source
    assert "'/api/tasks/skip-navigation-failure'" in source
    assert 'setBaseControlLatched(!navUi.baseControlLatched)' in source
    assert 'const deadman = Boolean(navUi.baseControlLatched)' in source
    assert "'/api/navigation/waypoint/save'" in source
    assert "'/api/navigation/waypoint/delete'" in source
    assert "kind: 'confirm-navigation'" in source
    assert '再次按 A 确认导航至' in source
    assert 'waypoint_poses' in source
    assert "'#ff4fd8'" in source
    assert 'drawCameraPreview(context,' not in source
    assert 'autolifeNavigationCameraPreview' in source
    assert 'toggleNavigationSession()' in source
    assert "setCameraMode('passthrough')" in source
    assert 'autolifeCameraModeToggleHandler' in source
    assert '云蝶 V29' in source
    assert "roundRect(context, 18, 14, 188, 50" in source
    assert "const taskCanExit = !['idle', 'cancelled', 'completed'].includes(taskState)" in source
    assert 'taskExitGestureEnabled = taskCanExit && !navUi.baseControlLatched' in source
    assert '松开 X 确认退出当前任务' in source
    assert '长按A完成动作 · 长按X退出任务' in source
    index = (Path(__file__).parents[1] / 'web' / 'index.html').read_text(encoding='utf-8')
    assert 'nav_app.js?v=yundie-306-navigation-50-map-70-v47' in index
    assert 'manualRelocalizationMoveSpeed: 1.80' in source
    assert 'manualRelocalizationMoveSpeed || 1.80' in source
    assert "kind: 'map-library'" in source
    assert "navigationAction('/api/navigation/map/switch', { path })" in source
    assert "path.includes('/map/switch') ? 15000 : 6500" in source
    assert 'drawForwardCorridorGuide()' in source
    assert '机器人前方 · 0.60 m' not in source
    assert "payload?.type === 'corridor_projection'" in source
    assert "corridorPanel.id = 'navigation-vr-corridor-panel'" in source
    assert 'NAV_PANEL_VERTICAL_OFFSET = -0.10' in source
    assert "corridorPanel.setAttribute('position', `0 ${NAV_PANEL_VERTICAL_OFFSET} -1.995`)" in source
    assert 'performance.now() - navUi.corridorReceivedAt < 450' in source
    assert 'context.lineWidth = 2.2' in source
    assert 'mapDisplayArea()' in source
    assert 'navUi.menuVisible = !navUi.menuVisible' in source
    assert '按右摇杆键打开航点菜单' in source
    assert 'context.roundRect' not in source
    assert "panel.setAttribute('width', '2.3')" in source
    assert "panel.setAttribute('height', '1.725')" in source
    assert "panel.setAttribute('position', `0 ${NAV_PANEL_VERTICAL_OFFSET} -1.99`)" in source
    assert "videoPanel.setAttribute('position', `0 ${NAV_PANEL_VERTICAL_OFFSET} -2.0`)" in source
    assert "videoPanel.id = 'navigation-vr-video-panel'" in source
    assert 'bindNavigationVideoTexture()' in source
    assert "document.querySelector('a-scene')?.camera?.el" in source
    assert "document.getElementById('cameraRig')" not in source
    assert 'const MAP_DISPLAY_SCALE = 0.70' in source
    assert 'const maximumWidth = 540 * MAP_DISPLAY_SCALE' in source
    assert 'const maximumHeight = 380 * MAP_DISPLAY_SCALE' in source
    assert 'y: 876 - height' in source
    assert 'uiScale: MAP_DISPLAY_SCALE' in source
    assert 'lastPreviewDrawAt' not in source
    assert "addEventListener('autolife-camera-frame'" in source
    assert 'recordNavigationCameraFrame()' in source
    assert 'function navigationCameraFps' in source
    assert '(samples.length - 1) * 1000 / duration' in source
    assert '画面 ${actualFps.toFixed(1)} FPS' in source
    assert 'navUi.sessionActive = false' in source
    assert "kind: 'gripper-force'" in source
    assert "fetch('/api/gripper/force'" in source
    assert '夹爪力度已应用' in source
    assert '夹爪力度设置失败' in source
    assert 'adjustGripperForce(horizontalAxis > 0 ? 1 : -1)' in source
    assert 'body_height_enabled: Boolean(deadman && bodyHeightEnabled)' in source
    assert 'bodyHeightDirection = xPressed ? 1 : -1' in source
    assert 'const quickResetChordPressed = xPressed && aPressed' in source
    assert 'navUi.quickResetHeightSuppressed = true' in source
    assert 'else if (!xPressed && !aPressed)' in source
    assert 'if (navUi.quickResetHeightSuppressed)' in source
    assert 'navUi.bodyHeightEngaged = false' in source
    assert '!navUi.baseControlLatched && yPressed' in source
    assert "context.fillStyle = navUi.baseControlLatched ? '#72f0b1' : '#a9bbc5'" in source
    assert "new Worker('/drive_worker.js?v=navigation-21-drive-smooth')" in source
    assert "type: deadman ? 'sample' : 'release'" in source
    assert "${protocol}//${window.location.host}/navigation/drive" in source
    assert 'now - navUi.lastDriveSentAt >= 28' not in source


def test_showroom_single_waypoint_voice_is_concise_and_location_based():
    task = {'location': '迎宾区', 'task': '展厅定点服务'}
    assert task_voice_text('departure', task, True) == '前往迎宾区执行任务。'
    assert task_voice_text('ready', task, True) == '已确认周围环境安全，开始执行任务。'
    assert task_voice_text('arrived', task, True) == (
        '已到达迎宾区，已确认周围环境安全，开始执行任务。'
    )
    assert task_voice_text('completed', task, True) == '迎宾区任务已完成。'


def test_regular_task_voice_keeps_action_name():
    task = {'location': '前台区', 'task': '拿水'}
    assert task_voice_text('departure', task, False) == '开始导航，前往前台区执行拿水。'
    assert task_voice_text('arrived', task, False) == (
        '已到达前台区，已确认周围环境安全，开始执行任务：拿水。'
    )
    assert task_voice_text('completed', task, False) == '拿水已完成。'


def test_arrival_voice_is_single_combined_message():
    source = (
        Path(__file__).parents[1]
        / 'openarmx_teleop_vr_navigation_306'
        / 'navigation_web_bridge.py'
    ).read_text(encoding='utf-8')
    assert '正在观察周围环境并确认任务执行方案' not in source
    assert "'arrived', task, self._showroom_single_task_active" in source
    assert "self._speak_task(task_voice_text(\n                    'ready'" not in source
    assert 'def _reload_task_catalog_if_changed(self):' in source
    assert 'self._reload_task_catalog_if_changed()' in source


def test_drive_worker_is_latest_frame_only_and_fail_safe():
    worker = (
        Path(__file__).parents[1] / 'web' / 'drive_worker.js'
    ).read_text(encoding='utf-8')
    assert 'const SEND_PERIOD_MS = 20' in worker
    assert 'const INPUT_STALE_MS = 320' in worker
    assert 'socket.bufferedAmount > MAX_BUFFERED_BYTES' in worker
    assert "socket.close(4000, 'stale drive queue')" in worker
    assert "payload.type = 'base_drive'" in worker
    assert 'payload.sequence = ++sequence' in worker
    assert "message.type === 'release'" in worker


def test_navigation_launch_selects_pitch_only_waist_assistance():
    launch = (
        Path(__file__).parents[1] / 'launch' / 'full_vr_navigation.launch.py'
    ).read_text(encoding='utf-8')
    assert "get_package_share_directory('openarmx_teleop_vr_306_v4')" in launch
    assert 'openarmx_teleop_vr_306_v3' not in launch
    assert "'waist_follow_profile': 'forward_pitch_only'" in launch
    assert "'waist_forward_assist_max_lean_deg': '10.0'" in launch
    assert "'waist_forward_assist_command_lead_deg': '5.0'" in launch
    assert "'forward_position_scale': '1.10'" in launch
    assert "'wrist_reach_reserve_enabled': 'true'" in launch
    assert "'wrist_reach_reserve_minimum_m': '0.008'" in launch
    assert "'wrist_reach_reserve_maximum_m': '0.035'" in launch
    assert "'wrist_reach_reserve_full_angle_deg': '70.0'" in launch
    assert "openarmx_teleop_vr_navigation_306.corridor_projection_node" in launch
    assert "'publish_rate_hz:=25.0'" in launch
    assert "'robot_width_m:=0.50'" in launch
    assert "'side_margin_m:=0.05'" in launch
    assert "'latency_compensation_seconds:=0.10'" in launch
    assert "'maximum_preview_heading_degrees:=22.0'" in launch
    assert "'chassis_length_m:=0.60'" in launch
    assert "'chassis_width_m:=0.60'" in launch
    assert "'obstacle_alert_distance_m:=1.0'" in launch
    assert "'obstacle_red_distance_m:=0.35'" in launch
    assert "'obstacle_yellow_distance_m:=0.65'" in launch
    assert "'obstacle_update_rate_hz:=12.5'" in launch
    assert "'body_height_control_enabled': 'true'" in launch
    assert "'short_distance_direct_start_enabled': 'false'" in launch


def test_navigation_launch_keeps_only_task_showroom_voice():
    launch = (
        Path(__file__).parents[1] / 'launch' / 'full_vr_navigation.launch.py'
    ).read_text(encoding='utf-8')
    assert "'enable_text_voice_control': 'false'" in launch
    assert 'openarmx_teleop_vr_navigation_306.task_speech_node' in launch
    assert 'openarmx_teleop_vr_navigation_306.voice_mode_router' in launch
    assert "DeclareLaunchArgument('task_voice_enabled', default_value='true')" in launch


def test_showroom_and_control_center_voice_are_mutually_exclusive():
    from openarmx_teleop_vr_navigation_306.voice_mode_router import (
        should_forward_control_center_voice,
    )

    assert should_forward_control_center_voice(False) is True
    assert should_forward_control_center_voice(True) is False
    bridge = (
        Path(__file__).parents[1]
        / 'openarmx_teleop_vr_navigation_306'
        / 'navigation_web_bridge.py'
    ).read_text(encoding='utf-8')
    assert 'if not self._showroom_mode_enabled or not text or not rclpy.ok()' in bridge
    assert 'self._publish_showroom_mode()' in bridge


def test_bridge_uses_real_vendor_gripper_force_ratio_and_private_height_topic():
    source = (
        Path(__file__).parents[1]
        / 'openarmx_teleop_vr_navigation_306'
        / 'navigation_web_bridge.py'
    ).read_text(encoding='utf-8')
    assert '/control_set_left_gripper_force_ratio_0_' in source
    assert '/control_set_right_gripper_force_ratio_0_' in source
    assert 'from openarmx_teleop_vr_306_v4.vr_web_bridge import VrWebBridge' in source
    assert '/openarmx_teleop_vr_306_v4/body_height_command' in source
    assert 'openarmx_teleop_vr_306_v3' not in source
    assert "'enabled': height_enabled" in source
    assert "'direction': height_direction" in source
    assert "json.dumps({'force': ratio}" in source
    assert 'get_subscription_count() < 1' in source
    assert "'manual_override_topic', '/manual_cmd_vel_active'" in source
    assert "'drive_watchdog_seconds', 0.36" in source
    assert "'drive_takeover_settle_seconds', 0.12" in source
    assert "'/navigation/drive', self._drive_websocket_handler" in source
    assert "'/api/gripper/force', self._api_gripper_force" in source
    assert 'async def _api_gripper_force' in source
    assert 'self._publish_manual_override(True)' in source
    assert 'self._publish_manual_override(False)' in source


def test_package_and_startup_guard_use_v4_without_allowing_parallel_teleop():
    package_root = Path(__file__).parents[1]
    package_xml = (package_root / 'package.xml').read_text(encoding='utf-8')
    startup = (
        package_root / 'scripts' / 'start_vr_navigation_306.sh'
    ).read_text(encoding='utf-8')
    corridor = (
        package_root / 'openarmx_teleop_vr_navigation_306'
        / 'corridor_projection_node.py'
    ).read_text(encoding='utf-8')
    assert '<depend>openarmx_teleop_vr_306_v4</depend>' in package_xml
    assert '<depend>openarmx_teleop_vr_306_v3</depend>' not in package_xml
    assert 'openarmx_306_v[0-9]+_(arm_controller|mapper)' in startup
    assert 'openarmx_teleop_vr_306_v[0-9]+' in startup
    assert '(controller_node|vr_mapper_node|vr_web_bridge)' in startup
    assert 'from openarmx_teleop_vr_306_v4.kinematics import (' in corridor
    assert 'from openarmx_teleop_vr_306_v4.schema import parse_joint_feedback' in corridor


def test_gripper_force_ratio_is_bounded_and_published_to_both_hands():
    class Publisher:
        def __init__(self):
            self.messages = []

        def publish(self, message):
            self.messages.append(message.data)

        def get_subscription_count(self):
            return 1

    left = Publisher()
    right = Publisher()
    bridge = SimpleNamespace(
        _left_gripper_force_publisher=left,
        _right_gripper_force_publisher=right,
        _gripper_force_ratio=1.0,
        get_parameter=lambda _name: SimpleNamespace(value=0.0),
    )
    ratio = NavigationVrWebBridge._set_gripper_force_ratio(
        bridge, {'ratio': 0.6}
    )
    assert ratio == 0.6
    assert left.messages == ['{"force":0.6}']
    assert right.messages == left.messages

    import pytest
    with pytest.raises(ValueError):
        NavigationVrWebBridge._set_gripper_force_ratio(
            bridge, {'ratio': -0.1}
        )


def test_metric_corridor_is_sixty_centimetres_wide_and_curves_with_yaw():
    distances = [0.5, 1.0, 1.5, 2.0]
    left, centre, right = planar_corridor(
        0.35, 0.0, 0.4, 0.60, distances, latency_seconds=0.10
    )
    widths = ((left - right) ** 2).sum(axis=1) ** 0.5
    assert all(abs(value - 0.60) < 1.0e-8 for value in widths)
    assert centre[-1, 1] > centre[0, 1]
    assert centre[-1, 0] > centre[0, 0]


def test_lidar_points_are_transformed_from_front_and_rear_mounts():
    front = SimpleNamespace(
        range_min=0.04, range_max=16.0, angle_min=0.0,
        angle_increment=__import__('math').pi, ranges=[1.0, 1.0],
    )
    rear = SimpleNamespace(
        range_min=0.04, range_max=16.0, angle_min=0.0,
        angle_increment=__import__('math').pi, ranges=[1.0, 1.0],
    )
    front_points = laser_points_in_base(front, 0.215, -1.0, 1.0)
    rear_points = laser_points_in_base(rear, -0.215, 1.0, -1.0)
    assert front_points[1][0] > 1.20
    assert rear_points[1][0] < -1.20


def test_obstacle_clearance_uses_sixty_centimetre_chassis_and_eight_sectors():
    points = []
    expected = {
        'front': (0.0, 0.50), 'front_left': (45.0, 0.70),
        'left': (90.0, 1.20), 'rear_left': (135.0, 0.40),
        'rear': (180.0, 0.30), 'rear_right': (-135.0, 0.80),
        'right': (-90.0, 1.50), 'front_right': (-45.0, 0.20),
    }
    for _key, (degrees, clearance) in expected.items():
        angle = __import__('math').radians(degrees)
        edge = 0.30 / max(abs(__import__('math').cos(angle)), abs(__import__('math').sin(angle)))
        radius = edge + clearance
        points.extend([(radius * __import__('math').cos(angle), radius * __import__('math').sin(angle))] * 3)
    result = directional_obstacle_clearances(points)
    assert set(result) == set(expected) - {'left', 'right'}
    assert result['front']['distance_m'] == 0.50
    assert result['front_right']['level'] == 'red'
    assert result['front']['level'] == 'yellow'
    assert result['front_left']['level'] == 'green'


def test_obstacle_overlay_is_compact_and_only_shows_within_one_metre():
    source = (Path(__file__).parents[1] / 'web' / 'nav_app.js').read_text(encoding='utf-8')
    assert 'drawDirectionalObstacleDistances(context, canvas, corridor?.obstacles)' in source
    assert "front_left: '左前'" in source
    assert 'distance > 1.0' in source
    assert 'corridorRenderKey' in source
    assert "red: '#ff625d'" in source


def test_corridor_turn_preview_stays_within_twenty_two_degrees():
    distances = __import__('numpy').linspace(0.35, 4.5, 28)
    _left, centre, _right = planar_corridor(
        0.15, 0.0, 1.3, 0.60, distances, latency_seconds=0.10
    )
    tangent = centre[-1] - centre[-2]
    heading = abs(__import__('math').atan2(tangent[1], tangent[0]))
    # The final finite-difference tangent is no larger than the configured
    # preview heading, even when the joystick commands maximum yaw.
    assert heading <= __import__('math').radians(22.0) + 1.0e-6


def test_latency_compensation_integrates_lateral_and_turn_motion():
    x, y, yaw = integrate_body_twist(0.3, 0.2, 0.5, 0.1)
    assert 0.0 < x < 0.04
    assert 0.0 < y < 0.03
    assert abs(yaw - 0.05) < 1.0e-9


def test_projection_uses_normalized_camera_pixels_and_rejects_behind_camera():
    intrinsics = {
        'fx': 600.0, 'fy': 600.0, 'ppx': 320.0, 'ppy': 240.0,
        'width': 640, 'height': 480,
    }
    points = [[0.0, 0.0, 2.0], [0.0, 0.0, -1.0]]
    projected = project_points(points, [0.0, 0.0, 0.0], __import__('numpy').eye(3), intrinsics)
    assert projected[0][3:] == (0.5, 0.5)
    assert projected[1] is None


def test_bridge_streams_only_latest_corridor_and_hides_stale_data():
    root = Path(__file__).parents[1]
    bridge = (
        root / 'openarmx_teleop_vr_navigation_306' / 'navigation_web_bridge.py'
    ).read_text(encoding='utf-8')
    assert "'corridor_projection_topic', '/vr_navigation/corridor_projection'" in bridge
    assert 'async def _corridor_websocket_sender' in bridge
    assert 'age <= 0.35' in bridge
    assert 'sequence != last_sequence' in bridge
    assert "'/api/navigation/map/switch'" in bridge
    assert "'maps': self._map_catalog()" in bridge
    assert 'SwitchWaypointMap.Request()' in bridge
    assert 'await asyncio.sleep(1.2)' in bridge
    assert "'/monitor/ws'" in bridge
    assert "'/api/monitor/state'" in bridge
    assert "'/monitor'" in bridge
    assert 'async def _monitor_websocket_handler' in bridge


def test_computer_monitor_is_read_only_and_does_not_open_a_second_webrtc_peer():
    root = Path(__file__).parents[1]
    source = (root / 'web' / 'monitor.js').read_text(encoding='utf-8')
    nav_source = (root / 'web' / 'nav_app.js').read_text(encoding='utf-8')
    bridge = (
        root / 'openarmx_teleop_vr_navigation_306' / 'navigation_web_bridge.py'
    ).read_text(encoding='utf-8')
    assert '/api/camera/frame.jpg?camera=rgbd_head_color&after=' in source
    assert '/api/navigation/state' in source
    assert '/api/monitor/state' in source
    assert '/monitor/ws' in source
    assert '50 - (performance.now() - started)' in source
    assert 'RTCPeerConnection' not in source
    assert '/api/hardware-control' not in source
    assert '/api/navigation/waypoint' not in source
    assert 'view: monitorViewState()' in nav_source
    assert 'const DESIGN_WIDTH = 1200' in source
    assert 'const DESIGN_HEIGHT = 900' in source
    assert 'return { x: 24, y: 876 - height' in source
    assert 'drawNavigationInset' not in source
    assert 'sendMonitorViewState();' in nav_source
    assert 'socket.bufferedAmount > 4096' in nav_source
    assert "payload.get('type') == 'monitor_view'" in bridge
    assert "'depth_valid_points': depth_valid_points" in bridge
    assert "payload.get('depth_last_error', '')" in bridge


def test_public_audience_view_is_read_only_lightweight_and_hides_teleop_ui():
    root = Path(__file__).parents[1]
    html = (root / 'web' / 'audience.html').read_text(encoding='utf-8')
    css = (root / 'web' / 'audience.css').read_text(encoding='utf-8')
    source = (root / 'web' / 'audience.js').read_text(encoding='utf-8')
    bridge = (
        root / 'openarmx_teleop_vr_navigation_306' / 'navigation_web_bridge.py'
    ).read_text(encoding='utf-8')

    assert '云蝶 · 自主任务执行中心' in html
    assert '自主任务序列' in html
    assert '机器人实时视野' in html
    assert 'audience.css?v=audience-v2' in html
    assert 'audience.js?v=audience-v2' in html
    assert 'height: 100dvh' in css
    assert 'grid-template-rows: auto auto minmax(0, 1fr) auto' in css
    assert 'overflow: hidden; overscroll-behavior: none' in css
    assert 'min-height: calc(100vh' not in css
    assert '/api/navigation/state' in source
    assert '/api/navigation/map.png?v=' in source
    assert '/api/camera/frame.jpg?camera=rgbd_head_color&after=' in source
    assert '84 - (performance.now() - started)' in source
    assert 'document.hidden' in source
    assert "application.router.add_get('/audience', self._audience_index)" in bridge
    assert "application.router.add_get('/show', self._audience_index)" in bridge
    assert 'async def _audience_index' in bridge

    combined = '\n'.join((html, css, source))
    for forbidden in (
        '/api/monitor/state', '/api/status', '/monitor/ws',
        'new WebSocket', 'RTCPeerConnection', '/api/hardware-control',
        'body_height_level', '长按A', '长按X', '右摇杆', '头部已锁定',
    ):
        assert forbidden not in combined


def test_navigation_web_client_uses_y_for_camera_and_b_for_head_lock():
    root = Path(__file__).parents[1]
    source = (root / 'web' / 'vr_app.js').read_text(encoding='utf-8')
    navigation = (root / 'web' / 'nav_app.js').read_text(encoding='utf-8')
    assert 'this.cameraTogglePressed = false' in source
    assert 'if (!this.cameraTogglePressed) toggleCameraMode()' not in source
    assert 'if (currentB && !this.cameraTogglePressed) toggleCameraMode()' not in source
    assert 'autolifeCameraModeToggleHandler' in source
    assert 'globalThis.autolifeHeadFollowControl' in source
    assert 'if (bPressed && !navUi.previous.b) toggleHeadMotionLock()' in navigation
    assert "modes.push('depth_cloud')" not in navigation
    assert "modes.push('precision_cloud')" in navigation
    assert 'connectDepthCloud()' in navigation
    assert 'disconnectDepthCloud()' in navigation
    assert '/navigation/depth-cloud' not in navigation
    assert 'event.data instanceof Blob' in navigation
    assert 'new THREE.BufferGeometry()' in navigation
    assert 'new THREE.PointsMaterial({' in navigation
    assert 'color: 0xffffff' in navigation
    assert 'new THREE.Points(geometry, material)' in navigation
    assert 'points.renderOrder = 10' in navigation
    assert 'positions[target] = raysX[index] * distance' in navigation
    assert 'positions[target + 1] = raysY[index] * distance' in navigation
    assert 'positions[target + 2] = -distance' in navigation
    assert 'navUi.depthGeometry.setDrawRange(0, validPoints)' in navigation
    assert "? 'official_rgb565_points_3d' : 'disabled'" in navigation
    assert 'depth_last_error: String(navUi.depthLastError' in navigation
    assert "NAVIGATION_ASSET_VERSION = 'yundie-306-navigation-50-map-70-v47'" in navigation
    assert 'function depthCloudIsReady(' in navigation
    assert 'return navUi.depthValidPoints > 0 && navUi.depthLastFrameAt > 0' in navigation
    assert 'const rgbFallback = selected && !ready' in navigation
    assert 'if (navUi.depthPendingFrame) renderPendingDepthCloudFrame()' in navigation
    assert "socket.close(1012, 'depth stream stalled')" in navigation
    assert 'navUi.depthLastPacketAt = navUi.depthConnectedAt' in navigation
    assert 'const reference = navUi.depthLastPacketAt || navUi.depthConnectedAt' in navigation
    assert 'depthNow - reference > 5000' in navigation
    assert 'depthNow - reference > 1200' not in navigation
    assert "type: 'depth_frame_ack', sequence" in navigation
    assert 'size: 2.0' in navigation
    assert 'sizeAttenuation: false' in navigation
    assert 'new THREE.PlaneGeometry(40, 40)' in navigation
    assert 'points.frustumCulled = false' in navigation
    assert 'points.raycast = () => {}' in navigation
    assert 'scheduleNavigationVersionCheck(config.navigation?.ui_version)' in navigation
    assert 'window.setInterval(checkNavigationAssetVersion, 30000)' in navigation
    assert 'revision(remoteVersion) > revision(NAVIGATION_ASSET_VERSION)' in navigation
    assert "background.name = 'navigation-depth-black-background'" in navigation
    assert '/navigation/precision-cloud' in navigation
    assert "config.sampling === 'stable_uniform_uv'" in navigation
    assert 'view.getUint16(colourPlaneOffset + index * 2, true)' in navigation
    assert 'vertexColors: coloured' in navigation
    assert 'function queueDepthCloudFrame(buffer, generation)' in navigation
    assert 'navUi.depthPendingFrame = { buffer, generation }' in navigation
    assert 'xrSession.requestAnimationFrame(renderLatest)' in navigation
    assert 'window.requestAnimationFrame(renderLatest)' in navigation
    assert 'const raysX = new Float32Array(capacity)' in navigation
    assert 'raysX[index] = (sourceU - ppx) / fx' in navigation
    assert 'new THREE.DataTexture(' not in navigation
    assert 'function adaptiveDepthWindow(' not in navigation
    release = navigation.index("} else if (navUi.previous.a) {")
    complete = navigation.index("? '/api/tasks/complete' : '/api/tasks/retry-navigation-failure'")
    assert release < complete
    assert 'if (xPressed || navUi.taskHoldMode !== taskHoldMode)' in navigation
    assert "state.image.mode !== 'rgbd' && !navigationCameraPreviewActive()" in source
    assert "state.image.mode === 'rgbd' || navigationCameraPreviewActive()" in source
    assert 'notifyCameraFrame(id)' in source
    assert 'getTexture: () =>' in source
    assert 'pumpIndependentVideoFrames(cameraState, peer, generation)' in source
    assert "fetch('/api/webrtc/status'" not in source
    independent = source[source.index('async function startCameraFeed(id)'):]
    independent = independent[:independent.index('function createAxisPart(')]
    assert 'using MJPEG fallback' not in independent
    assert 'scheduleCameraReconnect(cameraState, `video setup failed:' in independent


def test_precision_cloud_matches_official_robot_side_sampling_contract():
    root = Path(__file__).parents[1]
    bridge = (root / 'openarmx_teleop_vr_navigation_306' /
              'navigation_web_bridge.py').read_text(encoding='utf-8')
    stream = (root / 'openarmx_teleop_vr_navigation_306' /
              'depth_cloud_stream.py').read_text(encoding='utf-8')
    assert "self.declare_parameter('precision_cloud_point_count', 18000)" in bridge
    assert "self.declare_parameter('precision_cloud_maximum_fps', 30.0)" in bridge
    assert 'max_msg_size=256 * 1024, compress=False' in bridge
    assert "'flow_control': 'render_ack_latest_v1'" in bridge
    assert 'await self._wait_for_point_cloud_ack(' in bridge
    assert 'source.snapshot(\n                    sequence, 0.0' in bridge
    assert 'run_in_executor' not in bridge
    assert "payload.get('type') == 'depth_frame_ack'" in bridge
    assert "'/navigation/precision-cloud'" in bridge
    assert 'sample_count=int(' in bridge
    assert 'include_color=True' in bridge
    assert "'stable_uniform_uv'" in stream
    assert "'depth16_rgb565_planes'" in stream
    assert "if int(color_frame_id) != int(source_frame_id)" in stream
    assert "color[self._sample_v, self._sample_u, :]" in stream
    assert 'nonblock=True, with_meta=True' in stream
    assert "'depth input stalled; reopening SHM consumers'" in stream
    assert "'RGB-D pairing stalled; reopening SHM consumers'" in stream
    assert "'recoveries': int(recovery_count)" in stream


def test_waypoint_save_uses_nonblocking_vr_editor_and_never_stays_locked_after_timeout():
    root = Path(__file__).parents[1]
    source = (root / 'web' / 'nav_app.js').read_text(encoding='utf-8')
    bridge = (
        root / 'openarmx_teleop_vr_navigation_306' / 'navigation_web_bridge.py'
    ).read_text(encoding='utf-8')
    assert 'window.prompt(' not in source
    assert 'WAYPOINT_NAME_KEYS' in source
    assert 'drawWaypointNameEditor(context)' in source
    assert 'activateWaypointNameKey()' in source
    assert '右摇杆选择 · A 输入 · X 退格 · Y 取消' in source
    assert "renderer?.xr?.getSession?.()" in source
    assert '请先进入 VR 导航界面后再保存航点' in source
    assert "navigationAction('/api/navigation/waypoint/save', { name })" in source
    assert 'new AbortController()' in source
    assert 'abortController.abort()' in source
    assert 'navUi.actionPending = false' in source
    assert 'navUi.deleteMode = false' in source
    assert 'self._waypoint_query_generation += 1' in bridge
    assert 'generation != self._waypoint_query_generation' in bridge
    assert 'self._request_waypoint_list(force=True)' in bridge
    assert "'Clear-Site-Data': '\"cache\"'" in bridge


def test_forced_waypoint_refresh_ignores_an_older_query_result():
    class FakeFuture:
        def __init__(self):
            self.callback = None
            self.value = None

        def add_done_callback(self, callback):
            self.callback = callback

        def result(self):
            return self.value

        def finish(self, names):
            self.value = SimpleNamespace(success=True, names=names)
            self.callback(self)

    class FakeClient:
        def __init__(self):
            self.futures = []

        @staticmethod
        def service_is_ready():
            return True

        def call_async(self, _request):
            future = FakeFuture()
            self.futures.append(future)
            return future

    client = FakeClient()
    bridge = SimpleNamespace(
        _waypoint_query_pending=False,
        _waypoint_query_generation=0,
        _list_waypoints_client=client,
        _nav_lock=Lock(),
        _waypoints=[],
        get_logger=lambda: SimpleNamespace(warning=lambda _message: None),
    )
    NavigationVrWebBridge._request_waypoint_list(bridge)
    NavigationVrWebBridge._request_waypoint_list(bridge, force=True)
    client.futures[0].finish(['删除前的旧列表'])
    assert bridge._waypoints == []
    client.futures[1].finish(['删除后的新列表'])
    assert bridge._waypoints == ['删除后的新列表']
    assert bridge._waypoint_query_pending is False


def test_manual_drive_release_resumes_navigation_without_voice_prompt():
    root = Path(__file__).parents[1]
    bridge = (
        root / 'openarmx_teleop_vr_navigation_306' / 'navigation_web_bridge.py'
    ).read_text(encoding='utf-8')
    assert "Navigation resumed after manual base takeover" in bridge
    assert "手动接管结束，继续前往任务地点。" not in bridge


def test_native_desktop_shell_embeds_logo_and_owns_stack_lifecycle():
    root = Path(__file__).parents[1]
    desktop = (root / 'scripts' / 'vr_navigation_desktop_app.py').read_text(
        encoding='utf-8'
    )
    launcher = (root / 'scripts' / 'start_vr_navigation_306.sh').read_text(
        encoding='utf-8'
    )
    setup_source = (root / 'setup.py').read_text(encoding='utf-8')
    assert 'WebKit2.WebView()' in desktop
    assert 'self.window.fullscreen()' in desktop
    assert 'logo_server.py' in desktop
    assert 'set_display_mode.sh' in desktop
    assert 'environment["AUTOLIFE_EMBEDDED_LOGO"] = "1"' in desktop
    assert 'signal.SIGINT if graceful else signal.SIGTERM' in desktop
    assert 'start_new_session=True' in desktop
    assert 'AUTOLIFE_EMBEDDED_LOGO' in launcher
    assert '由桌面程序窗口托管' in launcher
    assert "'scripts/vr_navigation_desktop_app.py'" in setup_source
