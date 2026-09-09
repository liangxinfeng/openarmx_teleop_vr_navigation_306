# 云蝶 VR遥操导航与多任务编排

307机器人当前版本快照（2026-09-09）。包名沿用 `openarmx_teleop_vr_navigation_306`，启动默认话题后缀为 `0_307`，部分雷达话题和路径也为307配置。

## 功能

- WebXR双臂遥操、底盘接管、头部/身体高度控制。
- RGB、透视与精确点云显示，地图、航点、定位及距离提示。
- 语音/触屏卡片任务编排、固定任务模板、VR确认与任务进度。
- 任务语音、桌面胸屏入口、监视页面和公众观看页。

## 依赖关系

本项目是集成层，**不是单独可运行的完整机器人镜像**：

| 项目 | 用途 |
| --- | --- |
| [openarmx_teleop_vr_306_v4](https://github.com/liangxinfeng/openarmx_teleop_vr_306_v4) | 手柄映射、IK、SYNC会话与硬件输出 |
| [autolife_robot_control_center](https://github.com/liangxinfeng/autolife_robot_control_center) | 导航、重定位、地图航点、共用语音与胸屏 |
| [autolife_s2_voice](https://github.com/liangxinfeng/autolife_s2_voice) | 语音链路及资源 |
| 原机外部环境 | 厂商SDK/服务、`robot_env`、`void-cog`和语音模型 |

## 安装与启动

在Ubuntu 24.04 / ROS 2 Jazzy中，将本项目和上述三个仓库分别放入 `~/ros2_ws/src`，先完成各项目的依赖配置。不要重复放置同名ROS子包。

```bash
cd ~/ros2_ws/src
git clone https://github.com/liangxinfeng/openarmx_teleop_vr_navigation_306.git
cd ~/ros2_ws
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --packages-up-to openarmx_teleop_vr_navigation_306
source install/setup.bash
ros2 launch openarmx_teleop_vr_navigation_306 full_vr_navigation.launch.py \
  topic_suffix:=0_307 dry_run:=true start_navigation_stack:=false
```

**注意：`dry_run`主要传给机械臂控制器，不等于整个系统的无运动仿真。** 初次检查应隔离真机控制域，不接管底盘或分发导航任务；关闭启动导航栈也不会停掉已存在的后台导航服务。

实际使用前核对地图、航点、关节及厂商服务配置，再按现场规程启用导航栈和真机模式。原机 `~/vr307nav` 及桌面快捷方式是仓库外部署入口；仓库里的 `scripts/start_vr_navigation_306.sh` 和桌面应用仅是相关启动组件，不能假设克隆后快捷指令已安装。

## 网页入口

| 用途 | 地址 |
| --- | --- |
| VR遥操导航 | `https://<机器人IP>:8445` |
| 平板/浏览器任务编排 | `https://<机器人IP>:8766` |
| 同步监视 | `https://<机器人IP>:8445/monitor` |
| 公众观看 | `https://<机器人IP>:8445/audience` |

头显、平板和机器人须网络互通，并信任本机HTTPS证书。不要将控制端口直接暴露到公网。

## 任务与导航配置

- `config/task_actions.json`：地点、任务、自然语言提示词、身体高度挡位和固定任务模板；界面按配置加载。
- `launch/full_vr_navigation.launch.py`：V4参数覆盖、编号、雷达话题、端口和导航启动选项。
- 地图、航点及最终导航精调参数属于依赖包 `control_center_navigation`，不是重复存放在本仓库中。
- `web/`：VR、监视与公众界面；`task_page.py`：触屏任务编排页面。

任务需VR端确认后执行，任务状态不同，A/B/X键的作用也不同，应以当前任务卡片提示为准。X+A为复位组合；底盘接管时X/Y用于升降，不能沿用普通画面切换含义。

## 交付范围

本次保留307现有逻辑，仅补充公开仓库说明及忽略规则，未触发导航或机械臂动作。当前仓库沿用原许可声明；外部SDK、模型、证书和服务账号不随仓库交付。

[原始说明（历史按键描述可能已变化）](docs/ORIGINAL_README.md)
