"""Start the proven arm teleop core, the navigation bridge and optional Nav2."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    teleop_share = get_package_share_directory('openarmx_teleop_vr_306_v4')
    navigation_share = get_package_share_directory('control_center_navigation')

    topic_suffix = LaunchConfiguration('topic_suffix')
    dry_run = LaunchConfiguration('dry_run')
    web_host = LaunchConfiguration('web_host')
    web_port = LaunchConfiguration('web_port')
    # Use a navigation-specific key. The included teleop launch receives
    # rgbd_camera_enabled=false and launch configurations are shared; reusing
    # that name silently disabled the navigation bridge camera too.
    navigation_rgbd_camera_enabled = LaunchConfiguration(
        'navigation_rgbd_camera_enabled'
    )
    start_navigation_stack = LaunchConfiguration('start_navigation_stack')
    robot_env_python = LaunchConfiguration('robot_env_python')
    corridor_projection_enabled = LaunchConfiguration('corridor_projection_enabled')
    task_voice_enabled = LaunchConfiguration('task_voice_enabled')
    task_voice_port = LaunchConfiguration('task_voice_port')

    teleop = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(teleop_share, 'launch', 'full_vr_teleop.launch.py')
        ),
        launch_arguments={
            'topic_suffix': topic_suffix,
            'dry_run': dry_run,
            'start_web': 'false',
            'rgbd_camera_enabled': 'false',
            'waist_follow_profile': 'forward_pitch_only',
            'waist_forward_assist_max_lean_deg': '10.0',
            # Increase only forward reach so a fully extended operator arm
            # reaches the robot's guarded 26 cm shell more naturally. The
            # absolute workspace and wrist reserve remain authoritative.
            'forward_position_scale': '1.10',
            # Keep forward bending deliberately slower than the prior 10-deg
            # navigation override. The 5-deg feedback window affects only
            # reach-triggered waist pitch, not arm or body-height speed.
            'waist_forward_assist_command_lead_deg': '5.0',
            # Exact elbow lockout is a wrist singularity. Keep only a small
            # orientation-dependent reach reserve. The old
            # 15--70 mm reserve kept the elbow visibly bent during a natural
            # forward reach; 8--35 mm still protects wrist motion near the
            # straight-arm singularity without shortening the usable reach as
            # aggressively.
            'wrist_reach_reserve_enabled': 'true',
            'wrist_reach_reserve_minimum_m': '0.008',
            'wrist_reach_reserve_maximum_m': '0.035',
            'wrist_reach_reserve_start_angle_deg': '0.0',
            'wrist_reach_reserve_full_angle_deg': '70.0',
            # Navigation view stays visually level: retain Pitch/Yaw tracking
            # while ignoring HMD side tilt on the neck Roll axis.
            'head_roll_follow_enabled': 'false',
            # X/Y height control is accepted only by this navigation launch;
            # the standalone ~/vr306 behavior remains unchanged.
            'body_height_control_enabled': 'true',
            # ~/vr306nav-only motion profile. Reset derivative limits follow
            # time scaling: velocity x1.5, acceleration x1.5^2 and jerk
            # x1.5^3, producing an approximately 1.5x faster synchronized
            # Ruckig reset without changing targets or joint bounds.
            'body_height_lowering_rate_m_sec': '0.15',
            'quick_reset_max_velocity_deg_sec': '35.1',
            'quick_reset_max_acceleration_deg_sec2': '146.25',
            'quick_reset_max_jerk_deg_sec3': '1096.875',
            'quick_reset_neck_max_velocity_deg_sec': '45.0',
            'quick_reset_neck_max_acceleration_deg_sec2': '187.5',
            'quick_reset_neck_max_jerk_deg_sec3': '1406.25',
            # ~/vr306nav reset/head-follow neutral looks down 20 degrees.
            # The standalone ~/vr306 launch keeps its original zero target.
            'quick_reset_neck_joints': '[0.0, -20.0, 0.0]',
            'head_neutral_joints_deg': '[0.0, -20.0, 0.0]',
            # Every hardware-enable and X+A reset ends with the neck held.
            # The operator explicitly hands head motion back with VR button B.
            'lock_head_follow_on_enable_reset': 'true',
        }.items(),
    )

    navigation = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(navigation_share, 'launch', 'bringup_all.launch.py')
        ),
        condition=IfCondition(start_navigation_stack),
        launch_arguments={
            'autostart_localization': 'true',
            'autostart_navigation': 'false',
            'enable_global_relocalization': 'true',
            'global_relocalization_publish_initial_pose': 'true',
            'enable_waypoint_manager': 'true',
            # Preserve robot 307's regular navigation followed by final refinement.
            'short_distance_direct_start_enabled': 'false',
            'enable_keyboard_control': 'false',
            'enable_navigation_status_overlay': 'false',
            # The task/showroom speech node below is the sole voice authority
            # for this VR workflow. Keep the generic control-center bridge
            # disabled so navigation events are not spoken twice.
            'enable_text_voice_control': 'false',
            'enable_proximity_voice_alert': 'false',
            'startup_navigation_on_initialpose': 'true',
            'enable_odom_topic_relay': 'true',
            'manual_cmd_vel_topic': '/manual_cmd_vel',
            'open_navigation_ui': 'false',
        }.items(),
    )

    web_bridge = ExecuteProcess(
        cmd=[
            robot_env_python,
            '-m', 'openarmx_teleop_vr_navigation_306.navigation_web_bridge',
            '--ros-args',
            '-p', ['host:=', web_host],
            '-p', ['https_port:=', web_port],
            '-p', ['rgbd_camera_enabled:=', navigation_rgbd_camera_enabled],
        ],
        output='screen',
    )

    corridor_projection = ExecuteProcess(
        cmd=[
            robot_env_python,
            '-m', 'openarmx_teleop_vr_navigation_306.corridor_projection_node',
            '--ros-args',
            '-p', ['topic_suffix:=', topic_suffix],
            '-p', 'output_topic:=/vr_navigation/corridor_projection',
            '-p', 'manual_cmd_vel_topic:=/manual_cmd_vel',
            '-p', 'navigation_cmd_vel_topic:=/nav_cmd_vel',
            '-p', 'odom_topic:=/odom',
            '-p', 'publish_rate_hz:=25.0',
            '-p', 'robot_width_m:=0.50',
            '-p', 'side_margin_m:=0.05',
            '-p', 'latency_compensation_seconds:=0.10',
            '-p', 'maximum_preview_heading_degrees:=22.0',
            '-p', 'front_laser_topic:=/topic_gv_front_lidar_0_307',
            '-p', 'rear_laser_topic:=/topic_gv_rear_lidar_0_307',
            '-p', 'chassis_length_m:=0.60',
            '-p', 'chassis_width_m:=0.60',
            '-p', 'laser_maximum_range_m:=1.50',
            '-p', 'obstacle_update_rate_hz:=12.5',
            '-p', 'obstacle_alert_distance_m:=1.0',
            '-p', 'obstacle_red_distance_m:=0.35',
            '-p', 'obstacle_yellow_distance_m:=0.65',
        ],
        condition=IfCondition(corridor_projection_enabled),
        output='screen',
    )

    task_voice = ExecuteProcess(
        cmd=[
            robot_env_python,
            '-m', 'openarmx_teleop_vr_navigation_306.task_voice_web',
            '--host', '0.0.0.0',
            '--port', task_voice_port,
        ],
        condition=IfCondition(task_voice_enabled),
        output='screen',
    )

    task_speech = ExecuteProcess(
        cmd=[
            robot_env_python,
            '-m', 'openarmx_teleop_vr_navigation_306.task_speech_node',
        ],
        condition=IfCondition(task_voice_enabled),
        output='screen',
    )

    voice_mode_router = ExecuteProcess(
        cmd=[
            robot_env_python,
            '-m', 'openarmx_teleop_vr_navigation_306.voice_mode_router',
        ],
        condition=IfCondition(start_navigation_stack),
        output='screen',
    )

    return LaunchDescription([
        DeclareLaunchArgument('topic_suffix', default_value='0_307'),
        DeclareLaunchArgument('dry_run', default_value='true'),
        DeclareLaunchArgument('web_host', default_value='0.0.0.0'),
        DeclareLaunchArgument('web_port', default_value='8445'),
        DeclareLaunchArgument(
            'navigation_rgbd_camera_enabled', default_value='true'
        ),
        DeclareLaunchArgument('start_navigation_stack', default_value='true'),
        DeclareLaunchArgument('corridor_projection_enabled', default_value='true'),
        DeclareLaunchArgument('task_voice_enabled', default_value='true'),
        DeclareLaunchArgument('task_voice_port', default_value='8766'),
        DeclareLaunchArgument(
            'robot_env_python',
            default_value='/home/ubuntu/miniconda3/envs/robot_env/bin/python',
        ),
        navigation,
        voice_mode_router,
        teleop,
        corridor_projection,
        web_bridge,
        task_voice,
        task_speech,
    ])
