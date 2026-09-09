"""Project a metric base corridor into the aligned head RGB-D image.

The browser deliberately receives only normalized 2-D points.  Kinematics,
depth validation, velocity filtering and latency compensation stay on the
robot so the headset has no model or depth-processing burden.
"""

# The robot_env OpenSSL must be loaded before rclpy loads system libcrypto.
import ssl  # noqa: F401

import json
import math
import os
import time

import numpy as np
import pinocchio as pin
import rclpy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String

from openarmx_teleop_vr_306_v4.kinematics import (
    LEFT_ARM_JOINTS,
    LEG_WAIST_JOINTS,
    NECK_JOINTS,
    RIGHT_ARM_JOINTS,
)
from openarmx_teleop_vr_306_v4.schema import parse_joint_feedback


DEFAULT_URDF = (
    '/home/ubuntu/miniconda3/envs/robot_env/lib/python3.12/site-packages/'
    'autolife_robot_sdk/descriptions/autolife_s1/urdfs/robot_v2_2.urdf'
)

# The forehead link uses z-forward, y-right and x-up.  Columns below express
# optical x-right, y-down, z-forward in that link frame.
CAMERA_LINK_R_OPTICAL = np.array(
    [[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]], dtype=float
)

DIRECTION_KEYS = (
    'front', 'front_left', 'left', 'rear_left',
    'rear', 'rear_right', 'right', 'front_right',
)


def integrate_body_twist(vx, vy, wz, duration):
    """Integrate a constant body-frame planar twist."""
    duration = max(0.0, float(duration))
    if abs(wz) < 1.0e-6:
        return float(vx) * duration, float(vy) * duration, float(wz) * duration
    theta = float(wz) * duration
    sine = math.sin(theta)
    cosine = math.cos(theta)
    return (
        (sine * float(vx) - (1.0 - cosine) * float(vy)) / float(wz),
        ((1.0 - cosine) * float(vx) + sine * float(vy)) / float(wz),
        theta,
    )


def planar_corridor(
    vx,
    vy,
    wz,
    width,
    distances,
    latency_seconds=0.10,
    maximum_preview_heading_rad=math.radians(22.0),
):
    """Return left/centre/right metric ground points in the base frame."""
    distances = np.asarray(tuple(distances), dtype=float)
    speed = math.hypot(float(vx), float(vy))
    travel_angle = math.atan2(float(vy), float(vx)) if speed > 0.025 else 0.0
    farthest = max(0.01, float(np.max(np.abs(distances))))
    heading_limit = max(0.0, float(maximum_preview_heading_rad))
    lead_x, lead_y, lead_yaw = integrate_body_twist(vx, vy, wz, latency_seconds)
    raw_curvature = float(np.clip(float(wz) / max(speed, 0.18), -2.0, 2.0))
    # Bound the yaw-induced preview heading including latency compensation.
    # This preserves the requested turning direction without allowing a large
    # right-stick command to throw the entire overlay outside the RGB view.
    far_yaw_heading = float(np.clip(
        lead_yaw + raw_curvature * farthest,
        -heading_limit,
        heading_limit,
    ))
    curvature = (far_yaw_heading - lead_yaw) / farthest
    half_width = 0.5 * float(width)
    left, centre, right = [], [], []
    for distance in distances:
        distance = float(distance)
        if abs(curvature) < 1.0e-6:
            local_x = distance * math.cos(travel_angle)
            local_y = distance * math.sin(travel_angle)
            tangent = travel_angle
        else:
            tangent = travel_angle + curvature * distance
            local_x = (math.sin(tangent) - math.sin(travel_angle)) / curvature
            local_y = (-math.cos(tangent) + math.cos(travel_angle)) / curvature
        cosine = math.cos(lead_yaw)
        sine = math.sin(lead_yaw)
        x = lead_x + cosine * local_x - sine * local_y
        y = lead_y + sine * local_x + cosine * local_y
        tangent += lead_yaw
        normal_x, normal_y = -math.sin(tangent), math.cos(tangent)
        centre.append([x, y, 0.0])
        left.append([x + half_width * normal_x, y + half_width * normal_y, 0.0])
        right.append([x - half_width * normal_x, y - half_width * normal_y, 0.0])
    return np.asarray(left), np.asarray(centre), np.asarray(right)


