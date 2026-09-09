"""Route the normal navigation voice only when showroom mode is disabled."""

from __future__ import annotations

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import Bool, String

from text_voice_control.command_parser import parse_voice_command


def should_forward_control_center_voice(showroom_mode_enabled: bool) -> bool:
    """The control-center and showroom speech paths must be mutually exclusive."""
    return not bool(showroom_mode_enabled)


class VoiceModeRouter(Node):
    """Forward control-center prompts to the vendor TTS outside showroom mode."""

    def __init__(self) -> None:
        super().__init__('openarmx_navigation_voice_mode_router_306')
        self.declare_parameter('input_topic', '/text_voice_command')
        self.declare_parameter('tts_topic', '/topic_tts_0_307')
        self.declare_parameter(
            'showroom_mode_topic',
            '/openarmx_teleop_vr_navigation_306/showroom_mode',
        )

        self._showroom_mode_enabled = False
        self._tts_publisher = self.create_publisher(
            String, str(self.get_parameter('tts_topic').value), 10
        )
        mode_qos = QoSProfile(depth=1)
        mode_qos.reliability = ReliabilityPolicy.RELIABLE
        mode_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.create_subscription(
            Bool,
            str(self.get_parameter('showroom_mode_topic').value),
            self._on_showroom_mode,
            mode_qos,
        )
        self.create_subscription(
            String,
            str(self.get_parameter('input_topic').value),
            self._on_control_center_voice,
            10,
        )
        self.get_logger().info(
            'Voice mode router ready: showroom off=control-center voice, '
            'showroom on=task/showroom voice'
        )

    def _on_showroom_mode(self, message: Bool) -> None:
        enabled = bool(message.data)
        if enabled == self._showroom_mode_enabled:
            return
        self._showroom_mode_enabled = enabled
        active_voice = 'task/showroom' if enabled else 'control-center navigation'
        self.get_logger().info(f'Active navigation voice: {active_voice}')

    def _on_control_center_voice(self, message: String) -> None:
        if not should_forward_control_center_voice(self._showroom_mode_enabled):
            return
        command = parse_voice_command(message.data)
        if command is None:
            self.get_logger().warning('Ignored empty or invalid navigation voice command')
            return
        output = String()
        output.data = command.to_tts_payload(use_json=True)
        self._tts_publisher.publish(output)


def main(args=None) -> None:
    rclpy.init(args=args)
    node = VoiceModeRouter()
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
