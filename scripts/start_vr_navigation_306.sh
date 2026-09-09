#!/usr/bin/env bash
set -Eeuo pipefail

WORKSPACE_SETUP='/home/ubuntu/ros2_ws/install/setup.bash'
GV_SERVICE='gv-slam-service.service'
LOCK_FILE="/run/user/${UID}/openarmx-vr-navigation-306.lock"
CHILD_PID=''
GV_WAS_ACTIVE=0
GV_STOPPED_BY_US=0
GV_RESTORE_ALLOWED=1
CLEANED=0
CHILD_PGID=''
LOGO_SERVICE='autolife-logo-display.service'
LOGO_WAS_ACTIVE=0
LOGO_STARTED_BY_US=0
EMBEDDED_LOGO="${AUTOLIFE_EMBEDDED_LOGO:-0}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

stop_child() {
  [ -n "$CHILD_PID" ] || return 0
  if [ -n "$CHILD_PGID" ]; then
    kill -INT -- "-$CHILD_PGID" 2>/dev/null || true
  else
    kill -INT "$CHILD_PID" 2>/dev/null || true
  fi
  for _ in $(seq 1 100); do
    if [ -n "$CHILD_PGID" ]; then
      ps -eo pgid=,stat= | awk -v group="$CHILD_PGID" '$1 == group && $2 !~ /^Z/ {found=1} END {exit !found}' || break
    else
      kill -0 "$CHILD_PID" 2>/dev/null || break
    fi
    sleep 0.1
  done
  if [ -n "$CHILD_PGID" ] && ps -eo pgid=,stat= | awk -v group="$CHILD_PGID" '$1 == group && $2 !~ /^Z/ {found=1} END {exit !found}'; then
    kill -TERM -- "-$CHILD_PGID" 2>/dev/null || true
  elif [ -z "$CHILD_PGID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
    kill -TERM "$CHILD_PID" 2>/dev/null || true
  fi
  if [ -n "$CHILD_PGID" ]; then
    for _ in $(seq 1 50); do
      ps -eo pgid=,stat= | awk -v group="$CHILD_PGID" '$1 == group && $2 !~ /^Z/ {found=1} END {exit !found}' || break
      sleep 0.1
    done
  fi
  wait "$CHILD_PID" 2>/dev/null || true
}

cleanup() {
  [ "$CLEANED" -eq 0 ] || return 0
  CLEANED=1
  trap '' INT TERM HUP
  stop_child
  if [ "$LOGO_STARTED_BY_US" -eq 1 ]; then
    printf '\n正在关闭本次启动的 Logo 胸屏网页…\n'
    systemctl --user stop "$LOGO_SERVICE" 2>/dev/null || true
  fi
  if [ "$GV_WAS_ACTIVE" -eq 1 ] && [ "$GV_STOPPED_BY_US" -eq 1 ] && [ "$GV_RESTORE_ALLOWED" -eq 1 ]; then
    printf '\n正在恢复厂商 SLAM/定位服务…\n'
    if systemctl --user start "$GV_SERVICE"; then
      for _ in $(seq 1 100); do
        [ "$(systemctl --user is-active "$GV_SERVICE" 2>/dev/null || true)" = 'active' ] && break
        sleep 0.1
      done
      if [ "$(systemctl --user is-active "$GV_SERVICE" 2>/dev/null || true)" = 'active' ]; then
        green '厂商 SLAM/定位服务已恢复。'
      else
        red "厂商定位服务未能在等待时间内恢复，请执行：systemctl --user start $GV_SERVICE"
      fi
    else
      red "厂商定位服务恢复失败，请执行：systemctl --user start $GV_SERVICE"
    fi
  fi
}

trap cleanup EXIT
trap 'exit 130' INT TERM HUP

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  red 'VR导航程序已经在运行，请不要重复启动。'
  exit 2
fi

if [ ! -r "$WORKSPACE_SETUP" ]; then
  red "找不到 ROS 工作区：$WORKSPACE_SETUP"
  exit 2
fi

set +u
source /opt/ros/jazzy/setup.bash
source "$WORKSPACE_SETUP"
set -u
export ROS_DOMAIN_ID=0
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="lo"/></Interfaces></General><Discovery><ParticipantIndex>auto</ParticipantIndex><MaxAutoParticipantIndex>200</MaxAutoParticipantIndex></Discovery></Domain></CycloneDDS>'

if ! ros2 pkg prefix openarmx_teleop_vr_navigation_306 >/dev/null 2>&1; then
  red '未找到 openarmx_teleop_vr_navigation_306，请先编译功能包。'
  exit 2
fi

if pgrep -af 'openarmx_306_v[0-9]+_(arm_controller|mapper)|openarmx_teleop_vr_306_v[0-9]+\.(controller_node|vr_mapper_node|vr_web_bridge)|openarmx_teleop_vr_navigation_306\.navigation_web_bridge' >/tmp/vr306nav-existing.txt; then
  red '检测到另一套306遥操正在运行。请先在原终端按 Ctrl+C 退出：'
  cat /tmp/vr306nav-existing.txt
  exit 3
fi

START_NAVIGATION_STACK=true
GV_STATE="$(systemctl --user is-active "$GV_SERVICE" 2>/dev/null || true)"
if [ "$GV_STATE" = 'active' ]; then
  GV_WAS_ACTIVE=1
  printf '[1/2] 正在暂停厂商 SLAM/定位，避免两套 map → odom 冲突…\n'
  systemctl --user stop "$GV_SERVICE"
  GV_STOPPED_BY_US=1
  for _ in $(seq 1 150); do
    GV_STATE="$(systemctl --user is-active "$GV_SERVICE" 2>/dev/null || true)"
    [ "$GV_STATE" = 'inactive' ] || [ "$GV_STATE" = 'failed' ] || { sleep 0.1; continue; }
    break
  done
  GV_STATE="$(systemctl --user is-active "$GV_SERVICE" 2>/dev/null || true)"
  if [ "$GV_STATE" != 'inactive' ] && [ "$GV_STATE" != 'failed' ]; then
    red "无法安全停止 $GV_SERVICE（当前：$GV_STATE），本次未启动导航。"
    exit 4
  fi
elif [ "$GV_STATE" != 'inactive' ] && [ "$GV_STATE" != 'failed' ]; then
  red "无法确认 $GV_SERVICE 状态（当前：${GV_STATE:-unknown}），为避免定位冲突已中止。"
  exit 4
fi

# Only reuse a remaining AMCL when it belongs to a complete control-center
# navigation stack. A vendor AMCL by itself has no map-specific waypoint API.
if pgrep -af '/nav2_amcl/amcl|nav2_amcl.*--ros-args' >/tmp/vr306nav-amcl.txt; then
  SERVICE_LIST="$(timeout 4 ros2 service list 2>/dev/null || true)"
  if grep -qx '/list_waypoints' <<<"$SERVICE_LIST" \
      && grep -qx '/navigate_to_waypoint' <<<"$SERVICE_LIST" \
      && grep -qx '/cartographer_global_relocalization/relocalize' <<<"$SERVICE_LIST"; then
    START_NAVIGATION_STACK=false
    GV_RESTORE_ALLOWED=0
    printf '检测到完整的控制中心导航，复用当前地图、航点和全局重定位。\n'
  else
    GV_RESTORE_ALLOWED=0
    red '停止厂商定位后仍检测到不完整的外部AMCL。为避免TF冲突，本次未启动。'
    cat /tmp/vr306nav-amcl.txt
    exit 4
  fi
fi

green '安全检查通过：'
ROBOT_WEB_IP="$(ip -4 -o addr show scope global | awk '
  $4 ~ /^192\.168\.8\./ {sub(/\/.*/, "", $4); print $4; exit}
' || true)"
if [ -z "$ROBOT_WEB_IP" ]; then
  ROBOT_WEB_IP="$(hostname -I | awk '{print $1}')"
fi
printf '  机械臂：复用现有自研遥操核心，网页内仍需长按1秒使能\n'
printf '  底盘：左摇杆按键作为行驶确认，200ms数据超时立即停车\n'
printf '  导航：%s\n' "$([ "$START_NAVIGATION_STACK" = true ] && echo '启动当前地图、AMCL、航点和全局重定位' || echo '复用已经运行的控制中心导航')"
printf '  VR遥操与导航：https://%s:8445\n' "$ROBOT_WEB_IP"
printf '  语音任务分发：https://%s:8766\n' "$ROBOT_WEB_IP"
printf '按 Ctrl+C 退出；若本脚本暂停了厂商定位，退出后会自动恢复。\n\n'

setsid ros2 launch openarmx_teleop_vr_navigation_306 full_vr_navigation.launch.py \
  dry_run:=false \
  start_navigation_stack:="$START_NAVIGATION_STACK" &
CHILD_PID=$!
CHILD_PGID="$(ps -o pgid= -p "$CHILD_PID" | tr -d ' ')"
if ! [[ "$CHILD_PGID" =~ ^[0-9]+$ ]] || [ "$CHILD_PGID" -le 1 ]; then
  red '无法建立独立ROS进程组，已中止。'
  exit 5
fi
if [ "$EMBEDDED_LOGO" = '1' ]; then
  printf '  Logo胸屏：由桌面程序窗口托管（不启动外部浏览器）\n'
else
  LOGO_STATE="$(systemctl --user is-active "$LOGO_SERVICE" 2>/dev/null || true)"
  if [ "$LOGO_STATE" = 'active' ]; then
    LOGO_WAS_ACTIVE=1
    printf '  Logo胸屏：复用已打开的全屏页面\n'
  else
    printf '[2/2] 正在打开 Logo 胸屏全屏页面并接入任务状态…\n'
    if systemctl --user start "$LOGO_SERVICE"; then
      LOGO_STARTED_BY_US=1
    else
      red 'Logo 胸屏网页启动失败；遥操导航仍继续运行。'
    fi
  fi
fi
wait "$CHILD_PID"