def project_points(points_base, camera_translation, camera_rotation, intrinsics):
    """Project base-frame points; invalid points are represented by None."""
    points = np.asarray(points_base, dtype=float)
    translation = np.asarray(camera_translation, dtype=float).reshape(3)
    rotation = np.asarray(camera_rotation, dtype=float).reshape(3, 3)
    optical = (rotation.T @ (points - translation).T).T
    width = int(intrinsics['width'])
    height = int(intrinsics['height'])
    result = []
    for xyz in optical:
        if not np.all(np.isfinite(xyz)) or xyz[2] <= 0.08:
            result.append(None)
            continue
        u = float(intrinsics['fx']) * xyz[0] / xyz[2] + float(intrinsics['ppx'])
        v = float(intrinsics['fy']) * xyz[1] / xyz[2] + float(intrinsics['ppy'])
        result.append((u, v, float(xyz[2]), u / width, v / height))
    return result


def laser_points_in_base(scan, longitudinal_offset, cosine_sign, sine_sign, maximum_range=3.0):
    """Convert one horizontally mounted 360-degree scan into base-frame XY points."""
    points = []
    lower = max(0.0, float(scan.range_min))
    upper = min(float(scan.range_max), float(maximum_range))
    for index, raw_range in enumerate(scan.ranges):
        distance = float(raw_range)
        if not math.isfinite(distance) or distance <= lower or distance >= upper:
            continue
        angle = float(scan.angle_min) + index * float(scan.angle_increment)
        points.append((
            float(longitudinal_offset) + float(cosine_sign) * distance * math.cos(angle),
            float(sine_sign) * distance * math.sin(angle),
        ))
    return points


def directional_obstacle_clearances(
    points,
    chassis_length=0.60,
    chassis_width=0.60,
    alert_distance=1.0,
    red_distance=0.35,
    yellow_distance=0.65,
):
    """Return robust nearest clearance from the chassis edge in eight sectors."""
    half_length = 0.5 * float(chassis_length)
    half_width = 0.5 * float(chassis_width)
    buckets = {key: [] for key in DIRECTION_KEYS}
    for raw_x, raw_y in points:
        x, y = float(raw_x), float(raw_y)
        if not math.isfinite(x) or not math.isfinite(y):
            continue
        radius = math.hypot(x, y)
        if radius <= 1.0e-6:
            continue
        cosine, sine = x / radius, y / radius
        edge_x = half_length / abs(cosine) if abs(cosine) > 1.0e-6 else math.inf
        edge_y = half_width / abs(sine) if abs(sine) > 1.0e-6 else math.inf
        clearance = radius - min(edge_x, edge_y)
        # Returns inside the assumed chassis are reflections from the robot itself.
        if clearance < 0.015 or clearance > float(alert_distance):
            continue
        bearing = math.atan2(y, x)
        sector = int(math.floor((bearing + math.pi / 8.0) / (math.pi / 4.0))) % 8
        buckets[DIRECTION_KEYS[sector]].append(clearance)

    result = {}
    for key, values in buckets.items():
        if not values:
            continue
        values.sort()
        # The third-nearest ray rejects isolated lidar speckles without hiding
        # narrow real obstacles that normally cover several adjacent rays.
        distance = values[min(2, len(values) - 1)]
        level = 'red' if distance <= red_distance else (
            'yellow' if distance <= yellow_distance else 'green'
        )
        result[key] = {
            'distance_m': round(float(distance), 2),
            'level': level,
        }
    return result


