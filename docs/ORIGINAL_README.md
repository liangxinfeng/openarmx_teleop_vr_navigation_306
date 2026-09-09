# openarmx_teleop_vr_navigation_306（原始说明，部分历史内容）

306机器人的WebXR双臂遥操、RGB画面、底盘遥控、地图/航点导航和语音任务分发一体包。

## 启动

```bash
~/vr306nav
```

- VR遥操/导航：`https://<机器人IP>:8445`
- 网页语音任务分发：`https://<机器人IP>:8766`

浏览器端说出连续任务后，任务先进入VR待确认状态，不会立即运动。VR中按A确认分发后自动前往第一个地点；抵达并完成动作后，长按A一秒并松开，系统才确认完成并推进下一项。按住A期间只要按到X，本次完成确认立即取消，避免与X+A复位冲突。

VR快捷键：底盘未接管时按Y切换“RGB黑底/头显透视”，地图、菜单和任务提示保持可见；按B切换“头部跟随/头部锁定”。底盘接管时Y仍用于身体上升，不触发画面切换。

任务能力和每项任务的身体高度挡位统一配置在：

```text
config/task_actions.json
```

每个任务使用 `location + task + hints` 配置，例如：

```json
"前台区": [{
  "task": "拿水",
  "body_height_level": 5,
  "hints": ["拿水", "拿一瓶水", "给我弄瓶水", "我渴了"]
}]
```

`hints` 是用户可能说出的自然表达。运行期间增删地点、任务或提示词后，下一次语音/卡片任务会自动重新读取配置，不需要修改语音代码或重新编译。提示词应尽量使用有区分度的短语，避免只写“拿”“放”“去”等过于宽泛的单字。

高度挡位为1～5档（最低到最高），当前全部默认5档。导航出发、抵达、动作开始、动作完成均由包内任务语音节点调用本地TTS和机器人喇叭播报。

直接ROS启动（默认dry-run）示例：

```bash
source /opt/ros/jazzy/setup.bash
source '/home/ubuntu/ros2_ws/install/setup.bash'
ros2 launch openarmx_teleop_vr_navigation_306 full_vr_navigation.launch.py dry_run:=true
```
