"""On-demand latest-frame depth stream for the VR point-cloud view."""

from collections import deque
import threading
import time


class DepthCloudStream:
    """Read aligned head depth only while at least one VR client needs it.

    Frames are decimated before leaving the robot and only the newest sample is
    retained.  This keeps the optional view isolated from RGB WebRTC and from
    the latency-sensitive teleoperation data channel.
    """

    def __init__(
            self, *, stride=5, maximum_fps=15.0, depth_scale_m=0.001,
            module_name='mod_camera_rgbd_head', output_name='depth',
            sample_count=0, include_color=False, color_output_name='color'):
        self.stride = max(2, min(12, int(stride)))
        self.maximum_fps = max(1.0, min(30.0, float(maximum_fps)))
        self.depth_scale_m = float(depth_scale_m)
        self.module_name = str(module_name)
        self.output_name = str(output_name)
        self.sample_count = max(0, min(120000, int(sample_count)))
        self.include_color = bool(include_color)
        self.color_output_name = str(color_output_name)
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._consumer = None
        self._color_consumer = None
        self._sample_u = None
        self._sample_v = None
        self._demand = 0
        self._payload = None
        self._sequence = -1
        self._source_frame_id = -1
        self._configuration = None
        self._last_frame_time = 0.0
        self._frame_times = deque(maxlen=30)
        self._last_error = ''
        self._recovery_count = 0

    def acquire(self):
        with self._condition:
            self._demand += 1
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    name='vr-depth-cloud-latest-frame',
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()

    def release(self):
        with self._condition:
            self._demand = max(0, self._demand - 1)
            self._condition.notify_all()

    def close(self):
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._close_consumers()

    def snapshot(self, after_sequence=-1, timeout=1.0):
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while (
                    not self._stop.is_set()
                    and self._sequence <= int(after_sequence)
                    and time.monotonic() < deadline):
                self._condition.wait(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            configuration = self._configuration
            return (
                self._payload,
                self._sequence,
                None if configuration is None else dict(configuration),
            )

    def configuration(self):
        with self._condition:
            value = self._configuration
            return None if value is None else dict(value)

    def status(self):
        with self._condition:
            times = tuple(self._frame_times)
            age = (
                None if self._last_frame_time <= 0.0
                else max(0.0, time.monotonic() - self._last_frame_time)
            )
            sequence = self._sequence
            demand = self._demand
            error = self._last_error
            recovery_count = self._recovery_count
        fps = 0.0
        if len(times) >= 2 and times[-1] > times[0]:
            fps = (len(times) - 1) / (times[-1] - times[0])
        return {
            'online': bool(sequence >= 0 and age is not None and age < 1.0),
            'fps': round(fps, 1),
            'frame_age': None if age is None else round(age, 3),
            'clients': int(demand),
            'last_error': error,
            'recoveries': int(recovery_count),
            'thread_alive': bool(
                self._thread is not None and self._thread.is_alive()
            ),
        }

    @staticmethod
    def _stable_uniform_uv(width, height, sample_count, np):
        """Build the stable full-frame UV set used by official-style mode.

        The native Autolife bridge keeps one stable UV specification and sends
        depth/colour values for those positions.  Its robot_v2_2 defaults use
        18,000 samples and an almost-flat foveation curve.  A regular grid with
        evenly discarded excess cells reproduces that configuration without
        shipping a large UV table to every browser.
        """
        columns = max(2, int(round((sample_count * width / height) ** 0.5)))
        rows = max(2, int((sample_count + columns - 1) // columns))
        grid_count = columns * rows
        selected = np.floor(
            np.linspace(0, grid_count - 1, sample_count) + 0.5
        ).astype(np.int64)
        grid_u = selected % columns
        grid_v = selected // columns
        sample_u = np.floor(
            grid_u * (width - 1) / max(1, columns - 1) + 0.5
        ).astype(np.intp)
        sample_v = np.floor(
            grid_v * (height - 1) / max(1, rows - 1) + 0.5
        ).astype(np.intp)
        return sample_u, sample_v, columns, rows

    def _open_consumers(self):
        import numpy as np

        from autolife_robot_sdk.utils.camera_shm_catalog import (
            get_camera_shm_output,
            open_camera_shm_consumer,
        )

        output = get_camera_shm_output(self.module_name, self.output_name)
        consumer = open_camera_shm_consumer(
            output,
            name=(
                'openarmx_navigation_precision_cloud_depth'
                if self.include_color else 'openarmx_navigation_depth_cloud'
            ),
        )
        intrinsics = consumer.get_intrinsics()
        if not isinstance(intrinsics, dict):
            consumer.close()
            raise RuntimeError('RGB-D depth intrinsics are unavailable')
        width = int(output.width)
        height = int(output.height)
        offset = self.stride // 2
        columns = len(range(offset, width, self.stride))
        rows = len(range(offset, height, self.stride))
        sample_u = None
        sample_v = None
        sample_count = columns * rows
        sampling = 'regular_stride'
        grid_columns = columns
        grid_rows = rows
        if self.sample_count > 0:
            sample_count = self.sample_count
            sample_u, sample_v, grid_columns, grid_rows = self._stable_uniform_uv(
                width, height, sample_count, np
            )
            columns = sample_count
            rows = 1
            sampling = 'stable_uniform_uv'

        color_consumer = None
        if self.include_color:
            color_output = get_camera_shm_output(
                self.module_name, self.color_output_name
            )
            if (
                    int(color_output.width) != width
                    or int(color_output.height) != height):
                consumer.close()
                raise RuntimeError('RGB and depth shared-memory geometry differs')
            color_consumer = open_camera_shm_consumer(
                color_output, name='openarmx_navigation_precision_cloud_color'
            )
            color_intrinsics = color_consumer.get_intrinsics()
            for key in ('fx', 'fy', 'ppx', 'ppy'):
                if abs(float(color_intrinsics[key]) - float(intrinsics[key])) > 1e-3:
                    color_consumer.close()
                    consumer.close()
                    raise RuntimeError('RGB and depth camera intrinsics are not aligned')
        configuration = {
            'type': 'depth_cloud_config',
            'source_width': width,
            'source_height': height,
            'columns': columns,
            'rows': rows,
            'sample_count': sample_count,
            'sampling': sampling,
            'grid_columns': grid_columns,
            'grid_rows': grid_rows,
            'stride': self.stride,
            'offset': offset,
            'fx': float(intrinsics['fx']),
            'fy': float(intrinsics['fy']),
            'ppx': float(intrinsics['ppx']),
            'ppy': float(intrinsics['ppy']),
            'depth_scale_m': self.depth_scale_m,
            'maximum_fps': self.maximum_fps,
            'color_enabled': self.include_color,
            'payload_format': (
                'depth16_rgb565_planes' if self.include_color else 'depth16'
            ),
        }
        with self._condition:
            self._consumer = consumer
            self._color_consumer = color_consumer
            self._sample_u = sample_u
            self._sample_v = sample_v
            self._configuration = configuration
            self._condition.notify_all()

    def _close_consumers(self):
        consumers = (self._consumer, self._color_consumer)
        self._consumer = None
        self._color_consumer = None
        self._sample_u = None
        self._sample_v = None
        for consumer in consumers:
            if consumer is None:
                continue
            try:
                consumer.close()
            except Exception:
                pass

    def _set_error(self, error):
        with self._condition:
            self._last_error = str(error)
            self._condition.notify_all()

    def _run(self):
        import numpy as np

        minimum_interval = 1.0 / self.maximum_fps
        next_frame_time = 0.0
        last_output_time = time.monotonic()
        while not self._stop.is_set():
            with self._condition:
                demand = self._demand
            if self._stop.is_set():
                break
            if demand <= 0:
                self._close_consumers()
                next_frame_time = 0.0
                with self._condition:
                    if self._demand <= 0 and not self._stop.is_set():
                        self._condition.wait(timeout=0.5)
                continue
            try:
                delay = next_frame_time - time.monotonic()
                if delay > 0.0:
                    self._stop.wait(delay)
                    continue
                if self._consumer is None:
                    self._open_consumers()
                    last_output_time = time.monotonic()
                item = self._consumer.get_latest(
                    nonblock=True, with_meta=True
                )
                if item is None:
                    if time.monotonic() - last_output_time > 1.0:
                        raise RuntimeError(
                            'depth input stalled; reopening SHM consumers'
                        )
                    self._stop.wait(0.002)
                    continue
                depth, source_frame_id, _meta = item
                if (
                        depth.ndim != 2
                        or depth.dtype != np.uint16
                        or self._configuration is None):
                    raise RuntimeError(
                        f'unexpected depth frame {depth.shape}/{depth.dtype}'
                    )
                if int(source_frame_id) == self._source_frame_id:
                    continue
                color = None
                if self.include_color:
                    color_item = self._color_consumer.get_latest(
                        nonblock=True, with_meta=True
                    )
                    if color_item is None:
                        if time.monotonic() - last_output_time > 1.0:
                            raise RuntimeError(
                                'colour input stalled; reopening SHM consumers'
                            )
                        self._stop.wait(0.002)
                        continue
                    color, color_frame_id, _color_meta = color_item
                    # The head RGB-D producer publishes aligned colour and
                    # depth with the same frame id. Never combine two moments:
                    # a mismatched pair would put colour on the wrong object.
                    if int(color_frame_id) != int(source_frame_id):
                        if time.monotonic() - last_output_time > 1.0:
                            raise RuntimeError(
                                'RGB-D pairing stalled; reopening SHM consumers'
                            )
                        continue
                    if color.ndim != 3 or color.shape[2] != 3:
                        raise RuntimeError(
                            f'unexpected colour frame {color.shape}/{color.dtype}'
                        )
                now = time.monotonic()
                next_frame_time = now + minimum_interval
                offset = self.stride // 2
                if self._sample_u is None:
                    sampled = np.ascontiguousarray(
                        depth[offset::self.stride, offset::self.stride]
                    )
                else:
                    sampled = np.ascontiguousarray(
                        depth[self._sample_v, self._sample_u]
                    ).reshape(1, -1)
                expected = self._configuration
                if sampled.shape != (expected['rows'], expected['columns']):
                    raise RuntimeError('depth frame geometry changed at runtime')
                payload = sampled.astype('<u2', copy=False).tobytes(order='C')
                if color is not None:
                    if self._sample_u is None:
                        sampled_color = np.ascontiguousarray(
                            color[
                                offset::self.stride,
                                offset::self.stride,
                                :,
                            ]
                        ).reshape(-1, 3)
                    else:
                        sampled_color = np.ascontiguousarray(
                            color[self._sample_v, self._sample_u, :]
                        ).reshape(-1, 3)
                    # Shared memory is BGR. Match the official compact RGB565
                    # payload so 18k coloured points stay near 72 kB/frame.
                    blue = sampled_color[:, 0].astype(np.uint16)
                    green = sampled_color[:, 1].astype(np.uint16)
                    red = sampled_color[:, 2].astype(np.uint16)
                    rgb565 = (
                        ((red >> 3) << 11)
                        | ((green >> 2) << 5)
                        | (blue >> 3)
                    ).astype('<u2', copy=False)
                    payload += rgb565.tobytes(order='C')
                with self._condition:
                    self._source_frame_id = int(source_frame_id)
                    self._sequence = (self._sequence + 1) & 0xFFFFFFFF
                    self._payload = payload
                    self._last_frame_time = now
                    self._frame_times.append(now)
                    self._last_error = ''
                    self._condition.notify_all()
                last_output_time = now
            except Exception as error:
                self._set_error(error)
                with self._condition:
                    self._recovery_count += 1
                self._close_consumers()
                self._source_frame_id = -1
                last_output_time = time.monotonic()
                self._stop.wait(0.1)