class CorridorProjectionNode(Node):
    def __init__(self):
        super().__init__('vr_corridor_projection_306')
        self.declare_parameter('topic_suffix', '0_307')
        self.declare_parameter('output_topic', '/vr_navigation/corridor_projection')
        self.declare_parameter('manual_cmd_vel_topic', '/manual_cmd_vel')
        self.declare_parameter('navigation_cmd_vel_topic', '/nav_cmd_vel')
        self.declare_parameter('odom_topic', '/odom')
        self.declare_parameter('publish_rate_hz', 25.0)
        self.declare_parameter('robot_width_m', 0.50)
        self.declare_parameter('side_margin_m', 0.05)
        self.declare_parameter('preview_near_m', 0.35)
        self.declare_parameter('preview_far_m', 4.50)
        self.declare_parameter('preview_samples', 28)
        self.declare_parameter('velocity_filter_seconds', 0.12)
        self.declare_parameter('latency_compensation_seconds', 0.10)
        self.declare_parameter('maximum_preview_heading_degrees', 22.0)
        self.declare_parameter('depth_stale_seconds', 0.25)
        self.declare_parameter('joint_stale_seconds', 0.40)
        self.declare_parameter('obstacle_clearance_m', 0.16)
        self.declare_parameter('front_laser_topic', '/topic_gv_front_lidar_0_307')
        self.declare_parameter('rear_laser_topic', '/topic_gv_rear_lidar_0_307')
        self.declare_parameter('laser_stale_seconds', 0.40)
        self.declare_parameter('laser_maximum_range_m', 1.50)
        self.declare_parameter('obstacle_update_rate_hz', 12.5)
        self.declare_parameter('chassis_length_m', 0.60)
        self.declare_parameter('chassis_width_m', 0.60)
        self.declare_parameter('obstacle_alert_distance_m', 1.0)
        self.declare_parameter('obstacle_red_distance_m', 0.35)
        self.declare_parameter('obstacle_yellow_distance_m', 0.65)
        self.declare_parameter('urdf_path', DEFAULT_URDF)
        self.declare_parameter('base_frame', 'Link_Zero_Point')
        self.declare_parameter('camera_frame', 'Link_Camera_Head_Forehead')

        urdf_path = str(self.get_parameter('urdf_path').value)
        if not os.path.isfile(urdf_path):
            raise RuntimeError(f'corridor URDF does not exist: {urdf_path}')
        self._model = pin.buildModelFromUrdf(urdf_path)
        self._data = self._model.createData()
        self._base_frame_id = self._required_frame(
            str(self.get_parameter('base_frame').value)
        )
        self._camera_frame_id = self._required_frame(
            str(self.get_parameter('camera_frame').value)
        )
        self._joint_indices = {}
        for name in LEG_WAIST_JOINTS + LEFT_ARM_JOINTS + RIGHT_ARM_JOINTS + NECK_JOINTS:
            joint_id = self._model.getJointId(name)
            if joint_id == 0 or self._model.joints[joint_id].nq != 1:
                raise RuntimeError(f'corridor URDF is missing one-DoF joint {name}')
            self._joint_indices[name] = self._model.joints[joint_id].idx_q

        self._groups = None
        self._joint_received_at = 0.0
        self._twists = {}
        self._odom_twist = np.zeros(3, dtype=float)
        self._odom_received_at = 0.0
        self._filtered_twist = np.zeros(3, dtype=float)
        self._last_tick = time.monotonic()
        self._sequence = 0
        self._depth_consumer = None
        self._depth = None
        self._depth_frame_id = -1
        self._depth_received_at = 0.0
        self._intrinsics = None
        self._next_camera_open_at = 0.0
        self._laser_points = {}
        self._laser_received_at = {}
        self._laser_generation = 0
        self._obstacle_generation = -1
        self._obstacle_sources = ()
        self._obstacle_computed_at = 0.0
        self._obstacle_cache = {'valid': False, 'directions': {}}

        latest_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.BEST_EFFORT,
        )
        reliable_qos = QoSProfile(depth=5, reliability=ReliabilityPolicy.RELIABLE)
        suffix = str(self.get_parameter('topic_suffix').value)
        self.create_subscription(
            String,
            f'/topic_arm_whole_body_and_gripper_current_joints_status_{suffix}',
            self._on_joint_feedback,
            latest_qos,
        )
        for topic in (
            str(self.get_parameter('manual_cmd_vel_topic').value),
            str(self.get_parameter('navigation_cmd_vel_topic').value),
        ):
            self.create_subscription(
                Twist, topic, lambda message, source=topic: self._on_twist(source, message), latest_qos
            )
        self.create_subscription(
            Odometry,
            str(self.get_parameter('odom_topic').value),
            self._on_odom,
            latest_qos,
        )
        for source, topic, offset, cosine_sign, sine_sign in (
            ('front', str(self.get_parameter('front_laser_topic').value), 0.215, -1.0, 1.0),
            ('rear', str(self.get_parameter('rear_laser_topic').value), -0.215, 1.0, -1.0),
        ):
            self.create_subscription(
                LaserScan,
                topic,
                lambda message, source=source, offset=offset,
                       cosine_sign=cosine_sign, sine_sign=sine_sign:
                    self._on_laser(source, offset, cosine_sign, sine_sign, message),
                latest_qos,
            )
        self._publisher = self.create_publisher(
            String, str(self.get_parameter('output_topic').value), reliable_qos
        )
        rate = float(np.clip(self.get_parameter('publish_rate_hz').value, 10.0, 30.0))
        self.create_timer(1.0 / rate, self._tick)
        self.get_logger().info(
            f'VR metric corridor ready at {rate:.1f} Hz; depth-aligned projection, '
            'dynamic full-body FK and 100 ms motion compensation enabled'
        )

    def _required_frame(self, name):
        if not self._model.existFrame(name):
            raise RuntimeError(f'corridor URDF is missing frame {name}')
        return self._model.getFrameId(name)

    def _on_joint_feedback(self, message):
        try:
            parsed = parse_joint_feedback(json.loads(message.data))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self.get_logger().warning(f'invalid corridor joint feedback: {error}')
            return
        self._groups = parsed.as_dict()
        self._joint_received_at = time.monotonic()

    def _on_twist(self, source, message):
        vector = np.asarray(
            [message.linear.x, message.linear.y, message.angular.z], dtype=float
        )
        if np.all(np.isfinite(vector)):
            self._twists[source] = (time.monotonic(), vector)

    def _on_odom(self, message):
        vector = np.asarray(
            [message.twist.twist.linear.x, message.twist.twist.linear.y,
             message.twist.twist.angular.z], dtype=float
        )
        if np.all(np.isfinite(vector)):
            self._odom_twist = vector
            self._odom_received_at = time.monotonic()

    def _on_laser(self, source, offset, cosine_sign, sine_sign, message):
        self._laser_points[source] = laser_points_in_base(
            message,
            offset,
            cosine_sign,
            sine_sign,
            float(self.get_parameter('laser_maximum_range_m').value),
        )
        self._laser_received_at[source] = time.monotonic()
        self._laser_generation += 1

    def _obstacle_snapshot(self, now):
        stale_after = float(self.get_parameter('laser_stale_seconds').value)
        fresh_sources = [
            source for source, received_at in self._laser_received_at.items()
            if now - received_at <= stale_after
        ]
        if not fresh_sources:
            return {'valid': False, 'directions': {}}
        source_signature = tuple(sorted(fresh_sources))
        update_rate = max(
            1.0, float(self.get_parameter('obstacle_update_rate_hz').value)
        )
        if (
            source_signature == self._obstacle_sources
            and (
                self._obstacle_generation == self._laser_generation
                or now - self._obstacle_computed_at < 1.0 / update_rate
            )
        ):
            return self._obstacle_cache
        points = []
        for source in fresh_sources:
            points.extend(self._laser_points.get(source, ()))
        chassis_length = float(self.get_parameter('chassis_length_m').value)
        chassis_width = float(self.get_parameter('chassis_width_m').value)
        alert_distance = float(self.get_parameter('obstacle_alert_distance_m').value)
        self._obstacle_cache = {
            'valid': True,
            'chassis_m': [round(chassis_length, 3), round(chassis_width, 3)],
            'limit_m': round(alert_distance, 3),
            'source_count': len(fresh_sources),
            'directions': directional_obstacle_clearances(
                points,
                chassis_length,
                chassis_width,
                alert_distance,
                float(self.get_parameter('obstacle_red_distance_m').value),
                float(self.get_parameter('obstacle_yellow_distance_m').value),
            ),
        }
        self._obstacle_generation = self._laser_generation
        self._obstacle_sources = source_signature
        self._obstacle_computed_at = now
        return self._obstacle_cache

    def _open_depth_if_needed(self, now):
        if self._depth_consumer is not None or now < self._next_camera_open_at:
            return
        self._next_camera_open_at = now + 2.0
        try:
            from autolife_robot_sdk.utils.camera_shm_catalog import (
                get_camera_shm_output,
                open_camera_shm_consumer,
            )
            output = get_camera_shm_output('mod_camera_rgbd_head', 'depth')
            self._depth_consumer = open_camera_shm_consumer(
                output, name='vr_corridor_projection_306'
            )
            raw = self._depth_consumer.get_intrinsics()
            if not isinstance(raw, dict):
                raise RuntimeError('RGB-D intrinsics are unavailable')
            self._intrinsics = {
                'fx': float(raw['fx']), 'fy': float(raw['fy']),
                'ppx': float(raw['ppx']), 'ppy': float(raw['ppy']),
                'width': int(output.width), 'height': int(output.height),
            }
        except Exception as error:
            if self._depth_consumer is not None:
                self._depth_consumer.close()
            self._depth_consumer = None
            self._intrinsics = None
            self.get_logger().warning(f'waiting for aligned RGB-D depth: {error}')

    def _read_depth(self, now):
        self._open_depth_if_needed(now)
        if self._depth_consumer is None:
            return
        try:
            item = self._depth_consumer.get_latest(nonblock=True, with_meta=True)
            if item is None:
                return
            depth, frame_id, meta = item
            if depth.ndim != 2 or depth.dtype != np.uint16:
                raise RuntimeError(f'unexpected depth format {depth.shape}/{depth.dtype}')
            if self._intrinsics is None or (
                depth.shape[1] != self._intrinsics['width']
                or depth.shape[0] != self._intrinsics['height']
            ):
                raise RuntimeError('depth geometry does not match aligned color intrinsics')
            self._depth = depth
            self._depth_frame_id = int(frame_id)
            self._depth_received_at = now
        except Exception as error:
            self.get_logger().warning(f'RGB-D depth consumer reset: {error}')
            try:
                self._depth_consumer.close()
            except Exception:
                pass
            self._depth_consumer = None
            self._depth = None
            self._intrinsics = None

    def _selected_twist(self, now):
        candidates = [value for timestamp, value in self._twists.values()
                      if now - timestamp <= 0.30]
        if candidates:
            return max(candidates, key=lambda item: float(np.linalg.norm(item)))
        if now - self._odom_received_at <= 0.30:
            return self._odom_twist.copy()
        return np.zeros(3, dtype=float)

    def _camera_pose(self):
        q = pin.neutral(self._model)
        assignments = (
            (LEG_WAIST_JOINTS, self._groups['leg_waist']),
            (LEFT_ARM_JOINTS, self._groups['left_arm']),
            (RIGHT_ARM_JOINTS, self._groups['right_arm']),
            (NECK_JOINTS, self._groups['neck']),
        )
        for names, values in assignments:
            for name, degrees in zip(names, values):
                q[self._joint_indices[name]] = math.radians(float(degrees))
        pin.forwardKinematics(self._model, self._data, q)
        pin.updateFramePlacements(self._model, self._data)
        transform = (
            self._data.oMf[self._base_frame_id].inverse()
            * self._data.oMf[self._camera_frame_id]
        )
        return transform.translation.copy(), transform.rotation @ CAMERA_LINK_R_OPTICAL

    def _depth_at(self, projected):
        if projected is None or self._depth is None:
            return None
        u, v = int(round(projected[0])), int(round(projected[1]))
        if u < 2 or v < 2 or u >= self._depth.shape[1] - 2 or v >= self._depth.shape[0] - 2:
            return None
        patch = self._depth[v - 2:v + 3, u - 2:u + 3]
        valid = patch[patch > 0]
        if valid.size < 5:
            return None
        return float(np.median(valid)) * 0.001

    @staticmethod
    def _normalized_triplets(left, centre, right):
        output = [[], [], []]
        blocked_source_indices = []
        for source_index, triple in enumerate(zip(left, centre, right)):
            if any(point is None for point in triple):
                continue
            if not all(-0.10 <= point[3] <= 1.10 and -0.10 <= point[4] <= 1.10
                       for point in triple):
                continue
            for destination, point in zip(output, triple):
                destination.append([round(point[3], 5), round(point[4], 5)])
            blocked_source_indices.append(source_index)
        return output, blocked_source_indices

    def _publish(self, valid, reason='', **fields):
        self._sequence += 1
        fields.setdefault('obstacles', self._obstacle_snapshot(time.monotonic()))
        payload = {
            'type': 'corridor_projection',
            'sequence': self._sequence,
            'valid': bool(valid),
            'reason': str(reason),
            **fields,
        }
        self._publisher.publish(String(data=json.dumps(payload, separators=(',', ':'))))

    def _tick(self):
        now = time.monotonic()
        self._read_depth(now)
        if self._groups is None or now - self._joint_received_at > float(
            self.get_parameter('joint_stale_seconds').value
        ):
            self._publish(False, 'joint feedback stale')
            return
        if self._depth is None or now - self._depth_received_at > float(
            self.get_parameter('depth_stale_seconds').value
        ):
            self._publish(False, 'aligned depth stale')
            return
        if self._intrinsics is None:
            self._publish(False, 'camera intrinsics unavailable')
            return

        target = self._selected_twist(now)
        dt = float(np.clip(now - self._last_tick, 0.001, 0.10))
        self._last_tick = now
        tau = max(0.01, float(self.get_parameter('velocity_filter_seconds').value))
        alpha = 1.0 - math.exp(-dt / tau)
        self._filtered_twist += alpha * (target - self._filtered_twist)
        near = float(self.get_parameter('preview_near_m').value)
        far = max(near + 0.5, float(self.get_parameter('preview_far_m').value))
        count = int(np.clip(self.get_parameter('preview_samples').value, 12, 40))
        width = (
            float(self.get_parameter('robot_width_m').value)
            + 2.0 * float(self.get_parameter('side_margin_m').value)
        )
        distances = np.linspace(near, far, count)
        left_3d, centre_3d, right_3d = planar_corridor(
            *self._filtered_twist,
            width,
            distances,
            float(self.get_parameter('latency_compensation_seconds').value),
            math.radians(float(
                self.get_parameter('maximum_preview_heading_degrees').value
            )),
        )
        try:
            translation, rotation = self._camera_pose()
        except Exception as error:
            self._publish(False, f'dynamic camera FK failed: {error}')
            return
        left = project_points(left_3d, translation, rotation, self._intrinsics)
        centre = project_points(centre_3d, translation, rotation, self._intrinsics)
        right = project_points(right_3d, translation, rotation, self._intrinsics)
        (left_2d, centre_2d, right_2d), source_indices = self._normalized_triplets(
            left, centre, right
        )
        if len(centre_2d) < 3:
            self._publish(False, 'projected corridor is outside camera view')
            return

        clearance = float(self.get_parameter('obstacle_clearance_m').value)
        blocked_at = -1
        depth_samples = 0
        for visible_index, source_index in enumerate(source_indices):
            observed = self._depth_at(centre[source_index])
            if observed is None:
                continue
            depth_samples += 1
            expected = centre[source_index][2]
            if observed + clearance < expected:
                blocked_at = visible_index
                break
        self._publish(
            True,
            left=left_2d,
            centre=centre_2d,
            right=right_2d,
            blocked_at=blocked_at,
            # One valid median is sufficient when it already proves an
            # occlusion; otherwise several points are normally sampled.
            depth_valid=depth_samples >= 1,
            width_m=round(width, 3),
            depth_frame_id=self._depth_frame_id,
            filtered_twist=[round(float(value), 4) for value in self._filtered_twist],
        )

    def destroy_node(self):
        if self._depth_consumer is not None:
            try:
                self._depth_consumer.close()
            except Exception:
                pass
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = CorridorProjectionNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
