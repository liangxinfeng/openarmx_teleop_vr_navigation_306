#!/usr/bin/env python3
"""Native GTK shell for the VR teleoperation/navigation stack.

It embeds the existing chest-screen Logo page, owns the ROS launch process,
and safely stops the complete stack when the application window is closed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
from urllib.request import urlopen

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402


LOGO_URL = "http://127.0.0.1:8088/"
LOGO_ROOT = Path("/home/ubuntu/ros2_ws/src/autolife_robot_control_center/logo")
LOGO_SERVER = LOGO_ROOT / "logo_server.py"
DISPLAY_SCRIPT = LOGO_ROOT / "set_display_mode.sh"
LOGO_IMAGE = LOGO_ROOT / "yundie.webp"
LOGO_SERVICE = "autolife-logo-display.service"
MAIN_LOCK = Path(f"/run/user/{os.getuid()}/openarmx-vr-navigation-306.lock")
STATE_DIR = Path.home() / ".local/state/autolife-vr-navigation"


class StartupError(RuntimeError):
    pass


def command(arguments: list[str], *, timeout: float = 12.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )


def service_active() -> bool:
    result = command(["systemctl", "--user", "is-active", LOGO_SERVICE])
    return result.stdout.strip() == "active"


def port_available(host: str = "127.0.0.1", port: int = 8088) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def wait_for_logo(timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(LOGO_URL, timeout=0.35) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.10)
    return False


def check_main_stack_idle() -> None:
    MAIN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(MAIN_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StartupError(
                "VR导航程序已经在运行。请先关闭原来的终端或程序窗口。"
            ) from error
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def terminate_group(process: subprocess.Popen | None, graceful: float = 0.0) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGINT if graceful else signal.SIGTERM)
        process.wait(timeout=graceful if graceful else 4.0)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass


class VrNavigationDesktopApp:
    def __init__(self, robot_id: str, entry: Path):
        self.robot_id = robot_id
        self.entry = entry
        self.cancelled = threading.Event()
        self.startup_done = threading.Event()
        self.shutdown_started = False
        self.started_stack = False
        self.logo_service_was_active = False
        self.logo_service_stopped = False
        self.logo_service_restored = False
        self.display_changed = False
        self.logo_process: subprocess.Popen | None = None
        self.stack_process: subprocess.Popen | None = None
        self.log_handle = None
        self.lock_handle = None
        self._acquire_app_lock()
        self._build_window()
        threading.Thread(target=self._startup_worker, daemon=True).start()

    def _acquire_app_lock(self) -> None:
        lock_path = Path(
            f"/run/user/{os.getuid()}/autolife-vr-navigation-{self.robot_id}.desktop.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_handle = lock_path.open("a+")
        try:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StartupError("桌面程序已经打开，请勿重复启动。") from error

    def _build_window(self) -> None:
        self.window = Gtk.Window(title=f"云蝶 VR遥操导航 · {self.robot_id}")
        self.window.set_default_size(1024, 768)
        self.window.set_position(Gtk.WindowPosition.CENTER)
        self.window.connect("delete-event", self._on_close)
        self.window.connect("key-press-event", self._on_key)
        if LOGO_IMAGE.is_file():
            try:
                self.window.set_icon_from_file(str(LOGO_IMAGE))
            except GLib.Error:
                pass

        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            window, #splash { background: #05080d; }
            #status { color: #dbeeff; font-size: 18px; }
            #substatus { color: #7392a7; font-size: 13px; }
            .window-control {
                color: #d8efff; background: rgba(5, 14, 24, 0.58);
                border: 1px solid rgba(110, 205, 255, 0.25);
                border-radius: 20px; min-width: 38px; min-height: 38px;
                font-size: 20px; padding: 0;
            }
            .window-control:hover { background: rgba(23, 112, 155, 0.75); }
            #close-control:hover { background: rgba(174, 44, 61, 0.85); }
            """
        )
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.overlay = Gtk.Overlay()
        self.window.add(self.overlay)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(350)
        self.overlay.add(self.stack)

        splash = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        splash.set_name("splash")
        splash.set_valign(Gtk.Align.CENTER)
        splash.set_halign(Gtk.Align.CENTER)
        if LOGO_IMAGE.is_file():
            image = Gtk.Image.new_from_file(str(LOGO_IMAGE))
            image.set_pixel_size(300)
            splash.pack_start(image, False, False, 0)
        self.spinner = Gtk.Spinner()
        self.spinner.start()
        splash.pack_start(self.spinner, False, False, 0)
        self.status = Gtk.Label(label="正在启动 VR 遥操导航…")
        self.status.set_name("status")
        splash.pack_start(self.status, False, False, 0)
        self.substatus = Gtk.Label(label="程序将在本窗口中运行，不会打开外部浏览器")
        self.substatus.set_name("substatus")
        splash.pack_start(self.substatus, False, False, 0)
        self.stack.add_named(splash, "splash")

        self.webview = WebKit2.WebView()
        settings = self.webview.get_settings()
        settings.set_enable_javascript(True)
        settings.set_enable_page_cache(False)
        settings.set_enable_developer_extras(False)
        self.webview.connect("load-changed", self._on_web_load)
        self.stack.add_named(self.webview, "logo")

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        controls.set_halign(Gtk.Align.END)
        controls.set_valign(Gtk.Align.START)
        controls.set_margin_top(10)
        controls.set_margin_end(10)
        self.fullscreen_button = Gtk.Button(label="↙")
        self.fullscreen_button.get_style_context().add_class("window-control")
        self.fullscreen_button.set_tooltip_text("退出/进入全屏（F11 或 Esc）")
        self.fullscreen_button.connect("clicked", self._toggle_fullscreen)
        controls.pack_start(self.fullscreen_button, False, False, 0)
        close_button = Gtk.Button(label="×")
        close_button.set_name("close-control")
        close_button.get_style_context().add_class("window-control")
        close_button.set_tooltip_text("安全关闭完整功能包")
        close_button.connect("clicked", lambda _button: self._begin_shutdown())
        controls.pack_start(close_button, False, False, 0)
        self.overlay.add_overlay(controls)

        self.is_fullscreen = True
        self.window.connect("window-state-event", self._on_window_state)
        self.window.show_all()
        self.window.fullscreen()

    def _set_status(self, title: str, detail: str = "") -> bool:
        self.status.set_text(title)
        self.substatus.set_text(detail)
        self.stack.set_visible_child_name("splash")
        return False

    def _startup_worker(self) -> None:
        try:
            check_main_stack_idle()
            self.logo_service_was_active = service_active()
            if self.logo_service_was_active:
                result = command(
                    ["systemctl", "--user", "stop", LOGO_SERVICE], timeout=15.0
                )
                if result.returncode != 0:
                    raise StartupError(
                        "无法关闭原有 Logo 浏览器服务：" + result.stdout.strip()
                    )
                self.logo_service_stopped = True
                for _ in range(50):
                    if port_available():
                        break
                    time.sleep(0.10)
            if self.cancelled.is_set():
                return

            display = command([str(DISPLAY_SCRIPT), "portrait"], timeout=12.0)
            if display.returncode != 0:
                raise StartupError("竖屏切换失败：" + display.stdout.strip())
            self.display_changed = True
            if self.cancelled.is_set():
                return
            if not port_available():
                raise StartupError("端口 8088 已被其他程序占用，无法嵌入 Logo 页面。")

            STATE_DIR.mkdir(parents=True, exist_ok=True)
            logo_log = (STATE_DIR / f"logo-{self.robot_id}.log").open("ab", buffering=0)
            self.logo_process = subprocess.Popen(
                [
                    "/usr/bin/python3",
                    str(LOGO_SERVER),
                    "--host", "127.0.0.1",
                    "--port", "8088",
                    "--directory", str(LOGO_ROOT),
                ],
                stdin=subprocess.DEVNULL,
                stdout=logo_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            if not wait_for_logo():
                raise StartupError("Logo 页面服务启动超时，请查看 " + str(logo_log.name))
            if self.cancelled.is_set():
                return
            GLib.idle_add(self._show_logo)

            stack_log_path = STATE_DIR / f"desktop-{self.robot_id}.log"
            self.log_handle = stack_log_path.open("ab", buffering=0)
            self.log_handle.write(
                (f"\n===== desktop start {time.strftime('%F %T')} =====\n").encode()
            )
            environment = os.environ.copy()
            environment["AUTOLIFE_EMBEDDED_LOGO"] = "1"
            environment["PYTHONUNBUFFERED"] = "1"
            self.stack_process = subprocess.Popen(
                [str(self.entry)],
                stdin=subprocess.DEVNULL,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
            self.started_stack = True
            threading.Thread(target=self._watch_stack, daemon=True).start()
        except Exception as error:
            GLib.idle_add(
                self._set_status, "启动失败", str(error) or error.__class__.__name__
            )
            self._cleanup_startup_failure()
        finally:
            self.startup_done.set()

    def _show_logo(self) -> bool:
        self.webview.load_uri(LOGO_URL)
        self.stack.set_visible_child_name("logo")
        return False

    def _on_web_load(self, webview, load_event) -> None:
        if load_event != WebKit2.LoadEvent.FINISHED:
            return
        webview.run_javascript(
            """
            (() => {
              const button = document.getElementById('fullscreen-toggle');
              if (button) button.style.setProperty('display', 'none', 'important');
              document.documentElement.style.overflow = 'hidden';
              document.body.style.overflow = 'hidden';
            })();
            """,
            None,
            None,
            None,
        )

    def _watch_stack(self) -> None:
        process = self.stack_process
        if process is None:
            return
        return_code = process.wait()
        if not self.shutdown_started:
            log_path = STATE_DIR / f"desktop-{self.robot_id}.log"
            GLib.idle_add(
                self._set_status,
                "VR 遥操导航已退出",
                f"退出码 {return_code}；日志：{log_path}",
            )

    def _on_window_state(self, _window, event) -> bool:
        self.is_fullscreen = bool(event.new_window_state & Gdk.WindowState.FULLSCREEN)
        self.fullscreen_button.set_label("↙" if self.is_fullscreen else "⛶")
        return False

    def _toggle_fullscreen(self, _button=None) -> None:
        if self.is_fullscreen:
            self.window.unfullscreen()
        else:
            self.window.fullscreen()

    def _on_key(self, _window, event) -> bool:
        if event.keyval == Gdk.KEY_F11:
            self._toggle_fullscreen()
            return True
        if event.keyval == Gdk.KEY_Escape and self.is_fullscreen:
            self.window.unfullscreen()
            return True
        if (
            event.keyval in (Gdk.KEY_q, Gdk.KEY_Q)
            and event.state & Gdk.ModifierType.CONTROL_MASK
        ):
            self._begin_shutdown()
            return True
        return False

    def _on_close(self, _window, _event) -> bool:
        self._begin_shutdown()
        return True

    def _begin_shutdown(self) -> None:
        if self.shutdown_started:
            return
        self.shutdown_started = True
        self.cancelled.set()
        self._set_status(
            "正在安全关闭…", "正在停止 ROS 节点并恢复显示设置，请稍候"
        )
        threading.Thread(target=self._shutdown_worker, daemon=True).start()

    def _cleanup_startup_failure(self) -> None:
        terminate_group(self.stack_process, graceful=18.0)
        terminate_group(self.logo_process)
        if self.logo_service_stopped and not self.started_stack:
            restored = command(
                ["systemctl", "--user", "start", LOGO_SERVICE], timeout=15.0
            )
            self.logo_service_restored = restored.returncode == 0
        elif self.display_changed and not self.started_stack:
            command([str(DISPLAY_SCRIPT), "landscape"], timeout=12.0)

    def _shutdown_worker(self) -> None:
        self.startup_done.wait(timeout=20.0)
        terminate_group(self.stack_process, graceful=25.0)
        terminate_group(self.logo_process)
        if self.display_changed and not self.logo_service_restored:
            command([str(DISPLAY_SCRIPT), "landscape"], timeout=12.0)
        if self.log_handle is not None:
            self.log_handle.close()
        GLib.idle_add(Gtk.main_quit)


def validate(robot_id: str, entry: Path) -> int:
    paths = {
        "entry": entry,
        "logo_server": LOGO_SERVER,
        "display_script": DISPLAY_SCRIPT,
        "logo_image": LOGO_IMAGE,
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    print(
        json.dumps(
            {
                "success": not missing,
                "robot_id": robot_id,
                "webkit": "WebKit2 4.1",
                "entry": str(entry),
                "missing": missing,
            },
            ensure_ascii=False,
        )
    )
    return 0 if not missing else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-id", required=True)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if arguments.check:
        return validate(arguments.robot_id, arguments.entry)

    try:
        application = VrNavigationDesktopApp(arguments.robot_id, arguments.entry)
    except Exception as error:
        dialog = Gtk.MessageDialog(
            transient_for=None,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="VR 遥操导航无法启动",
        )
        dialog.format_secondary_text(str(error))
        dialog.run()
        dialog.destroy()
        return 2

    signal.signal(
        signal.SIGINT, lambda *_args: GLib.idle_add(application._begin_shutdown)
    )
    signal.signal(
        signal.SIGTERM, lambda *_args: GLib.idle_add(application._begin_shutdown)
    )
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
