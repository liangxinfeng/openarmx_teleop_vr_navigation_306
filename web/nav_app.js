/* Lightweight VR navigation surface layered on the proven teleop client. */

const NAVIGATION_ASSET_VERSION = 'yundie-306-navigation-50-map-70-v47';

const navUi = {
  enabled: false,
  sessionActive: false,
  visible: false,
  menuVisible: false,
  websocket: null,
  driveWorker: null,
  driveLinkState: 'disconnected',
  driveInputWasActive: false,
  reconnectTimer: null,
  state: null,
  selected: 0,
  items: [],
  mapVersion: -1,
  mapBitmap: null,
  canvas: null,
  context: null,
  texture: null,
  textureBound: false,
  panel: null,
  videoPanel: null,
  backdropPanel: null,
  depthRoot: null,
  depthPoints: null,
  depthGeometry: null,
  depthPositionAttribute: null,
  depthColorAttribute: null,
  depthPositions: null,
  depthColors: null,
  depthRaysX: null,
  depthRaysY: null,
  depthGeometryFormat: '',
  depthCapacity: 0,
  depthValidPoints: 0,
  depthBackground: null,
  depthSocket: null,
  depthReconnectTimer: null,
  depthGeneration: 0,
  depthConfig: null,
  depthFrameCount: 0,
  depthLastFrameAt: 0,
  depthLastPacketAt: 0,
  depthLastError: '',
  depthOnline: false,
  depthConnectedAt: 0,
  depthWatchdogAt: 0,
  precisionDepthEnabled: false,
  depthPendingFrame: null,
  depthRenderScheduled: false,
  depthMinimumM: 0.20,
  depthMaximumM: 5.0,
  videoTexture: null,
  corridor: null,
  corridorReceivedAt: 0,
  corridorCanvas: null,
  corridorContext: null,
  corridorTexture: null,
  corridorPanel: null,
  corridorRenderKey: '',
  pollTimer: null,
  pollPending: false,
  actionPending: false,
  gripperForceRatio: 1.0,
  gripperForceMinimumRatio: 0.2,
  gripperForceMaximumCurrent: 10.0,
  gripperForceStep: 0.1,
  gripperForceRequestId: 0,
  forceAdjustLatch: false,
  bodyHeightEngaged: false,
  quickResetHeightSuppressed: false,
  cameraFrameTimes: [],
  namingActive: false,
  waypointNameDraft: '',
  waypointNameTouched: false,
  nameKeyIndex: 0,
  nameSelectionLatch: false,
  baseControlLatched: false,
  deleteMode: false,
  deleteConfirmName: null,
  navigateConfirmName: null,
  mapMode: false,
  mapConfirmPath: null,
  mapConfirmName: null,
  templateMode: false,
  templateConfirmId: null,
  templateConfirmName: null,
  showroomModeEnabled: false,
  manualRelocalization: null,
  manualRelocalizationMoveSpeed: 1.80,
  relocalizationAlertKey: '',
  taskVersion: -1,
  taskDecisionKey: '',
  taskAcceptArmed: false,
  taskAcceptPending: false,
  taskHoldStartedAt: 0,
  taskHoldMode: '',
  taskCompleteLatched: false,
  taskHoldCancelled: false,
  taskSkipHoldStartedAt: 0,
  taskSkipLatched: false,
  taskSkipCancelled: false,
  taskRejectHoldStartedAt: 0,
  taskRejectLatched: false,
  taskRejectCancelled: false,
  cameraMode: 'rgbd',
  monitorViewKey: '',
  monitorViewLastSentAt: 0,
  lastDriveSentAt: 0,
  previous: { y: false, a: false, b: false, x: false, leftStick: false, rightStick: false },
  selectionLatch: false,
  statusLine: '正在等待导航数据…'
};

// Camera-local metres. Moving every coplanar HUD layer together preserves
// pixel-perfect RGB/depth/map alignment while lowering the viewing angle.
const NAV_PANEL_VERTICAL_OFFSET = -0.10;
const MAP_DISPLAY_SCALE = 0.70;

const WAYPOINT_NAME_KEYS = [
  ...['前台', '吧台', '客房', '洗衣房', '充电桩', '门口',
    '厨房', '餐厅', '走廊', '电梯', '房间', '大厅'].map(value => ({ label: value, value })),
  ...'0123456789_-'.split('').map(value => ({ label: value, value })),
  ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('').map(value => ({ label: value, value })),
  { label: '退格', action: 'backspace' },
  { label: '清空', action: 'clear' },
  { label: '取消', action: 'cancel' },
  { label: '保存', action: 'save' }
];
const WAYPOINT_NAME_COLUMNS = 6;

function navShowNotice(message, duration = 2800, color = '#68E1FF') {
  if (typeof showVrNotice === 'function') {
    showVrNotice(message, duration, color);
  }
}

function scheduleNavigationVersionCheck(serverVersion) {
  // Older deployments used descriptive names unrelated to the web asset.
  // Once both sides expose the same yundie asset token, a later package
  // update can reload a long-lived Quest tab automatically.
  const remoteVersion = String(serverVersion || '');
  if (!remoteVersion.startsWith('yundie-306-navigation-')) return;
  const revision = value => Number(String(value).match(/-v(\d+)$/)?.[1] || 0);
  // Static assets can be deployed while the running ROS bridge still reports
  // the previous revision. Reload only when the server is actually newer.
  if (revision(remoteVersion) > revision(NAVIGATION_ASSET_VERSION)) {
    window.location.reload();
  }
}

async function checkNavigationAssetVersion() {
  try {
    const config = await fetch('/api/config', { cache: 'no-store' })
      .then(response => response.json());
    scheduleNavigationVersionCheck(config.navigation?.ui_version);
  } catch (_error) {
    // Version checks are advisory and must never disturb teleoperation.
  }
}

function toggleHeadMotionLock() {
  const control = globalThis.autolifeHeadFollowControl;
  if (!control || typeof control.enabled !== 'function'
      || typeof control.setEnabled !== 'function') {
    navShowNotice('头部锁定接口暂不可用', 2200, '#ffcf70');
    return;
  }
  if (typeof control.canToggle === 'function' && !control.canToggle()) {
    navShowNotice('请先完成真机使能或复位，再按 B 切换头部', 2600, '#ffcf70');
    return;
  }
  const enableFollowing = !control.enabled();
  navShowNotice(
    enableFollowing ? '正在解除头部锁定…' : '正在锁定机器人头部…',
    1400,
    enableFollowing ? '#72f0b1' : '#ffcf70'
  );
  void control.setEnabled(enableFollowing);
}

function isPointCloudMode(mode = navUi.cameraMode) {
  return mode === 'precision_cloud';
}

function depthCloudIsReady(now = performance.now()) {
  void now;
  // Once a valid cloud exists, retain it while the transport reconnects. A
  // short network or render pause must not expose the ordinary RGB panel.
  return navUi.depthValidPoints > 0 && navUi.depthLastFrameAt > 0;
}

function applyDepthCloudDisplayState(now = performance.now()) {
  const selected = navUi.visible && isPointCloudMode();
  const ready = selected && depthCloudIsReady(now);
  if (selected) attachDepthPointCloudToCamera();
  if (navUi.depthRoot) navUi.depthRoot.visible = ready;
  // RGB is only a first-frame fail-safe. After the first valid XYZ frame the
  // last cloud remains visible during reconnect and is replaced atomically by
  // the next live frame.
  const rgbFallback = selected && !ready;
  navUi.videoPanel?.setAttribute('visible', rgbFallback ? 'true' : 'false');
  navUi.backdropPanel?.setAttribute('visible', rgbFallback ? 'true' : 'false');
}

function setNavigationView(visible, notify = true) {
  const wasVisible = navUi.visible;
  navUi.visible = Boolean(visible && navUi.sessionActive);
  if (!navUi.visible || !wasVisible) navUi.cameraFrameTimes.length = 0;
  if (!navUi.visible) {
    navUi.menuVisible = false;
    navUi.namingActive = false;
    navUi.manualRelocalization = null;
  }
  globalThis.autolifeNavigationCameraPreview = navUi.visible;
  const rgbVisible = navUi.visible && navUi.cameraMode === 'rgbd';
  const depthVisible = navUi.visible && isPointCloudMode();
  navUi.videoPanel?.setAttribute('visible', rgbVisible ? 'true' : 'false');
  if (depthVisible) applyDepthCloudDisplayState();
  else if (navUi.depthRoot) navUi.depthRoot.visible = false;
  navUi.backdropPanel?.setAttribute(
    'visible', (rgbVisible || (depthVisible && !depthCloudIsReady())) ? 'true' : 'false'
  );
  navUi.corridorPanel?.setAttribute('visible', navUi.visible ? 'true' : 'false');
  navUi.panel?.setAttribute('visible', navUi.visible ? 'true' : 'false');
  // The navigation canvas already contains the live RGB frame. Keep the
  // regular full-screen camera panel hidden so two RGB planes never overlap.
  if (typeof setCameraMode === 'function') {
    setCameraMode('passthrough');
  }
  if (navUi.visible) bindNavigationVideoTexture();
  if (depthVisible) connectDepthCloud();
  else disconnectDepthCloud();
  if (notify) {
    navShowNotice(
      navUi.visible
        ? '导航界面已打开\n按 Y 切换透视 / RGB / 深度点云，按 B 锁定头部'
        : '导航界面已关闭',
      2200,
      navUi.visible ? '#72f0b1' : '#68e1ff'
    );
  }
  drawNavigationPanel();
}

function toggleNavigationSession() {
  navUi.sessionActive = true;
  const modes = ['passthrough', 'rgbd'];
  if (navUi.precisionDepthEnabled) modes.push('precision_cloud');
  const current = Math.max(0, modes.indexOf(navUi.cameraMode));
  const previousMode = navUi.cameraMode;
  navUi.cameraMode = modes[(current + 1) % modes.length];
  if (previousMode !== navUi.cameraMode
      && (isPointCloudMode(previousMode) || isPointCloudMode())) {
    disconnectDepthCloud();
  }
  setNavigationView(true, false);
  const labels = {
    passthrough: '头显透视画面\n导航信息保持显示',
    rgbd: 'RGB 实时画面\n背景已切换为黑色',
    precision_cloud: '官方精确点云\n同帧RGB颜色与深度空间'
  };
  navShowNotice(
    labels[navUi.cameraMode] || labels.passthrough,
    2400,
    navUi.cameraMode === 'passthrough' ? '#68e1ff' : '#72f0b1'
  );
}

// This package owns camera presentation inside the navigation panel. The base
// client must never open its old full-screen RGB plane on top of the HUD.
globalThis.autolifeCameraModeToggleHandler = () => true;

function navigationWebsocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/navigation/ws`;
}

function driveWebsocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/navigation/drive`;
}

function ensureDriveWorker() {
  if (navUi.driveWorker || !navUi.enabled || typeof Worker === 'undefined') return;
  const worker = new Worker('/drive_worker.js?v=navigation-21-drive-smooth');
  navUi.driveWorker = worker;
  worker.onmessage = event => {
    const payload = event.data || {};
    if (payload.type !== 'drive_link_state') return;
    navUi.driveLinkState = String(payload.state || 'disconnected');
    if (navUi.driveLinkState === 'connected') {
      navUi.statusLine = '底盘独立控制链路已连接';
    } else if (navUi.driveLinkState === 'congested') {
      navUi.statusLine = '底盘控制链路正在清除积压命令…';
    } else if (navUi.driveLinkState === 'disconnected') {
      navUi.statusLine = '底盘独立控制链路重连中…';
    }
    drawNavigationPanel();
  };
  worker.onerror = () => {
    navUi.driveLinkState = 'disconnected';
    navUi.statusLine = '底盘独立控制线程异常，正在使用兼容链路';
    drawNavigationPanel();
  };
  worker.postMessage({ type: 'configure', url: driveWebsocketUrl() });
}

function connectNavigationWebsocket() {
  if (!navUi.enabled) return;
  window.setInterval(checkNavigationAssetVersion, 30000);
  if (navUi.websocket?.readyState === WebSocket.OPEN
      || navUi.websocket?.readyState === WebSocket.CONNECTING) return;
  const socket = new WebSocket(navigationWebsocketUrl());
  navUi.websocket = socket;
  socket.onopen = () => {
    if (navUi.websocket !== socket) return;
    navUi.statusLine = '底盘控制链路已连接';
    ensureDriveWorker();
    void sendGripperForceRatio(false);
    drawNavigationPanel();
    sendMonitorViewState(true);
  };
  socket.onmessage = event => {
    if (navUi.websocket !== socket) return;
    try {
      const payload = JSON.parse(event.data);
      if (payload?.type === 'corridor_projection') {
        navUi.corridor = payload;
        navUi.corridorReceivedAt = performance.now();
        drawForwardCorridorGuide();
      } else if (payload?.type === 'gripper_force_state') {
        const ratio = Number(payload.ratio);
        if (Number.isFinite(ratio)) {
          navUi.gripperForceRatio = Math.max(
            navUi.gripperForceMinimumRatio, Math.min(1.0, ratio)
          );
          window.localStorage.setItem(
            'autolife-vr-gripper-force-ratio',
            navUi.gripperForceRatio.toFixed(1)
          );
          drawNavigationPanel();
        }
      }
    } catch (_error) {
      // A malformed optional overlay must never affect base control.
    }
  };
  socket.onclose = () => {
    if (navUi.websocket !== socket) return;
    navUi.websocket = null;
    navUi.corridor = null;
    drawForwardCorridorGuide();
    navUi.statusLine = '导航数据链路重连中…';
    drawNavigationPanel();
    window.clearTimeout(navUi.reconnectTimer);
    navUi.reconnectTimer = window.setTimeout(connectNavigationWebsocket, 1000);
  };
  socket.onerror = () => socket.close();
}

function sendBaseDrive(
  deadman,
  forward = 0,
  lateral = 0,
  turn = 0,
  bodyHeightEnabled = false,
  bodyHeightDirection = 0
) {
  const payload = {
    deadman: Boolean(deadman),
    forward,
    lateral,
    turn,
    body_height_enabled: Boolean(deadman && bodyHeightEnabled),
    body_height_direction: deadman ? bodyHeightDirection : 0,
    view: monitorViewState()
  };
  ensureDriveWorker();
  if (navUi.driveWorker) {
    navUi.driveWorker.postMessage({
      type: deadman ? 'sample' : 'release',
      payload
    });
    return;
  }
  // Compatibility fallback for browsers without Worker support.
  const socket = navUi.websocket;
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({ type: 'base_drive', timestamp: Date.now(), ...payload }));
}

async function sendGripperForceRatio(showNotice = false) {
  const requestedRatio = navUi.gripperForceRatio;
  const requestId = ++navUi.gripperForceRequestId;
  try {
    const response = await fetch('/api/gripper/force', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ratio: requestedRatio })
    });
    const result = await response.json();
    if (!response.ok || !result.success) {
      throw new Error(result.message || `HTTP ${response.status}`);
    }
    if (requestId !== navUi.gripperForceRequestId) return true;
    const appliedRatio = Number(result.ratio);
    if (Number.isFinite(appliedRatio)) {
      navUi.gripperForceRatio = Math.max(
        navUi.gripperForceMinimumRatio, Math.min(1.0, appliedRatio)
      );
      window.localStorage.setItem(
        'autolife-vr-gripper-force-ratio',
        navUi.gripperForceRatio.toFixed(1)
      );
    }
    if (showNotice) {
      navShowNotice(
        `夹爪力度已应用：${Math.round(navUi.gripperForceRatio * 100)}%`
          + `（${(navUi.gripperForceRatio * navUi.gripperForceMaximumCurrent).toFixed(1)}`
          + `/${navUi.gripperForceMaximumCurrent.toFixed(1)}）`,
        1600,
        '#72f0b1'
      );
    }
    drawNavigationPanel();
    return true;
  } catch (error) {
    if (requestId !== navUi.gripperForceRequestId) return false;
    navShowNotice(
      `夹爪力度设置失败：${error.message || error}`,
      2200,
      '#ff8b8b'
    );
    return false;
  }
}

function adjustGripperForce(direction) {
  const ratio = Math.max(
    navUi.gripperForceMinimumRatio,
    Math.min(
      1.0,
      navUi.gripperForceRatio + Math.sign(direction) * navUi.gripperForceStep
    )
  );
  navUi.gripperForceRatio = Math.round(ratio * 10) / 10;
  window.localStorage.setItem(
    'autolife-vr-gripper-force-ratio',
    navUi.gripperForceRatio.toFixed(1)
  );
  void sendGripperForceRatio(true);
  drawNavigationPanel();
}

function monitorViewState() {
  let cameraMode = 'unknown';
  try {
    if (navUi.visible) {
      cameraMode = navUi.cameraMode;
    } else if (typeof state !== 'undefined' && state?.image?.mode) {
      cameraMode = state.image.mode;
    }
  } catch (_error) {
    // Monitoring is optional and must never affect VR control.
  }
  const selected = navUi.items[navUi.selected];
  const maximumRows = 5;
  const start = Math.max(0, Math.min(
    navUi.selected - Math.floor(maximumRows / 2),
    Math.max(0, navUi.items.length - maximumRows)
  ));
  const cameraStatus = globalThis.autolifeVrCameraPreview?.getStatus?.() || {};
  const headFollowing = Boolean(globalThis.autolifeHeadFollowControl?.enabled?.());
  let menuTitle = '航点与操作';
  if (navUi.templateConfirmId) menuTitle = '确认预设流程';
  else if (navUi.templateMode) menuTitle = '预设任务流程';
  else if (navUi.navigateConfirmName) menuTitle = '确认导航';
  const manual = navUi.manualRelocalization;
  return {
    layout_version: 2,
    canvas_width: 1200,
    canvas_height: 900,
    title: '云蝶 V29',
    navigation_visible: Boolean(navUi.visible),
    menu_visible: Boolean(navUi.menuVisible),
    camera_mode: cameraMode,
    camera_available: Boolean(cameraStatus.available),
    depth_renderer: navUi.cameraMode === 'precision_cloud'
      ? 'official_rgb565_points_3d' : 'disabled',
    depth_valid_points: Number(navUi.depthValidPoints || 0),
    depth_point_capacity: Number(navUi.depthCapacity || 0),
    depth_last_error: String(navUi.depthLastError || ''),
    head_following: headFollowing,
    base_control_latched: Boolean(navUi.baseControlLatched),
    selected_label: String(selected?.label || ''),
    selected_waypoint: String(selectedWaypointName() || ''),
    menu_title: menuTitle,
    menu_rows: navUi.items.slice(start, start + maximumRows).map((item, offset) => ({
      label: String(item?.label || ''),
      selected: start + offset === navUi.selected
    })),
    naming_active: Boolean(navUi.namingActive),
    naming_draft: String(navUi.waypointNameDraft || ''),
    name_key_index: Number(navUi.nameKeyIndex || 0),
    manual_relocalization: manual ? {
      stage: String(manual.stage || 'position'),
      x: Number(manual.x),
      y: Number(manual.y),
      yaw: Number(manual.yaw || 0)
    } : null,
    vr_camera_fps: Math.round(navigationCameraFps() * 10) / 10
  };
}

// Keep the spectator layout synchronized with the VR canvas using a tiny
// state message.  No composed video frame leaves the headset, and congestion
// on this optional path is dropped instead of delaying teleoperation data.
function sendMonitorViewState(force = false) {
  const socket = navUi.websocket;
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  if (socket.bufferedAmount > 4096) return;
  const view = monitorViewState();
  const key = JSON.stringify(view);
  const now = performance.now();
  if (!force && key === navUi.monitorViewKey && now - navUi.monitorViewLastSentAt < 750) {
    return;
  }
  socket.send(JSON.stringify({ type: 'monitor_view', view }));
  navUi.monitorViewKey = key;
  navUi.monitorViewLastSentAt = now;
}

function setBaseControlLatched(enabled, notify = true) {
  const next = Boolean(enabled);
  if (next === navUi.baseControlLatched) return;
  navUi.baseControlLatched = next;
  if (next) {
    // Claim manual ownership immediately; the next XR frame supplies axes.
    sendBaseDrive(true);
    navUi.driveInputWasActive = true;
  } else {
    navUi.bodyHeightEngaged = false;
    sendBaseDrive(false);
    navUi.driveInputWasActive = false;
  }
  if (notify) {
    navShowNotice(
      next ? '底盘控制已接管\nX 降低身体 · Y 升高身体' : '底盘控制已取消',
      1800,
      next ? '#61f2a7' : '#ffcf70'
    );
  }
  drawNavigationPanel();
}

function navigationItems() {
  const waypoints = Array.isArray(navUi.state?.waypoints) ? navUi.state.waypoints : [];
  const maps = Array.isArray(navUi.state?.maps) ? navUi.state.maps : [];
  const templates = Array.isArray(navUi.state?.task_templates)
    ? navUi.state.task_templates : [];
  if (navUi.templateConfirmId) {
    return [
      { kind: 'cancel-template', label: '← 取消预设流程' },
      {
        kind: 'confirm-template',
        label: `确认启动：${navUi.templateConfirmName}`,
        id: navUi.templateConfirmId,
        name: navUi.templateConfirmName
      }
    ];
  }
  if (navUi.templateMode) {
    return [
      { kind: 'cancel-template', label: '← 返回航点菜单' },
      ...templates.map(template => ({
        kind: 'template-candidate',
        label: `${template.name}（${template.task_count}项）`,
        id: template.id,
        name: template.name
      }))
    ];
  }
  if (navUi.mapConfirmPath) {
    return [
      { kind: 'cancel-map', label: '← 取消切换地图' },
      {
        kind: 'confirm-map',
        label: `确认切换：${navUi.mapConfirmName}`,
        path: navUi.mapConfirmPath,
        name: navUi.mapConfirmName
      }
    ];
  }
  if (navUi.mapMode) {
    return [
      { kind: 'cancel-map', label: '← 返回航点菜单' },
      ...maps.map(map => ({
        kind: 'map-candidate',
        label: map.active ? `● ${map.name}（当前）` : `○ ${map.name}`,
        path: map.path,
        name: map.name,
        active: Boolean(map.active)
      }))
    ];
  }
  if (navUi.navigateConfirmName) {
    return [
      { kind: 'cancel-navigation', label: '← 取消导航' },
      {
        kind: 'confirm-navigation',
        label: `确认前往：${navUi.navigateConfirmName}`,
        name: navUi.navigateConfirmName
      }
    ];
  }
  if (navUi.deleteConfirmName) {
    return [
      { kind: 'cancel-delete', label: '← 取消删除' },
      {
        kind: 'confirm-delete',
        label: `确认删除：${navUi.deleteConfirmName}`,
        name: navUi.deleteConfirmName
      }
    ];
  }
  if (navUi.deleteMode) {
    return [
      { kind: 'cancel-delete', label: '← 取消删除' },
      ...waypoints.map(name => ({
        kind: 'delete-candidate', label: `删除：${name}`, name
      }))
    ];
  }
  return [
    { kind: 'stop', label: '■ 停止当前导航' },
    { kind: 'relocalize', label: '◎ 重新全局定位' },
    { kind: 'manual-relocalize', label: '⌖ 手动重定位' },
    { kind: 'map-library', label: '▣ 切换地图与航点库' },
    { kind: 'task-templates', label: '▶ 预设多任务流程…' },
    {
      kind: 'gripper-force',
      label: `夹爪力度 ${Math.round(navUi.gripperForceRatio * 100)}%  ← →`
    },
    { kind: 'save-waypoint', label: '＋ 保存当前位置为航点' },
    { kind: 'delete-waypoint', label: '－ 删除航点…' },
    ...waypoints.map(name => ({ kind: 'waypoint', label: name, name }))
  ];
}

function createNavigationPanel() {
  if (navUi.panel) return;
  const canvas = document.createElement('canvas');
  canvas.id = 'navigation-vr-canvas';
  canvas.width = 1200;
  canvas.height = 900;
  canvas.style.display = 'none';
  document.body.appendChild(canvas);
  navUi.canvas = canvas;
  navUi.context = canvas.getContext('2d', { alpha: true });

  const panel = document.createElement('a-plane');
  panel.id = 'navigation-vr-panel';
  // The transparent UI is head-locked and sits just in front of a second
  // plane that shares the base RGB texture without copying or resampling it.
  panel.setAttribute('width', '2.3');
  panel.setAttribute('height', '1.725');
  panel.setAttribute('position', `0 ${NAV_PANEL_VERTICAL_OFFSET} -1.99`);
  panel.setAttribute('visible', 'false');
  panel.setAttribute(
    'material',
    'shader: flat; color: #07121b; opacity: 0.98; transparent: true; side: double'
  );
  const videoPanel = document.createElement('a-plane');
  videoPanel.id = 'navigation-vr-video-panel';
  videoPanel.setAttribute('width', '2.3');
  videoPanel.setAttribute('height', '1.725');
  videoPanel.setAttribute('position', `0 ${NAV_PANEL_VERTICAL_OFFSET} -2.0`);
  videoPanel.setAttribute('visible', 'false');
  videoPanel.setAttribute('material', 'shader: flat; color: #101820; side: double');
  const backdropPanel = document.createElement('a-plane');
  backdropPanel.id = 'navigation-vr-black-backdrop';
  backdropPanel.setAttribute('width', '2.36');
  backdropPanel.setAttribute('height', '1.785');
  backdropPanel.setAttribute('position', `0 ${NAV_PANEL_VERTICAL_OFFSET} -2.01`);
  backdropPanel.setAttribute('visible', 'false');
  backdropPanel.setAttribute(
    'material',
    'shader: flat; color: #000000; opacity: 1; transparent: false; side: double'
  );
  const corridorCanvas = document.createElement('canvas');
  corridorCanvas.id = 'navigation-corridor-canvas';
  corridorCanvas.width = 1200;
  corridorCanvas.height = 900;
  corridorCanvas.style.display = 'none';
  document.body.appendChild(corridorCanvas);
  navUi.corridorCanvas = corridorCanvas;
  navUi.corridorContext = corridorCanvas.getContext('2d', { alpha: true });
  const corridorPanel = document.createElement('a-plane');
  corridorPanel.id = 'navigation-vr-corridor-panel';
  corridorPanel.setAttribute('width', '2.3');
  corridorPanel.setAttribute('height', '1.725');
  corridorPanel.setAttribute('position', `0 ${NAV_PANEL_VERTICAL_OFFSET} -1.995`);
  corridorPanel.setAttribute('visible', 'false');
  corridorPanel.setAttribute(
    'material',
    'shader: flat; opacity: 1; transparent: true; side: double; depthWrite: false'
  );
  const attach = () => {
    const camera = document.querySelector('a-scene')?.camera?.el;
    if (!camera) return;
    if (backdropPanel.parentNode !== camera) camera.appendChild(backdropPanel);
    if (videoPanel.parentNode !== camera) camera.appendChild(videoPanel);
    if (corridorPanel.parentNode !== camera) camera.appendChild(corridorPanel);
    if (panel.parentNode !== camera) camera.appendChild(panel);
    attachDepthPointCloudToCamera();
  };
  const ensureCorridorTexture = () => {
    const mesh = corridorPanel.getObject3D('mesh');
    if (!mesh) return false;
    if (!navUi.corridorTexture) navUi.corridorTexture = new THREE.CanvasTexture(corridorCanvas);
    navUi.corridorTexture.minFilter = THREE.LinearFilter;
    navUi.corridorTexture.magFilter = THREE.LinearFilter;
    navUi.corridorTexture.generateMipmaps = false;
    mesh.material = new THREE.MeshBasicMaterial({
      map: navUi.corridorTexture,
      transparent: true,
      depthTest: false,
      depthWrite: false,
      side: THREE.DoubleSide
    });
    mesh.renderOrder = 20;
    drawForwardCorridorGuide();
    return true;
  };
  const ensureTexture = () => {
    const mesh = panel.getObject3D('mesh');
    if (!mesh) return false;
    if (!navUi.texture) navUi.texture = new THREE.CanvasTexture(canvas);
    navUi.texture.minFilter = THREE.LinearFilter;
    navUi.texture.magFilter = THREE.LinearFilter;
    navUi.texture.generateMipmaps = false;
    mesh.material = new THREE.MeshBasicMaterial({
      map: navUi.texture,
      transparent: true,
      depthTest: false,
      depthWrite: false,
      side: THREE.DoubleSide
    });
    mesh.renderOrder = 30;
    navUi.textureBound = true;
    drawNavigationPanel();
    return true;
  };
  panel.addEventListener('object3dset', ensureTexture);
  panel.addEventListener('loaded', ensureTexture);
  document.querySelector('a-scene').appendChild(backdropPanel);
  document.querySelector('a-scene').appendChild(videoPanel);
  corridorPanel.addEventListener('object3dset', ensureCorridorTexture);
  corridorPanel.addEventListener('loaded', ensureCorridorTexture);
  document.querySelector('a-scene').appendChild(corridorPanel);
  document.querySelector('a-scene').appendChild(panel);
  navUi.videoPanel = videoPanel;
  navUi.backdropPanel = backdropPanel;
  navUi.corridorPanel = corridorPanel;
  navUi.panel = panel;
  attach();
  document.querySelector('a-scene').addEventListener('enter-vr', attach);
  window.setTimeout(ensureTexture, 0);
  window.setTimeout(ensureTexture, 250);
  window.setTimeout(ensureCorridorTexture, 0);
  window.setTimeout(ensureCorridorTexture, 250);
  navUi.ensureTexture = ensureTexture;
}

function bindNavigationVideoTexture() {
  const mesh = navUi.videoPanel?.getObject3D('mesh');
  const texture = globalThis.autolifeVrCameraPreview?.getTexture?.();
  if (!mesh || !texture) return false;
  if (navUi.videoTexture !== texture) {
    mesh.material?.dispose?.();
    mesh.material = new THREE.MeshBasicMaterial({
      map: texture,
      depthTest: false,
      depthWrite: false,
      side: THREE.DoubleSide,
      toneMapped: false
    });
    navUi.videoTexture = texture;
  }
  mesh.renderOrder = 10;
  return true;
}

function depthCloudWebsocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/navigation/precision-cloud`;
}

function disconnectDepthCloud() {
  navUi.depthGeneration += 1;
  navUi.depthOnline = false;
  navUi.depthConnectedAt = 0;
  navUi.depthLastPacketAt = 0;
  navUi.depthLastFrameAt = 0;
  navUi.depthValidPoints = 0;
  navUi.depthGeometry?.setDrawRange?.(0, 0);
  navUi.depthPendingFrame = null;
  navUi.depthRenderScheduled = false;
  if (navUi.depthRoot) navUi.depthRoot.visible = false;
  if (navUi.depthReconnectTimer) {
    window.clearTimeout(navUi.depthReconnectTimer);
    navUi.depthReconnectTimer = null;
  }
  const socket = navUi.depthSocket;
  navUi.depthSocket = null;
  if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, 'mode changed');
}

function scheduleDepthCloudReconnect(generation) {
  if (
    generation !== navUi.depthGeneration
    || !isPointCloudMode()
    || !navUi.visible
    || navUi.depthReconnectTimer
  ) return;
  navUi.depthReconnectTimer = window.setTimeout(() => {
    navUi.depthReconnectTimer = null;
    connectDepthCloud();
  }, 600);
}

function attachDepthPointCloudToCamera() {
  const cameraObject = document.querySelector('a-scene')?.camera?.el?.object3D;
  if (!cameraObject) return false;
  if (!navUi.depthRoot) {
    const root = new THREE.Group();
    root.name = 'navigation-depth-point-cloud-root';
    root.position.set(0, Number(NAV_PANEL_VERTICAL_OFFSET) || 0, 0);
    root.visible = false;

    // A dedicated far plane keeps the point-cloud view nearly black without
    // flattening or hiding XYZ points at their real depth.
    const background = new THREE.Mesh(
      new THREE.PlaneGeometry(40, 40),
      new THREE.MeshBasicMaterial({
        color: 0x000000,
        depthTest: false,
        depthWrite: false,
        side: THREE.DoubleSide,
        toneMapped: false
      })
    );
    background.name = 'navigation-depth-black-background';
    background.position.set(0, 0, -5.15);
    background.renderOrder = 0;
    root.add(background);
    navUi.depthRoot = root;
    navUi.depthBackground = background;
  }
  if (navUi.depthRoot.parent !== cameraObject) {
    navUi.depthRoot.parent?.remove(navUi.depthRoot);
    cameraObject.add(navUi.depthRoot);
  }
  navUi.depthRoot.visible = Boolean(
    navUi.visible && isPointCloudMode()
  );
  return true;
}

function ensureDepthPointGeometry(config) {
  const columns = Number(config?.columns || 0);
  const rows = Number(config?.rows || 0);
  const capacity = columns * rows;
  const coloured = config?.color_enabled === true;
  const format = `${capacity}:${coloured ? 'rgb565' : 'white'}`;
  if (capacity <= 0 || !attachDepthPointCloudToCamera()) return false;
  if (navUi.depthGeometry && navUi.depthGeometryFormat === format) return true;

  if (navUi.depthPoints) {
    navUi.depthRoot?.remove(navUi.depthPoints);
    navUi.depthGeometry?.dispose?.();
    navUi.depthPoints.material?.dispose?.();
  }
  const positions = new Float32Array(capacity * 3);
  const raysX = new Float32Array(capacity);
  const raysY = new Float32Array(capacity);
  const positionAttribute = new THREE.BufferAttribute(positions, 3);
  positionAttribute.setUsage?.(THREE.DynamicDrawUsage);
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', positionAttribute);
  let colors = null;
  let colorAttribute = null;
  if (coloured) {
    colors = new Float32Array(capacity * 3);
    colorAttribute = new THREE.BufferAttribute(colors, 3);
    colorAttribute.setUsage?.(THREE.DynamicDrawUsage);
    geometry.setAttribute('color', colorAttribute);
  }
  geometry.setDrawRange(0, 0);
  const material = new THREE.PointsMaterial({
    color: 0xffffff,
    vertexColors: coloured,
    // Match the official robot_v2_2 PointCloudSystem: a fixed two-pixel
    // screen-space particle with no distance attenuation.
    size: 2.0,
    sizeAttenuation: false,
    transparent: false,
    depthTest: true,
    depthWrite: true,
    toneMapped: false
  });
  const points = new THREE.Points(geometry, material);
  points.name = coloured
    ? 'navigation-official-precision-rgb-points'
    : 'navigation-depth-white-points';
  points.frustumCulled = false;
  points.raycast = () => {};
  points.renderOrder = 10;
  navUi.depthRoot.add(points);
  navUi.depthPoints = points;
  navUi.depthGeometry = geometry;
  navUi.depthPositionAttribute = positionAttribute;
  navUi.depthColorAttribute = colorAttribute;
  navUi.depthPositions = positions;
  navUi.depthColors = colors;
  navUi.depthRaysX = raysX;
  navUi.depthRaysY = raysY;
  navUi.depthCapacity = capacity;
  navUi.depthGeometryFormat = format;
  navUi.depthValidPoints = 0;

  // UV layout and camera intrinsics do not change between frames. Precompute
  // the unprojection rays once; the old per-frame divisions and temporary
  // [u,v] arrays caused periodic garbage collection stalls on Quest.
  const fx = Number(config.fx || 0);
  const fy = Number(config.fy || 0);
  const ppx = Number(config.ppx || 0);
  const ppy = Number(config.ppy || 0);
  const stride = Math.max(1, Number(config.stride || 1));
  const offset = Number(config.offset || 0);
  for (let index = 0; index < capacity; index += 1) {
    let sourceU;
    let sourceV;
    if (config.sampling === 'stable_uniform_uv') {
      [sourceU, sourceV] = precisionSourceUv(config, index, capacity);
    } else {
      sourceU = offset + (index % columns) * stride;
      sourceV = offset + Math.floor(index / columns) * stride;
    }
    raysX[index] = (sourceU - ppx) / fx;
    raysY[index] = -(sourceV - ppy) / fy;
  }

  const maximum = Math.max(0.5, Number(
    config.maximum_m || navUi.depthMaximumM
  ));
  if (navUi.depthBackground) navUi.depthBackground.position.z = -(maximum + 0.15);
  return true;
}

function precisionSourceUv(config, index, pointCount) {
  const gridColumns = Math.max(2, Number(config.grid_columns || 2));
  const gridRows = Math.max(2, Number(config.grid_rows || 2));
  const gridCount = gridColumns * gridRows;
  const selected = pointCount <= 1
    ? 0 : Math.round(index * (gridCount - 1) / (pointCount - 1));
  const gridU = selected % gridColumns;
  const gridV = Math.floor(selected / gridColumns);
  const sourceWidth = Math.max(2, Number(config.source_width || 2));
  const sourceHeight = Math.max(2, Number(config.source_height || 2));
  return [
    Math.round(gridU * (sourceWidth - 1) / (gridColumns - 1)),
    Math.round(gridV * (sourceHeight - 1) / (gridRows - 1))
  ];
}

function renderDepthCloudFrame(buffer) {
  const config = navUi.depthConfig;
  if (!config || !(buffer instanceof ArrayBuffer)) return;
  const columns = Number(config.columns || 0);
  const rows = Number(config.rows || 0);
  const pointCount = columns * rows;
  const coloured = config.color_enabled === true;
  const bytesPerPoint = coloured ? 4 : 2;
  if (
    columns <= 0 || rows <= 0
    || buffer.byteLength !== 4 + pointCount * bytesPerPoint
    || !ensureDepthPointGeometry(config)
  ) return;

  const scale = Number(config.depth_scale_m || 0.001);
  const minimum = Number(config.minimum_m || navUi.depthMinimumM);
  const maximum = Math.max(minimum + 0.1, Number(
    config.maximum_m || navUi.depthMaximumM
  ));
  const view = new DataView(buffer);
  const positions = navUi.depthPositions;
  const colors = navUi.depthColors;
  const raysX = navUi.depthRaysX;
  const raysY = navUi.depthRaysY;
  const colourPlaneOffset = 4 + pointCount * 2;
  let validPoints = 0;
  for (let index = 0; index < pointCount; index += 1) {
    const distance = view.getUint16(4 + index * 2, true) * scale;
    if (distance < minimum || distance > maximum) continue;
    const target = validPoints * 3;
    positions[target] = raysX[index] * distance;
    positions[target + 1] = raysY[index] * distance;
    // Camera optical +Z points forward; Three.js cameras look along local -Z.
    positions[target + 2] = -distance;
    if (coloured && colors) {
      const rgb565 = view.getUint16(colourPlaneOffset + index * 2, true);
      colors[target] = ((rgb565 >> 11) & 0x1f) / 31;
      colors[target + 1] = ((rgb565 >> 5) & 0x3f) / 63;
      colors[target + 2] = (rgb565 & 0x1f) / 31;
    }
    validPoints += 1;
  }
  if (typeof navUi.depthPositionAttribute.clearUpdateRanges === 'function'
      && typeof navUi.depthPositionAttribute.addUpdateRange === 'function') {
    navUi.depthPositionAttribute.clearUpdateRanges();
    navUi.depthPositionAttribute.addUpdateRange(0, validPoints * 3);
  } else if (navUi.depthPositionAttribute.updateRange) {
    navUi.depthPositionAttribute.updateRange.offset = 0;
    navUi.depthPositionAttribute.updateRange.count = validPoints * 3;
  }
  navUi.depthPositionAttribute.needsUpdate = true;
  if (navUi.depthColorAttribute) {
    if (typeof navUi.depthColorAttribute.clearUpdateRanges === 'function'
        && typeof navUi.depthColorAttribute.addUpdateRange === 'function') {
      navUi.depthColorAttribute.clearUpdateRanges();
      navUi.depthColorAttribute.addUpdateRange(0, validPoints * 3);
    } else if (navUi.depthColorAttribute.updateRange) {
      navUi.depthColorAttribute.updateRange.offset = 0;
      navUi.depthColorAttribute.updateRange.count = validPoints * 3;
    }
    navUi.depthColorAttribute.needsUpdate = true;
  }
  navUi.depthGeometry.setDrawRange(0, validPoints);
  navUi.depthValidPoints = validPoints;
  navUi.depthFrameCount += 1;
  navUi.depthLastFrameAt = performance.now();
  navUi.depthLastError = '';
  applyDepthCloudDisplayState(navUi.depthLastFrameAt);
}

function renderPendingDepthCloudFrame() {
  const pending = navUi.depthPendingFrame;
  navUi.depthPendingFrame = null;
  if (!pending || pending.generation !== navUi.depthGeneration) return;
  try {
    renderDepthCloudFrame(pending.buffer);
  } catch (error) {
    navUi.depthLastError = `3D frame: ${String(error?.message || error)}`;
    console.error('Precision point-cloud frame rejected:', error);
  } finally {
    // Rendering is the consumer boundary. Acknowledge only after this sample
    // has left the capacity-one browser queue; the robot then sends whichever
    // sensor sample is newest instead of replaying old TCP-buffered frames.
    const socket = navUi.depthSocket;
    if (
      pending.buffer?.byteLength >= 4
      && pending.generation === navUi.depthGeneration
      && socket?.readyState === WebSocket.OPEN
      && socket.bufferedAmount < 4096
    ) {
      const sequence = new DataView(pending.buffer).getUint32(0, true);
      socket.send(JSON.stringify({ type: 'depth_frame_ack', sequence }));
    }
  }
}

function queueDepthCloudFrame(buffer, generation) {
  // Capacity-one latest-frame queue. A busy render frame is overwritten by
  // newer sensor data instead of replaying stale WebSocket frames and adding
  // visible latency to the VR view.
  navUi.depthPendingFrame = { buffer, generation };
  if (navUi.depthRenderScheduled) return;
  navUi.depthRenderScheduled = true;
  const renderLatest = () => {
    navUi.depthRenderScheduled = false;
    renderPendingDepthCloudFrame();
  };
  // During an immersive Quest session, Chromium may suspend the ordinary
  // window animation loop. Schedule against the active XRSession so queued
  // point-cloud frames cannot remain forever behind the black backdrop.
  const xrSession = document.querySelector('a-scene')?.renderer?.xr?.getSession?.();
  if (xrSession?.requestAnimationFrame) {
    xrSession.requestAnimationFrame(renderLatest);
  } else if (window.requestAnimationFrame) {
    window.requestAnimationFrame(renderLatest);
  } else {
    window.setTimeout(renderLatest, 0);
  }
}

function connectDepthCloud() {
  if (!navUi.precisionDepthEnabled || !isPointCloudMode() || !navUi.visible) return;
  if (
    navUi.depthSocket?.readyState === WebSocket.OPEN
    || navUi.depthSocket?.readyState === WebSocket.CONNECTING
  ) return;
  const generation = ++navUi.depthGeneration;
  const socket = new WebSocket(depthCloudWebsocketUrl());
  socket.binaryType = 'arraybuffer';
  navUi.depthSocket = socket;
  socket.onopen = () => {
    if (generation !== navUi.depthGeneration) return;
    navUi.depthOnline = true;
    navUi.depthConnectedAt = performance.now();
    // Give this new connection its own watchdog grace period. Reusing the
    // previous connection's frame timestamp caused immediate reconnect loops.
    navUi.depthLastPacketAt = navUi.depthConnectedAt;
    drawNavigationPanel();
  };
  socket.onmessage = event => {
    if (generation !== navUi.depthGeneration) return;
    if (typeof event.data === 'string') {
      try {
        const config = JSON.parse(event.data);
        if (config?.type === 'depth_cloud_config') {
          navUi.depthConfig = config;
          if (!ensureDepthPointGeometry(config)) {
            navUi.depthLastError = '3D renderer is waiting for the WebXR camera';
          }
        }
      } catch (error) {
        navUi.depthLastError = `depth configuration: ${String(error?.message || error)}`;
      }
      return;
    }
    if (event.data instanceof ArrayBuffer) {
      navUi.depthLastPacketAt = performance.now();
      queueDepthCloudFrame(event.data, generation);
    } else if (event.data instanceof Blob) {
      // Quest Chromium normally honours binaryType=arraybuffer, but Blob is a
      // valid WebSocket binary representation. Supporting both avoids a
      // browser-version-specific black screen.
      void event.data.arrayBuffer().then(buffer => {
        if (generation !== navUi.depthGeneration) return;
        navUi.depthLastPacketAt = performance.now();
        queueDepthCloudFrame(buffer, generation);
      });
    }
  };
  socket.onerror = () => {
    navUi.depthOnline = false;
    navUi.depthLastError = 'depth WebSocket error';
  };
  socket.onclose = () => {
    if (navUi.depthSocket === socket) navUi.depthSocket = null;
    navUi.depthOnline = false;
    drawNavigationPanel();
    scheduleDepthCloudReconnect(generation);
  };
}

function recordNavigationCameraFrame(now = performance.now()) {
  if (!navUi.visible) {
    navUi.cameraFrameTimes.length = 0;
    return;
  }
  navUi.cameraFrameTimes.push(now);
  const cutoff = now - 1000;
  while (navUi.cameraFrameTimes[0] < cutoff) navUi.cameraFrameTimes.shift();
}

function navigationCameraFps(now = performance.now()) {
  const samples = navUi.cameraFrameTimes;
  const cutoff = now - 1000;
  while (samples[0] < cutoff) samples.shift();
  if (samples.length < 2 || now - samples[samples.length - 1] > 1000) return 0.0;
  const duration = samples[samples.length - 1] - samples[0];
  return duration > 0 ? (samples.length - 1) * 1000 / duration : 0.0;
}

function roundRect(context, x, y, width, height, radius, fillStyle) {
  const safeRadius = Math.max(0, Math.min(radius, width / 2, height / 2));
  context.beginPath();
  context.moveTo(x + safeRadius, y);
  context.lineTo(x + width - safeRadius, y);
  context.quadraticCurveTo(x + width, y, x + width, y + safeRadius);
  context.lineTo(x + width, y + height - safeRadius);
  context.quadraticCurveTo(x + width, y + height, x + width - safeRadius, y + height);
  context.lineTo(x + safeRadius, y + height);
  context.quadraticCurveTo(x, y + height, x, y + height - safeRadius);
  context.lineTo(x, y + safeRadius);
  context.quadraticCurveTo(x, y, x + safeRadius, y);
  context.closePath();
  context.fillStyle = fillStyle;
  context.fill();
}

function statusText(payload, fallback) {
  if (!payload || typeof payload !== 'object') return fallback;
  return String(payload.detail || payload.message || payload.state || fallback);
}

function selectedWaypointName() {
  if (!navUi.menuVisible) return null;
  const item = navUi.items[navUi.selected];
  if (item?.kind === 'waypoint' || item?.kind === 'delete-candidate') return item.name;
  if (navUi.navigateConfirmName) return navUi.navigateConfirmName;
  if (navUi.deleteConfirmName) return navUi.deleteConfirmName;
  return null;
}

function waypointPose(name) {
  const poses = Array.isArray(navUi.state?.waypoint_poses)
    ? navUi.state.waypoint_poses
    : [];
  return poses.find(pose => pose?.name === name) || null;
}

function mapPoint(meta, left, top, scale, pose) {
  if (!pose) return null;
  const dx = pose.x - meta.origin_x;
  const dy = pose.y - meta.origin_y;
  const cosine = Math.cos(meta.origin_yaw || 0);
  const sine = Math.sin(meta.origin_yaw || 0);
  const mapX = (cosine * dx + sine * dy) / meta.resolution;
  const mapY = (-sine * dx + cosine * dy) / meta.resolution;
  return {
    x: left + mapX * scale,
    y: top + (meta.height - mapY) * scale,
    heading: pose.yaw - (meta.origin_yaw || 0)
  };
}

function normalizedAngle(angle) {
  return Math.atan2(Math.sin(angle), Math.cos(angle));
}

function worldPoseFromMapMeters(meta, mapX, mapY, yaw) {
  const originYaw = Number(meta.origin_yaw || 0);
  const cosine = Math.cos(originYaw);
  const sine = Math.sin(originYaw);
  return {
    x: Number(meta.origin_x) + cosine * mapX - sine * mapY,
    y: Number(meta.origin_y) + sine * mapX + cosine * mapY,
    yaw: normalizedAngle(yaw)
  };
}

function startManualRelocalization() {
  const meta = navUi.state?.map;
  if (!meta || !navUi.mapBitmap) {
    navShowNotice('地图尚未就绪，无法进行手动重定位', 3000, '#ffcf70');
    return;
  }
  let pose = navUi.state?.pose;
  if (!pose) {
    pose = worldPoseFromMapMeters(
      meta,
      Number(meta.width) * Number(meta.resolution) * 0.5,
      Number(meta.height) * Number(meta.resolution) * 0.5,
      Number(meta.origin_yaw || 0)
    );
  }
  setBaseControlLatched(false, false);
  sendBaseDrive(false);
  navUi.menuVisible = false;
  navUi.manualRelocalization = {
    stage: 'position',
    x: Number(pose.x),
    y: Number(pose.y),
    yaw: Number(pose.yaw || 0)
  };
  navShowNotice('手动重定位：右摇杆移动光标，按 A 确定位置', 3500, '#ffd66b');
  drawNavigationPanel();
}

function cancelManualRelocalization(notify = true) {
  if (!navUi.manualRelocalization) return;
  navUi.manualRelocalization = null;
  if (notify) navShowNotice('已取消手动重定位', 1800, '#ffcf70');
  drawNavigationPanel();
}

function shapedManualAxis(value, deadzone = 0.16) {
  const numeric = Number(value || 0);
  const magnitude = Math.abs(numeric);
  if (magnitude <= deadzone) return 0;
  return Math.sign(numeric) * (magnitude - deadzone) / (1 - deadzone);
}

function updateManualRelocalization(horizontal, vertical, deltaMilliseconds) {
  const cursor = navUi.manualRelocalization;
  const meta = navUi.state?.map;
  if (!cursor || !meta) return;
  const elapsed = Math.max(0, Math.min(0.05, Number(deltaMilliseconds || 0) / 1000));
  if (elapsed <= 0) return;
  const axisX = shapedManualAxis(horizontal);
  const axisY = shapedManualAxis(-vertical);
  const originYaw = Number(meta.origin_yaw || 0);
  if (cursor.stage === 'position') {
    if (axisX === 0 && axisY === 0) return;
    const dx = cursor.x - Number(meta.origin_x);
    const dy = cursor.y - Number(meta.origin_y);
    const cosine = Math.cos(originYaw);
    const sine = Math.sin(originYaw);
    let mapX = cosine * dx + sine * dy;
    let mapY = -sine * dx + cosine * dy;
    const speed = Number(navUi.manualRelocalizationMoveSpeed || 1.80);
    mapX += axisX * speed * elapsed;
    mapY += axisY * speed * elapsed;
    const margin = Math.max(0.02, Number(meta.resolution || 0.05));
    mapX = Math.max(margin, Math.min(Number(meta.width) * Number(meta.resolution) - margin, mapX));
    mapY = Math.max(margin, Math.min(Number(meta.height) * Number(meta.resolution) - margin, mapY));
    Object.assign(cursor, worldPoseFromMapMeters(meta, mapX, mapY, cursor.yaw));
  } else if (Math.hypot(axisX, axisY) > 0.20) {
    const desiredYaw = normalizedAngle(originYaw + Math.atan2(axisY, axisX));
    const yawError = normalizedAngle(desiredYaw - cursor.yaw);
    const maximumStep = 2.8 * elapsed;
    cursor.yaw = normalizedAngle(
      cursor.yaw + Math.max(-maximumStep, Math.min(maximumStep, yawError))
    );
  } else return;
  drawNavigationPanel();
}

async function confirmManualRelocalization() {
  const cursor = navUi.manualRelocalization;
  if (!cursor) return;
  if (cursor.stage === 'position') {
    cursor.stage = 'heading';
    navShowNotice('位置已确定：右摇杆调整箭头方向，再按 A 完成', 3500, '#ffd66b');
    drawNavigationPanel();
    return;
  }
  navShowNotice('正在发送手动重定位位姿…', 1800, '#68e1ff');
  const succeeded = await navigationAction('/api/navigation/manual-relocalize', {
    x: cursor.x,
    y: cursor.y,
    yaw: cursor.yaw
  });
  if (succeeded) {
    navUi.manualRelocalization = null;
    await refreshNavigationState();
  }
  drawNavigationPanel();
}

function drawPoseArrow(context, point, size, fill, stroke, ring = false) {
  if (!point) return;
  context.save();
  context.translate(point.x, point.y);
  if (ring) {
    context.beginPath();
    context.arc(0, 0, size + 8, 0, Math.PI * 2);
    context.setLineDash([5, 4]);
    context.strokeStyle = stroke;
    context.lineWidth = 3;
    context.stroke();
    context.setLineDash([]);
  }
  context.rotate(-point.heading);
  context.beginPath();
  context.moveTo(size, 0);
  context.lineTo(-size * 0.65, -size * 0.58);
  context.lineTo(-size * 0.38, 0);
  context.lineTo(-size * 0.65, size * 0.58);
  context.closePath();
  context.fillStyle = fill;
  context.strokeStyle = stroke;
  context.lineWidth = Math.max(2, size * 0.17);
  context.fill();
  context.stroke();
  context.restore();
}

function drawMap(context, area) {
  const uiScale = Number(area.uiScale || 1.0);
  roundRect(context, area.x, area.y, area.w, area.h, 18 * uiScale, '#14232b');
  const bitmap = navUi.mapBitmap;
  const meta = navUi.state?.map;
  if (!bitmap || !meta) {
    context.fillStyle = '#475a64';
    context.font = `${Math.round(28 * uiScale)}px sans-serif`;
    context.fillText('等待 /map 地图…', area.x + 75 * uiScale, area.y + area.h / 2);
    return;
  }
  const scale = Math.min(area.w / bitmap.width, area.h / bitmap.height);
  const width = bitmap.width * scale;
  const height = bitmap.height * scale;
  const left = area.x + (area.w - width) / 2;
  const top = area.y + (area.h - height) / 2;
  context.save();
  roundRect(
    context, area.x, area.y, area.w, area.h,
    14 * uiScale, 'rgba(0, 0, 0, 0)'
  );
  context.clip();
  context.drawImage(bitmap, left, top, width, height);
  context.restore();

  const selectedPose = waypointPose(selectedWaypointName());
  drawPoseArrow(
    context,
    mapPoint(meta, left, top, scale, selectedPose),
    18 * uiScale,
    '#ff4fd8',
    '#fff1fb',
    true
  );
  drawPoseArrow(
    context,
    mapPoint(meta, left, top, scale, navUi.manualRelocalization),
    (navUi.manualRelocalization?.stage === 'heading' ? 21 : 17) * uiScale,
    '#ffc247',
    '#fff7d6',
    true
  );
  drawPoseArrow(
    context,
    mapPoint(meta, left, top, scale, navUi.state?.pose),
    13 * uiScale,
    '#087a45',
    navUi.state?.pose_fresh ? '#d7ffeb' : '#ffcf70'
  );
}

function mapDisplayArea() {
  const maximumWidth = 540 * MAP_DISPLAY_SCALE;
  const maximumHeight = 380 * MAP_DISPLAY_SCALE;
  const bitmap = navUi.mapBitmap;
  let aspect = bitmap?.width > 0 && bitmap?.height > 0
    ? bitmap.width / bitmap.height
    : 1.45;
  if (!Number.isFinite(aspect) || aspect <= 0) aspect = 1.45;
  let width = maximumWidth;
  let height = width / aspect;
  if (height > maximumHeight) {
    height = maximumHeight;
    width = height * aspect;
  }
  return {
    x: 24,
    y: 876 - height,
    w: width,
    h: height,
    uiScale: MAP_DISPLAY_SCALE
  };
}

function drawForwardCorridorGuide() {
  const context = navUi.corridorContext;
  const canvas = navUi.corridorCanvas;
  if (!context || !canvas) return;
  context.clearRect(0, 0, canvas.width, canvas.height);
  const corridor = navUi.corridor;
  const fresh = performance.now() - navUi.corridorReceivedAt < 450;
  const renderKey = `${navUi.visible ? 1 : 0}:${fresh ? 1 : 0}:${corridor?.sequence ?? -1}`;
  if (renderKey === navUi.corridorRenderKey) return;
  navUi.corridorRenderKey = renderKey;
  if (!navUi.visible || !fresh) {
    if (navUi.corridorTexture) navUi.corridorTexture.needsUpdate = true;
    return;
  }
  drawDirectionalObstacleDistances(context, canvas, corridor?.obstacles);
  const arraysValid = ['left', 'centre', 'right'].every(
    key => Array.isArray(corridor?.[key]) && corridor[key].length >= 3
  );
  if (!corridor?.valid || !arraysValid) {
    if (navUi.corridorTexture) navUi.corridorTexture.needsUpdate = true;
    return;
  }
  const toPixels = points => points.map(point => [
    Number(point[0]) * canvas.width,
    Number(point[1]) * canvas.height
  ]).filter(point => point.every(Number.isFinite));
  const left = toPixels(corridor.left);
  const centre = toPixels(corridor.centre);
  const right = toPixels(corridor.right);
  const count = Math.min(left.length, centre.length, right.length);
  if (count < 3) return;
  const blockedAt = Number.isInteger(corridor.blocked_at)
    ? Math.max(-1, Math.min(count - 1, corridor.blocked_at))
    : -1;
  const clearColour = corridor.depth_valid === false
    ? 'rgba(255, 207, 112, 0.72)'
    : 'rgba(92, 240, 198, 0.76)';

  context.save();
  context.beginPath();
  context.moveTo(left[0][0], left[0][1]);
  for (let index = 1; index < count; index += 1) context.lineTo(left[index][0], left[index][1]);
  for (let index = count - 1; index >= 0; index -= 1) context.lineTo(right[index][0], right[index][1]);
  context.closePath();
  context.fillStyle = 'rgba(62, 230, 180, 0.025)';
  context.fill();
  context.lineCap = 'round';
  context.lineJoin = 'round';
  context.lineWidth = 2.2;

  const strokeRange = (points, start, end, colour) => {
    if (end - start < 1) return;
    context.beginPath();
    context.moveTo(points[start][0], points[start][1]);
    for (let index = start + 1; index <= end; index += 1) {
      context.lineTo(points[index][0], points[index][1]);
    }
    context.strokeStyle = colour;
    context.stroke();
  };
  for (const edge of [left, right]) {
    if (blockedAt >= 1) strokeRange(edge, 0, blockedAt, clearColour);
    if (blockedAt >= 0 && blockedAt < count - 1) {
      strokeRange(edge, Math.max(0, blockedAt), count - 1, 'rgba(255, 102, 94, 0.82)');
    } else if (blockedAt < 0) {
      strokeRange(edge, 0, count - 1, clearColour);
    }
  }
  context.setLineDash([10, 12]);
  context.lineWidth = 1.2;
  strokeRange(centre, 0, count - 1, 'rgba(225, 255, 247, 0.42)');
  context.setLineDash([]);
  context.restore();
  if (navUi.corridorTexture) navUi.corridorTexture.needsUpdate = true;
}

function drawDirectionalObstacleDistances(context, canvas, obstacles) {
  if (!obstacles?.valid || !obstacles.directions) return;
  const positions = {
    front: [0.50, 0.10],
    front_left: [0.23, 0.17],
    left: [0.10, 0.47],
    rear_left: [0.23, 0.86],
    rear: [0.50, 0.92],
    rear_right: [0.77, 0.86],
    right: [0.90, 0.47],
    front_right: [0.77, 0.17]
  };
  const labels = {
    front: '前', front_left: '左前', left: '左', rear_left: '左后',
    rear: '后', rear_right: '右后', right: '右', front_right: '右前'
  };
  const colours = {
    green: '#64e8a2', yellow: '#ffd166', red: '#ff625d'
  };
  context.save();
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  for (const [key, position] of Object.entries(positions)) {
    const item = obstacles.directions[key];
    const distance = Number(item?.distance_m);
    if (!Number.isFinite(distance) || distance < 0 || distance > 1.0) continue;
    const x = position[0] * canvas.width;
    const y = position[1] * canvas.height;
    const colour = colours[item.level] || colours.green;
    const text = `${labels[key]} ${distance < 1 ? distance.toFixed(2) : distance.toFixed(1)}m`;
    context.font = 'bold 19px sans-serif';
    const width = Math.max(76, context.measureText(text).width + 24);
    roundRect(context, x - width / 2, y - 15, width, 30, 11, 'rgba(3, 14, 21, 0.58)');
    context.strokeStyle = colour;
    context.lineWidth = item.level === 'red' ? 2.0 : 1.2;
    context.strokeRect(x - width / 2 + 1, y - 14, width - 2, 28);
    context.fillStyle = colour;
    context.fillText(text, x, y + 1);
  }
  context.restore();
}

function drawWaypointNameEditor(context) {
  roundRect(context, 570, 78, 606, 720, 18, 'rgba(4, 18, 27, 0.94)');
  context.fillStyle = '#eafaff';
  context.font = 'bold 24px sans-serif';
  context.fillText('输入航点名称', 594, 116);
  roundRect(context, 590, 136, 566, 58, 11, 'rgba(13, 46, 60, 0.96)');
  context.fillStyle = '#ffffff';
  context.font = 'bold 25px sans-serif';
  const shownName = navUi.waypointNameDraft || '请输入名称';
  context.fillText(shownName.slice(0, 28), 608, 174);

  const startX = 590;
  const startY = 214;
  const keyWidth = 86;
  const keyHeight = 48;
  const gapX = 9;
  const gapY = 8;
  WAYPOINT_NAME_KEYS.forEach((key, index) => {
    const column = index % WAYPOINT_NAME_COLUMNS;
    const row = Math.floor(index / WAYPOINT_NAME_COLUMNS);
    const x = startX + column * (keyWidth + gapX);
    const y = startY + row * (keyHeight + gapY);
    const selected = index === navUi.nameKeyIndex;
    const actionColor = key.action === 'save'
      ? 'rgba(17, 130, 81, 0.96)'
      : (key.action === 'cancel' ? 'rgba(128, 62, 48, 0.96)' : 'rgba(15, 54, 69, 0.96)');
    roundRect(
      context,
      x,
      y,
      keyWidth,
      keyHeight,
      8,
      selected ? 'rgba(10, 130, 165, 0.98)' : actionColor
    );
    context.fillStyle = selected ? '#ffffff' : '#d7edf4';
    context.font = key.label.length > 3 ? 'bold 14px sans-serif' : 'bold 17px sans-serif';
    const labelWidth = context.measureText(key.label).width;
    context.fillText(key.label, x + (keyWidth - labelWidth) / 2, y + 31);
  });
  context.fillStyle = '#9edbea';
  context.font = '16px sans-serif';
  context.fillText('右摇杆选择 · A 输入 · X 退格 · Y 取消', 690, 780);
}

function drawNavigationPanel() {
  const context = navUi.context;
  if (!context) return;
  context.clearRect(0, 0, navUi.canvas.width, navUi.canvas.height);
  roundRect(context, 18, 14, 188, 50, 12, 'rgba(3, 14, 21, 0.82)');
  context.fillStyle = '#68e1ff';
  context.font = 'bold 31px sans-serif';
  context.fillText('云蝶 V29', 30, 49);
  const cameraStatus = globalThis.autolifeVrCameraPreview?.getStatus?.() || {};
  roundRect(context, 1015, 18, 161, 40, 10, 'rgba(3, 14, 21, 0.82)');
  const depthMode = isPointCloudMode();
  const imageOnline = depthMode ? navUi.depthOnline : cameraStatus.available;
  context.fillStyle = imageOnline ? '#72f0b1' : '#ffcf70';
  context.font = 'bold 17px sans-serif';
  context.fillText(
    depthMode
      ? (navUi.depthOnline ? '● 深度点云' : '○ 点云连接中')
      : (cameraStatus.available ? '● RGB 实时' : '○ RGB 连接中'),
    1032,
    44
  );
  const headFollowing = Boolean(globalThis.autolifeHeadFollowControl?.enabled?.());
  roundRect(context, 802, 18, 200, 40, 10, 'rgba(3, 14, 21, 0.82)');
  context.fillStyle = headFollowing ? '#72f0b1' : '#ffcf70';
  context.font = 'bold 17px sans-serif';
  context.fillText(headFollowing ? 'B · 头部跟随' : 'B · 头部已锁定', 820, 44);

  navUi.items = navigationItems();
  navUi.selected = Math.max(0, Math.min(navUi.selected, navUi.items.length - 1));
  const mapArea = mapDisplayArea();
  const mapUiScale = Number(mapArea.uiScale || 1.0);
  drawMap(context, mapArea);
  roundRect(
    context,
    mapArea.x + 10 * mapUiScale,
    mapArea.y + 10 * mapUiScale,
    112 * mapUiScale,
    31 * mapUiScale,
    8 * mapUiScale,
    'rgba(4, 18, 27, 0.82)'
  );
  context.fillStyle = '#eafaff';
  context.font = `bold ${Math.round(17 * mapUiScale)}px sans-serif`;
  context.fillText(
    '地图与定位',
    mapArea.x + 21 * mapUiScale,
    mapArea.y + 31 * mapUiScale
  );
  const hasPose = Boolean(navUi.state?.pose);
  roundRect(
    context,
    mapArea.x + 10 * mapUiScale,
    mapArea.y + mapArea.h - 40 * mapUiScale,
    Math.min(mapArea.w - 20 * mapUiScale, 206 * mapUiScale),
    30 * mapUiScale,
    8 * mapUiScale,
    'rgba(4, 18, 27, 0.82)'
  );
  context.fillStyle = navUi.state?.pose_fresh ? '#61f2a7' : '#ffcf70';
  context.font = `${Math.round(17 * mapUiScale)}px sans-serif`;
  context.fillText(
    navUi.state?.pose_fresh
      ? '● 定位实时'
      : (hasPose ? '● 显示最近位置' : '● 等待首次定位'),
    mapArea.x + 20 * mapUiScale,
    mapArea.y + mapArea.h - 19 * mapUiScale
  );

  if (navUi.manualRelocalization) {
    roundRect(context, 846, 88, 330, 248, 17, 'rgba(4, 18, 27, 0.90)');
    context.fillStyle = '#ffd66b';
    context.font = 'bold 24px sans-serif';
    context.fillText('手动重定位', 866, 128);
    context.fillStyle = '#eafaff';
    context.font = 'bold 20px sans-serif';
    context.fillText(
      navUi.manualRelocalization.stage === 'position' ? '① 确定机器人位置' : '② 确定机器人朝向',
      866,
      172
    );
    context.fillStyle = '#b8d9e4';
    context.font = '17px sans-serif';
    if (navUi.manualRelocalization.stage === 'position') {
      context.fillText('右摇杆：移动黄色定位光标', 866, 215);
      context.fillText('A：确认位置', 866, 249);
    } else {
      context.fillText('右摇杆：调整黄色箭头方向', 866, 215);
      context.fillText('A：确认方位并重定位', 866, 249);
    }
    context.fillText('X：取消', 866, 294);
  } else if (navUi.namingActive) {
    drawWaypointNameEditor(context);
  } else if (navUi.menuVisible) {
    roundRect(context, 846, 88, 330, 446, 17, 'rgba(4, 18, 27, 0.88)');
    context.fillStyle = '#eafaff';
    context.font = 'bold 23px sans-serif';
    context.fillText(
      navUi.templateConfirmId
        ? '确认预设流程'
        : (navUi.templateMode
          ? '预设任务流程'
          : (navUi.navigateConfirmName ? '确认导航' : '航点与操作')),
      866,
      126
    );

    const maximumRows = 5;
    const start = Math.max(0, Math.min(
      navUi.selected - Math.floor(maximumRows / 2),
      Math.max(0, navUi.items.length - maximumRows)
    ));
    navUi.items.slice(start, start + maximumRows).forEach((item, offset) => {
      const index = start + offset;
      const y = 147 + offset * 50;
      if (index === navUi.selected) {
        roundRect(context, 860, y, 302, 42, 9, 'rgba(10, 102, 129, 0.94)');
      }
      context.fillStyle = index === navUi.selected ? '#ffffff' : '#bdd0d8';
      context.font = index === navUi.selected ? 'bold 19px sans-serif' : '18px sans-serif';
      const label = item.label.length > 15 ? `${item.label.slice(0, 14)}…` : item.label;
      context.fillText(`${index === navUi.selected ? '› ' : '  '}${label}`, 872, y + 28);
    });
    context.fillStyle = '#68e1ff';
    context.font = '16px sans-serif';
    context.fillText('右摇杆上下选择 · 力度项左右调节', 864, 423);
    context.fillText('按右摇杆键关闭菜单', 864, 452);
    context.fillText('X 全局重定位', 864, 481);
  } else {
    roundRect(context, 891, 91, 284, 43, 11, 'rgba(4, 18, 27, 0.78)');
    context.fillStyle = '#d9f8ff';
    context.font = '18px sans-serif';
    context.fillText('按右摇杆键打开航点菜单', 910, 119);
  }

  const dispatch = navUi.state?.task_dispatch;
  if (dispatch && !['idle', 'cancelled', 'completed'].includes(dispatch.state)) {
    const current = dispatch.current;
    const pending = dispatch.state === 'pending_confirmation';
    const awaiting = dispatch.state === 'awaiting_action';
    const adjusting = dispatch.state === 'adjusting_height';
    const completed = dispatch.state === 'completed';
    const navigationFailed = dispatch.state === 'navigation_failed';
    const failed = dispatch.state === 'failed' || navigationFailed;
    const background = pending
      ? 'rgba(89, 55, 10, 0.94)'
      : (failed ? 'rgba(92, 22, 30, 0.94)' : 'rgba(4, 28, 39, 0.94)');
    roundRect(context, 420, 72, 406, 142, 16, background);
    context.fillStyle = pending ? '#ffd276' : (failed ? '#ff91a7' : '#72f0b1');
    context.font = 'bold 22px sans-serif';
    const title = pending ? '任务分发确认' : (
      completed ? '任务串已完成' : `任务 ${Math.max(1, dispatch.current_index + 1)} / ${dispatch.total}`
    );
    context.fillText(title, 442, 106);
    context.fillStyle = '#eefbff';
    context.font = 'bold 19px sans-serif';
    if (current) {
      context.fillText(`${current.location} · ${current.task}`.slice(0, 23), 442, 140);
      context.fillStyle = '#b8d7e1';
      context.font = '16px sans-serif';
      context.fillText(`身体高度：${current.body_height_level}档`, 442, 168);
    }
    context.fillStyle = pending ? '#ffd276' : '#9edbea';
    context.font = 'bold 16px sans-serif';
    const instruction = pending
      ? `收到 ${dispatch.total} 项任务 · A接受 · 长按X退出`
      : (awaiting ? '长按A完成动作 · 长按X退出任务'
        : (navigationFailed ? '未到达 · 长按A重试 · 长按B跳过 · 长按X退出'
        : (completed ? '任务流程已完成'
          : (adjusting ? '正在确认执行条件 · 长按X退出'
            : '长按X退出当前任务'))));
    context.fillText(String(instruction || '').slice(0, 28), 442, 197);
  }

  const relocalizationState = String(navUi.state?.relocalization?.state || '').toLowerCase();
  const manualRelocalizationRequired = !navUi.state?.pose_fresh
    && ['failed', 'failure', 'aborted', 'rejected'].includes(relocalizationState);
  if (manualRelocalizationRequired) {
    roundRect(context, 398, 226, 492, 54, 13, 'rgba(107, 65, 13, 0.96)');
    context.fillStyle = '#ffe19a';
    context.font = 'bold 17px sans-serif';
    context.fillText('⚠ 自动重定位失败 · 请打开菜单选择“手动重定位”', 417, 259);
  }

  context.fillStyle = '#a9bbc5';
  context.font = '18px sans-serif';
  const navText = navUi.baseControlLatched
    ? '● 底盘已接管（再按左摇杆键取消）'
    : statusText(navUi.state?.navigation, navUi.statusLine);
  roundRect(context, 586, 825, 590, 48, 11, 'rgba(4, 18, 27, 0.79)');
  // roundRect changes fillStyle. Restore the foreground colour so this does
  // not become an unexplained empty blue bar in the lower-right corner.
  context.fillStyle = navUi.baseControlLatched ? '#72f0b1' : '#a9bbc5';
  context.fillText(navText.slice(0, 27), 606, 856);
  const actualFps = navigationCameraFps();
  context.save();
  context.textAlign = 'right';
  context.fillStyle = actualFps >= 20.0
    ? '#68e1ff'
    : (actualFps > 0.0 ? '#ffcf70' : '#a9bbc5');
  context.font = 'bold 17px sans-serif';
  context.fillText(`画面 ${actualFps.toFixed(1)} FPS`, 1155, 856);
  context.restore();
  sendMonitorViewState();
  if (navUi.texture) navUi.texture.needsUpdate = true;
}

async function refreshNavigationState() {
  if (navUi.pollPending) return;
  navUi.pollPending = true;
  try {
    const response = await fetch('/api/navigation/state', { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    navUi.state = await response.json();
    navUi.showroomModeEnabled = navUi.state?.showroom_mode_enabled === true;
    const showroomToggle = document.getElementById('showroomModeToggle');
    if (showroomToggle && !showroomToggle.dataset.pending) {
      showroomToggle.checked = navUi.showroomModeEnabled;
    }
    const relocalizationState = String(
      navUi.state?.relocalization?.state || ''
    ).toLowerCase();
    const manualRelocalizationRequired = !navUi.state?.pose_fresh
      && ['failed', 'failure', 'aborted', 'rejected'].includes(relocalizationState);
    const relocalizationAlertKey = manualRelocalizationRequired
      ? `${relocalizationState}:${navUi.state?.relocalization?.attempt ?? ''}` : '';
    if (manualRelocalizationRequired
        && relocalizationAlertKey !== navUi.relocalizationAlertKey) {
      navUi.relocalizationAlertKey = relocalizationAlertKey;
      navUi.sessionActive = true;
      setNavigationView(true, false);
      navShowNotice(
        '自动重定位失败\n请打开菜单选择“手动重定位”',
        8000,
        '#ffd276'
      );
    } else if (!manualRelocalizationRequired && navUi.state?.pose_fresh) {
      navUi.relocalizationAlertKey = '';
    }
    const task = navUi.state.task_dispatch;
    const taskVersion = Number(task?.version ?? -1);
    if (taskVersion !== navUi.taskVersion) {
      navUi.taskVersion = taskVersion;
      if (task?.state === 'pending_confirmation') {
        navUi.sessionActive = true;
        setNavigationView(true, false);
        navShowNotice(
          `收到 ${task.total} 项任务\n按 A 接受 · 长按 X 1秒拒绝`,
          8000,
          '#ffd276'
        );
      } else if (task?.state === 'awaiting_action') {
        navShowNotice(
          `已确认周围环境安全\n开始执行任务：${task.current?.task || ''}\n动作完成后，长按 A 1秒并松开确认完成动作`,
          5000,
          '#72f0b1'
        );
      } else if (task?.state === 'navigation_failed') {
        navShowNotice(
          `未能到达：${task.current?.location || '当前点位'}\n长按 A 1秒重试当前点位 · 长按 B 1秒跳过`,
          8000,
          '#ff91a7'
        );
      }
    }
    const version = Number(navUi.state.map_version ?? -1);
    if (navUi.state.map_ready && version !== navUi.mapVersion) {
      const mapResponse = await fetch(
        `/api/navigation/map.png?v=${version}&t=${Date.now()}`,
        { cache: 'no-store' }
      );
      if (mapResponse.ok) {
        const bitmap = await createImageBitmap(await mapResponse.blob());
        navUi.mapBitmap?.close?.();
        navUi.mapBitmap = bitmap;
        navUi.mapVersion = version;
      }
    }
    navUi.statusLine = '导航状态已连接';
  } catch (error) {
    navUi.statusLine = `导航状态不可用：${error.message}`;
  } finally {
    navUi.pollPending = false;
    drawNavigationPanel();
  }
}

async function navigationAction(path, payload = {}) {
  if (navUi.actionPending) {
    navShowNotice('上一项操作仍在处理，请稍候', 1800, '#ffcf70');
    return false;
  }
  navUi.actionPending = true;
  const abortController = new AbortController();
  const timeoutMilliseconds = path.includes('/map/switch') ? 15000 : 6500;
  const requestTimeout = window.setTimeout(() => abortController.abort(), timeoutMilliseconds);
  let succeeded = false;
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: abortController.signal
    });
    let result = {};
    try { result = await response.json(); } catch (_) { /* text-only failure */ }
    if (!response.ok || result.success === false) {
      throw new Error(result.message || `HTTP ${response.status}`);
    }
    navShowNotice(result.message || '命令已发送');
    succeeded = true;
  } catch (error) {
    const detail = error.name === 'AbortError'
      ? '操作响应超时，界面已自动解锁，可重新操作'
      : error.message;
    const needsRelocalization = /重定位|定位尚未就绪/.test(String(detail));
    navShowNotice(
      `${needsRelocalization ? '需要手动重定位' : '导航操作失败'}\n${detail}`,
      needsRelocalization ? 7000 : 3800,
      needsRelocalization ? '#ffd276' : '#ff8c8c'
    );
  } finally {
    window.clearTimeout(requestTimeout);
    navUi.actionPending = false;
    if (path.includes('/waypoint/save') || path.includes('/waypoint/delete')) {
      navUi.deleteMode = false;
      navUi.deleteConfirmName = null;
      navUi.navigateConfirmName = null;
      navUi.selected = 0;
    }
    if (path.includes('/map/switch')) {
      navUi.mapMode = false;
      navUi.mapConfirmPath = null;
      navUi.mapConfirmName = null;
      navUi.selected = 0;
    }
    window.setTimeout(refreshNavigationState, 150);
    drawNavigationPanel();
  }
  return succeeded;
}

function suggestedWaypointName() {
  const existing = new Set(
    Array.isArray(navUi.state?.waypoints) ? navUi.state.waypoints : []
  );
  for (let index = 1; index < 1000; index += 1) {
    const candidate = `航点_${String(index).padStart(2, '0')}`;
    if (!existing.has(candidate)) return candidate;
  }
  return '新航点';
}

function requestWaypointNameAndSave() {
  const xrSession = document.querySelector('a-scene')?.renderer?.xr?.getSession?.();
  if (!xrSession || !navUi.visible) {
    navShowNotice('请先进入 VR 导航界面后再保存航点', 3200, '#ffcf70');
    return;
  }
  if (navUi.actionPending) {
    navShowNotice('上一项操作仍在处理，请稍候', 1800, '#ffcf70');
    return;
  }
  navUi.namingActive = true;
  navUi.menuVisible = true;
  navUi.waypointNameDraft = suggestedWaypointName();
  navUi.waypointNameTouched = false;
  navUi.nameKeyIndex = WAYPOINT_NAME_KEYS.length - 1;
  navUi.nameSelectionLatch = false;
  drawNavigationPanel();
  navShowNotice('使用右摇杆和 A 键输入航点名称', 2400, '#68e1ff');
}

function cancelWaypointNameEntry() {
  navUi.namingActive = false;
  navUi.nameSelectionLatch = false;
  drawNavigationPanel();
  navShowNotice('已取消保存航点', 1500, '#ffcf70');
}

function backspaceWaypointName() {
  const characters = Array.from(navUi.waypointNameDraft);
  characters.pop();
  navUi.waypointNameDraft = characters.join('');
  navUi.waypointNameTouched = true;
  drawNavigationPanel();
}

function saveEnteredWaypointName() {
  const name = String(navUi.waypointNameDraft).trim();
  if (!name || name.length > 64 || name.includes('/') || name.includes('\\')) {
    navShowNotice('航点名称不能为空、不能超过64字，也不能包含 / 或 \\', 3500, '#ff8c8c');
    return;
  }
  const existing = Array.isArray(navUi.state?.waypoints) ? navUi.state.waypoints : [];
  if (existing.includes(name)) {
    navShowNotice(`航点“${name}”已经存在，请换一个名称`, 3200, '#ffcf70');
    return;
  }
  navUi.namingActive = false;
  navUi.deleteMode = false;
  navUi.deleteConfirmName = null;
  navUi.navigateConfirmName = null;
  navShowNotice(`正在保存航点：${name}`, 1800, '#68e1ff');
  void navigationAction('/api/navigation/waypoint/save', { name });
}

function activateWaypointNameKey() {
  const key = WAYPOINT_NAME_KEYS[navUi.nameKeyIndex];
  if (!key) return;
  if (key.action === 'backspace') {
    backspaceWaypointName();
  } else if (key.action === 'clear') {
    navUi.waypointNameDraft = '';
    navUi.waypointNameTouched = true;
    drawNavigationPanel();
  } else if (key.action === 'cancel') {
    cancelWaypointNameEntry();
  } else if (key.action === 'save') {
    saveEnteredWaypointName();
  } else if (key.value) {
    if (!navUi.waypointNameTouched) navUi.waypointNameDraft = '';
    navUi.waypointNameTouched = true;
    const candidate = `${navUi.waypointNameDraft}${key.value}`;
    if (Array.from(candidate).length <= 32) navUi.waypointNameDraft = candidate;
    drawNavigationPanel();
  }
}

function moveWaypointNameSelection(horizontal, vertical) {
  const count = WAYPOINT_NAME_KEYS.length;
  const columns = WAYPOINT_NAME_COLUMNS;
  let row = Math.floor(navUi.nameKeyIndex / columns) + vertical;
  let column = navUi.nameKeyIndex % columns + horizontal;
  const rows = Math.ceil(count / columns);
  row = Math.max(0, Math.min(rows - 1, row));
  column = Math.max(0, Math.min(columns - 1, column));
  let next = row * columns + column;
  while (next >= count && column > 0) {
    column -= 1;
    next = row * columns + column;
  }
  navUi.nameKeyIndex = Math.max(0, Math.min(count - 1, next));
  drawNavigationPanel();
}

function activateNavigationItem() {
  const item = navUi.items[navUi.selected];
  if (!item) return;
  if (item.kind === 'stop') {
    void navigationAction('/api/navigation/stop');
  } else if (item.kind === 'relocalize') {
    void navigationAction('/api/navigation/relocalize');
  } else if (item.kind === 'manual-relocalize') {
    startManualRelocalization();
  } else if (item.kind === 'map-library') {
    const maps = Array.isArray(navUi.state?.maps) ? navUi.state.maps : [];
    if (!maps.length) {
      navShowNotice('没有找到可加载的地图', 2600, '#ffcf70');
      return;
    }
    navUi.mapMode = true;
    navUi.mapConfirmPath = null;
    navUi.mapConfirmName = null;
    navUi.deleteMode = false;
    navUi.navigateConfirmName = null;
    navUi.selected = 0;
    drawNavigationPanel();
  } else if (item.kind === 'task-templates') {
    const templates = Array.isArray(navUi.state?.task_templates)
      ? navUi.state.task_templates : [];
    if (!templates.length) {
      navShowNotice('config 中没有可用的预设任务流程', 2800, '#ffcf70');
      return;
    }
    navUi.templateMode = true;
    navUi.templateConfirmId = null;
    navUi.templateConfirmName = null;
    navUi.selected = 0;
    drawNavigationPanel();
  } else if (item.kind === 'template-candidate') {
    navUi.templateConfirmId = item.id;
    navUi.templateConfirmName = item.name;
    navUi.selected = 1;
    navShowNotice(`再次按 A 启动任务流程：${item.name}`, 3200, '#ffcf70');
    drawNavigationPanel();
  } else if (item.kind === 'cancel-template') {
    if (navUi.templateConfirmId) {
      navUi.templateConfirmId = null;
      navUi.templateConfirmName = null;
    } else {
      navUi.templateMode = false;
    }
    navUi.selected = 0;
    drawNavigationPanel();
  } else if (item.kind === 'confirm-template') {
    const identifier = item.id;
    const name = item.name;
    navUi.templateMode = false;
    navUi.templateConfirmId = null;
    navUi.templateConfirmName = null;
    navUi.selected = 0;
    navShowNotice(`正在启动预设流程：${name}`, 2400, '#68e1ff');
    void navigationAction('/api/tasks/template/start', { id: identifier });
  } else if (item.kind === 'gripper-force') {
    navShowNotice('右摇杆左右调节夹爪力度', 1800, '#68e1ff');
  } else if (item.kind === 'map-candidate') {
    if (item.active) {
      navShowNotice(`当前已加载地图：${item.name}`, 2200, '#72f0b1');
      return;
    }
    navUi.mapConfirmPath = item.path;
    navUi.mapConfirmName = item.name;
    navUi.selected = 1;
    navShowNotice(`再次按 A 确认切换地图：${item.name}`, 3200, '#ffcf70');
    drawNavigationPanel();
  } else if (item.kind === 'cancel-map') {
    if (navUi.mapConfirmPath) {
      navUi.mapConfirmPath = null;
      navUi.mapConfirmName = null;
      navUi.selected = 0;
    } else {
      navUi.mapMode = false;
      navUi.selected = 0;
    }
    drawNavigationPanel();
  } else if (item.kind === 'confirm-map') {
    const path = item.path;
    const name = item.name;
    navShowNotice(`正在切换地图并加载航点库：${name}`, 2600, '#68e1ff');
    void navigationAction('/api/navigation/map/switch', { path });
  } else if (item.kind === 'save-waypoint') {
    requestWaypointNameAndSave();
  } else if (item.kind === 'delete-waypoint') {
    const waypoints = Array.isArray(navUi.state?.waypoints) ? navUi.state.waypoints : [];
    if (!waypoints.length) {
      navShowNotice('当前地图还没有可删除的航点', 2600, '#ffcf70');
      return;
    }
    navUi.deleteMode = true;
    navUi.deleteConfirmName = null;
    navUi.navigateConfirmName = null;
    navUi.selected = 0;
    drawNavigationPanel();
  } else if (item.kind === 'delete-candidate') {
    navUi.deleteConfirmName = item.name;
    navUi.selected = 0;
    drawNavigationPanel();
  } else if (item.kind === 'cancel-delete') {
    navUi.deleteMode = false;
    navUi.deleteConfirmName = null;
    navUi.selected = 0;
    drawNavigationPanel();
  } else if (item.kind === 'cancel-navigation') {
    navUi.navigateConfirmName = null;
    navUi.selected = 0;
    drawNavigationPanel();
  } else if (item.kind === 'confirm-navigation') {
    const name = item.name;
    navUi.navigateConfirmName = null;
    navUi.selected = 0;
    if (navUi.showroomModeEnabled) {
      navShowNotice(`展厅模式：正在启动 ${name} 的单点任务`, 2400, '#68e1ff');
      void navigationAction('/api/tasks/showroom-waypoint/start', { name });
    } else {
      navShowNotice(`正在前往：${name}`);
      void navigationAction('/api/navigation/waypoint', { name });
    }
  } else if (item.kind === 'confirm-delete') {
    const name = item.name;
    navUi.deleteMode = false;
    navUi.deleteConfirmName = null;
    navUi.selected = 0;
    void navigationAction('/api/navigation/waypoint/delete', { name });
  } else if (item.kind === 'waypoint') {
    navUi.navigateConfirmName = item.name;
    navUi.deleteMode = false;
    navUi.deleteConfirmName = null;
    // Put the explicit confirmation under the cursor so a deliberate second
    // A press starts navigation; cancel remains one row above.
    navUi.selected = 1;
    navShowNotice(`再次按 A 确认导航至：${item.name}`, 3200, '#ffcf70');
    drawNavigationPanel();
  }
}

function gamepadForHand(session, hand) {
  for (const source of session?.inputSources || []) {
    if (source.handedness === hand && source.gamepad) return source.gamepad;
  }
  return null;
}

AFRAME.registerComponent('autolife-vr-navigation', {
  init: function () {
    createNavigationPanel();
    // The video plane shares the exact camera texture used by normal RGB mode.
    // The UI canvas is independent, so video frames never redraw menu text.
    globalThis.addEventListener('autolife-camera-frame', () => {
      recordNavigationCameraFrame();
      if (navUi.visible) bindNavigationVideoTexture();
    });
    connectNavigationWebsocket();
    ensureDriveWorker();
    void refreshNavigationState();
    if (!navUi.pollTimer) {
      navUi.pollTimer = window.setInterval(refreshNavigationState, 250);
    }
    this.el.renderer.xr.addEventListener('sessionend', () => {
      setBaseControlLatched(false, false);
      sendBaseDrive(false);
      navUi.sessionActive = false;
      navUi.visible = false;
      navUi.menuVisible = false;
      navUi.namingActive = false;
      navUi.mapMode = false;
      navUi.mapConfirmPath = null;
      navUi.mapConfirmName = null;
      navUi.manualRelocalization = null;
      globalThis.autolifeNavigationCameraPreview = false;
      navUi.panel?.setAttribute('visible', 'false');
      navUi.videoPanel?.setAttribute('visible', 'false');
      if (navUi.depthRoot) navUi.depthRoot.visible = false;
      navUi.backdropPanel?.setAttribute('visible', 'false');
      navUi.corridorPanel?.setAttribute('visible', 'false');
      disconnectDepthCloud();
      sendBaseDrive(false);
    });
    this.el.renderer.xr.addEventListener('sessionstart', () => {
      navUi.sessionActive = true;
      navUi.cameraMode = 'rgbd';
      window.setTimeout(() => setNavigationView(true, false), 80);
    });
  },

  tick: function (time, delta) {
    // Consume at most the newest sample inside A-Frame's WebXR loop. This is
    // also a fallback for Quest Chromium versions that suspend window RAF.
    if (navUi.depthPendingFrame) renderPendingDepthCloudFrame();
    if (navUi.visible && isPointCloudMode()) {
      const depthNow = performance.now();
      applyDepthCloudDisplayState(depthNow);
      if (depthNow - navUi.depthWatchdogAt >= 500) {
        navUi.depthWatchdogAt = depthNow;
        const reference = navUi.depthLastPacketAt || navUi.depthConnectedAt;
        if (navUi.depthOnline && reference > 0 && depthNow - reference > 5000) {
          navUi.depthLastError = 'depth stream stalled; reconnecting';
          const socket = navUi.depthSocket;
          if (socket && socket.readyState < WebSocket.CLOSING) {
            socket.close(1012, 'depth stream stalled');
          }
        }
      }
    }
    if (!navUi.textureBound) navUi.ensureTexture?.();
    if (navUi.visible) bindNavigationVideoTexture();
    const session = this.el.renderer.xr.getSession();
    if (!session) return;
    const left = gamepadForHand(session, 'left');
    const right = gamepadForHand(session, 'right');
    if (!left || !right) {
      setBaseControlLatched(false, false);
      sendBaseDrive(false);
      return;
    }
    const leftButtons = left.buttons || [];
    const rightButtons = right.buttons || [];
    const yPressed = Boolean(leftButtons[5]?.pressed);
    const xPressed = Boolean(leftButtons[4]?.pressed);
    const aPressed = Boolean(rightButtons[4]?.pressed);
    const bPressed = Boolean(rightButtons[5]?.pressed);
    const taskState = String(navUi.state?.task_dispatch?.state || 'idle');
    const pendingTaskDecision = taskState === 'pending_confirmation';
    const taskDecisionKey = pendingTaskDecision
      ? `${navUi.state?.task_dispatch?.version ?? ''}:${navUi.state?.task_dispatch?.updated_at ?? ''}`
      : '';
    if (taskDecisionKey !== navUi.taskDecisionKey) {
      navUi.taskDecisionKey = taskDecisionKey;
      // A task may arrive while the browser still remembers A as pressed.
      // Require one observed release before accepting it, then use an
      // independent latch instead of the fragile one-frame global edge.
      navUi.taskAcceptArmed = pendingTaskDecision && !aPressed;
      navUi.taskAcceptPending = false;
    }
    if (pendingTaskDecision && !aPressed && !xPressed) {
      navUi.taskAcceptArmed = true;
    }
    if (!pendingTaskDecision) {
      navUi.taskAcceptArmed = false;
      navUi.taskAcceptPending = false;
    }
    const taskCanExit = !['idle', 'cancelled', 'completed'].includes(taskState);
    const taskExitGestureEnabled = taskCanExit && !navUi.baseControlLatched;
    let taskAConsumed = false;
    // X+A is the guarded quick-reset chord.  It must take priority over the
    // X/Y body-height controls during the whole hold interval, not only after
    // the mapper's one-second gesture timer has fired.
    const quickResetChordPressed = xPressed && aPressed;
    if (quickResetChordPressed && !(navUi.previous.x && navUi.previous.a)) {
      // Lock before the mapper's one-second reset gesture is accepted. This
      // also makes the behavior effective for a controller started before the
      // new control-boundary parameter was loaded.
      const headControl = globalThis.autolifeHeadFollowControl;
      if (headControl?.enabled?.()) void headControl.setEnabled(false);
    }
    if (quickResetChordPressed) {
      navUi.quickResetHeightSuppressed = true;
    } else if (!xPressed && !aPressed) {
      // Keep suppression latched through an uneven button release. Height
      // control becomes eligible again only after the whole chord is up.
      navUi.quickResetHeightSuppressed = false;
    }
    // Long X is the one consistent mission-exit gesture for speech, card,
    // template and showroom tasks.  A short X remains global relocalization
    // only when no mission is active.  While chassis takeover is latched, X
    // keeps its body-height meaning and cannot accidentally cancel a mission.
    if (taskExitGestureEnabled) {
      if (quickResetChordPressed || aPressed) {
        navUi.taskRejectCancelled = true;
      } else if (xPressed && !navUi.previous.x) {
        navUi.taskRejectHoldStartedAt = performance.now();
        navUi.taskRejectLatched = false;
        navUi.taskRejectCancelled = false;
      }
      if (xPressed && !aPressed && !quickResetChordPressed) {
        const rejectHeldMs = performance.now() - navUi.taskRejectHoldStartedAt;
        if (rejectHeldMs >= 1000 && !navUi.taskRejectLatched
            && !navUi.taskRejectCancelled) {
          navUi.taskRejectLatched = true;
          navShowNotice('松开 X 确认退出当前任务', 2600, '#ffcf70');
        }
      } else if (!xPressed && navUi.previous.x) {
        if (navUi.taskRejectLatched && !navUi.taskRejectCancelled
            && !aPressed) {
          navShowNotice('当前任务已退出，导航正在停止…', 2600, '#ffcf70');
          void navigationAction('/api/tasks/cancel');
        }
        navUi.taskRejectHoldStartedAt = 0;
        navUi.taskRejectLatched = false;
        navUi.taskRejectCancelled = false;
      }
    } else {
      navUi.taskRejectHoldStartedAt = 0;
      navUi.taskRejectLatched = false;
      navUi.taskRejectCancelled = false;
    }
    if (pendingTaskDecision && aPressed && !xPressed
        && navUi.taskAcceptArmed && !navUi.taskAcceptPending) {
      taskAConsumed = true;
      navUi.taskAcceptArmed = false;
      navUi.taskAcceptPending = true;
      navShowNotice('正在确认任务分发…', 1800, '#68e1ff');
      void navigationAction('/api/tasks/confirm').finally(() => {
        navUi.taskAcceptPending = false;
      });
    }
    const taskHoldMode = taskState === 'awaiting_action'
      ? 'complete' : (taskState === 'navigation_failed' ? 'retry-navigation-failure' : '');
    if (taskHoldMode) {
      taskAConsumed = aPressed || navUi.previous.a;
      if (aPressed && !navUi.previous.a) {
        navUi.taskHoldStartedAt = performance.now();
        navUi.taskHoldMode = taskHoldMode;
        navUi.taskCompleteLatched = false;
        navUi.taskHoldCancelled = xPressed;
      }
      if (aPressed) {
        if (xPressed || navUi.taskHoldMode !== taskHoldMode) {
          navUi.taskHoldCancelled = true;
        }
        const heldMs = performance.now() - navUi.taskHoldStartedAt;
        if (heldMs >= 1000 && !navUi.taskCompleteLatched
            && !navUi.taskHoldCancelled) {
          navUi.taskCompleteLatched = true;
          navShowNotice(
            taskHoldMode === 'complete'
              ? '松开 A 确认“动作已完成”'
              : '松开 A 重新前往当前未到达点位',
            2400,
            taskHoldMode === 'complete' ? '#72f0b1' : '#ffcf70'
          );
        }
      } else if (navUi.previous.a) {
        if (navUi.taskCompleteLatched && !navUi.taskHoldCancelled && !xPressed) {
          const path = navUi.taskHoldMode === 'complete'
            ? '/api/tasks/complete' : '/api/tasks/retry-navigation-failure';
          void navigationAction(path);
        }
        navUi.taskHoldStartedAt = 0;
        navUi.taskHoldMode = '';
        navUi.taskCompleteLatched = false;
        navUi.taskHoldCancelled = false;
      }
    } else {
      navUi.taskHoldStartedAt = 0;
      navUi.taskHoldMode = '';
      navUi.taskCompleteLatched = false;
      navUi.taskHoldCancelled = false;
    }
    if (taskState === 'navigation_failed') {
      if (bPressed && !navUi.previous.b) {
        navUi.taskSkipHoldStartedAt = performance.now();
        navUi.taskSkipLatched = false;
        navUi.taskSkipCancelled = aPressed || xPressed;
      }
      if (bPressed) {
        if (aPressed || xPressed) navUi.taskSkipCancelled = true;
        const heldMs = performance.now() - navUi.taskSkipHoldStartedAt;
        if (heldMs >= 1000 && !navUi.taskSkipLatched
            && !navUi.taskSkipCancelled) {
          navUi.taskSkipLatched = true;
          navShowNotice('松开 B 跳过当前点位并前往下一任务', 2400, '#ffcf70');
        }
      } else if (navUi.previous.b) {
        if (navUi.taskSkipLatched && !navUi.taskSkipCancelled
            && !aPressed && !xPressed) {
          void navigationAction('/api/tasks/skip-navigation-failure');
        } else if (!navUi.taskSkipCancelled) {
          // Preserve the original short-B head-motion toggle.
          toggleHeadMotionLock();
        }
        navUi.taskSkipHoldStartedAt = 0;
        navUi.taskSkipLatched = false;
        navUi.taskSkipCancelled = false;
      }
    } else {
      navUi.taskSkipHoldStartedAt = 0;
      navUi.taskSkipLatched = false;
      navUi.taskSkipCancelled = false;
      if (bPressed && !navUi.previous.b) toggleHeadMotionLock();
    }
    if (!navUi.baseControlLatched && yPressed && !navUi.previous.y) {
      if (navUi.manualRelocalization) cancelManualRelocalization();
      else if (navUi.namingActive) cancelWaypointNameEntry();
      else toggleNavigationSession();
    }

    const leftAxes = left.axes || [];
    const rightAxes = right.axes || [];
    const leftStickPressed = Boolean(
      leftButtons[3]?.pressed || (!leftButtons[3] && leftButtons[2]?.pressed)
    );
    const rightStickPressed = Boolean(
      rightButtons[3]?.pressed || (!rightButtons[3] && rightButtons[2]?.pressed)
    );
    const bothStickButtonsPressed = leftStickPressed && rightStickPressed;
    if (bothStickButtonsPressed) {
      setBaseControlLatched(false, false);
    } else {
      if (!navUi.namingActive && !navUi.manualRelocalization
          && leftStickPressed && !navUi.previous.leftStick) {
        setBaseControlLatched(!navUi.baseControlLatched);
      }
      if (
        navUi.visible
        && !navUi.namingActive
        && !navUi.manualRelocalization
        && !navUi.baseControlLatched
        && rightStickPressed
        && !navUi.previous.rightStick
      ) {
        navUi.menuVisible = !navUi.menuVisible;
        navUi.selectionLatch = false;
        navShowNotice(
          navUi.menuVisible ? '航点菜单已打开' : '航点菜单已收起',
          1500,
          '#68e1ff'
        );
        drawNavigationPanel();
      }
    }
    const deadman = Boolean(navUi.baseControlLatched);
    let bodyHeightDirection = 0;
    if (navUi.quickResetHeightSuppressed) {
      // Release height ownership immediately.  The drive worker is
      // latest-frame-only, so this replaces any queued lowering sample.
      navUi.bodyHeightEngaged = false;
    } else if (deadman && !pendingTaskDecision && xPressed !== yPressed) {
      // X lowers the body and Y raises it. The height profile stays engaged
      // until chassis takeover is cancelled, so releasing a key holds height.
      bodyHeightDirection = xPressed ? 1 : -1;
      navUi.bodyHeightEngaged = true;
    }
    if (deadman) {
      sendBaseDrive(
        true,
        -(leftAxes[3] || 0),
        -(leftAxes[2] || 0),
        -(rightAxes[2] || 0),
        navUi.bodyHeightEngaged,
        bodyHeightDirection
      );
      navUi.driveInputWasActive = true;
    } else if (navUi.driveInputWasActive) {
      sendBaseDrive(false);
      navUi.driveInputWasActive = false;
    }

    if (navUi.visible && !deadman) {
      if (navUi.manualRelocalization) {
        updateManualRelocalization(rightAxes[2] || 0, rightAxes[3] || 0, delta);
        if (aPressed && !navUi.previous.a && !xPressed && !taskAConsumed) {
          void confirmManualRelocalization();
        }
        if (xPressed && !navUi.previous.x && !aPressed) {
          cancelManualRelocalization();
        }
      } else if (navUi.namingActive) {
        const horizontal = rightAxes[2] || 0;
        const vertical = rightAxes[3] || 0;
        if (Math.abs(horizontal) < 0.35 && Math.abs(vertical) < 0.35) {
          navUi.nameSelectionLatch = false;
        }
        if (
          !navUi.nameSelectionLatch
          && Math.max(Math.abs(horizontal), Math.abs(vertical)) > 0.68
        ) {
          if (Math.abs(horizontal) > Math.abs(vertical)) {
            moveWaypointNameSelection(horizontal > 0 ? 1 : -1, 0);
          } else {
            moveWaypointNameSelection(0, vertical > 0 ? 1 : -1);
          }
          navUi.nameSelectionLatch = true;
        }
        if (aPressed && !navUi.previous.a && !xPressed && !taskAConsumed) activateWaypointNameKey();
        if (xPressed && !navUi.previous.x && !aPressed) backspaceWaypointName();
      } else if (navUi.menuVisible) {
        const horizontalAxis = rightAxes[2] || 0;
        const selectionAxis = rightAxes[3] || 0;
        const selectedItem = navUi.items[navUi.selected];
        if (Math.abs(horizontalAxis) < 0.35) navUi.forceAdjustLatch = false;
        if (
          selectedItem?.kind === 'gripper-force'
          && !navUi.forceAdjustLatch
          && Math.abs(horizontalAxis) > 0.68
          && Math.abs(horizontalAxis) > Math.abs(selectionAxis)
        ) {
          adjustGripperForce(horizontalAxis > 0 ? 1 : -1);
          navUi.forceAdjustLatch = true;
        }
        if (Math.abs(selectionAxis) < 0.35) navUi.selectionLatch = false;
        if (
          !navUi.selectionLatch
          && Math.abs(selectionAxis) > 0.68
          && Math.abs(selectionAxis) >= Math.abs(horizontalAxis)
        ) {
          const direction = selectionAxis > 0 ? 1 : -1;
          navUi.selected = Math.max(
            0, Math.min(navUi.items.length - 1, navUi.selected + direction)
          );
          navUi.selectionLatch = true;
          drawNavigationPanel();
        }
        if (aPressed && !navUi.previous.a && !xPressed && !taskAConsumed) activateNavigationItem();
      }
      if (!navUi.manualRelocalization
          && !navUi.namingActive
          && !taskCanExit
          && xPressed && !navUi.previous.x && !aPressed) {
        void navigationAction('/api/navigation/relocalize');
      }
    }
    navUi.previous = {
      y: yPressed,
      a: aPressed,
      b: bPressed,
      x: xPressed,
      leftStick: leftStickPressed,
      rightStick: rightStickPressed
    };
  }
});

document.addEventListener('DOMContentLoaded', async () => {
  try {
    const config = await fetch('/api/config', { cache: 'no-store' }).then(response => response.json());
    scheduleNavigationVersionCheck(config.navigation?.ui_version);
    navUi.enabled = config.navigation?.enabled === true;
    navUi.precisionDepthEnabled = config.navigation?.precision_cloud?.enabled === true;
    navUi.depthMinimumM = Number(
      config.navigation?.precision_cloud?.minimum_m ?? 0.20
    );
    navUi.depthMaximumM = Number(
      config.navigation?.precision_cloud?.maximum_m ?? 5.0
    );
    navUi.gripperForceMinimumRatio = Number(
      config.navigation?.gripper_force_minimum_ratio ?? 0.2
    );
    navUi.gripperForceStep = Number(config.navigation?.gripper_force_step ?? 0.1);
    navUi.gripperForceMaximumCurrent = Number(
      config.navigation?.gripper_force_maximum_current ?? 10.0
    );
    navUi.showroomModeEnabled = config.navigation?.showroom_mode_enabled === true;
    const savedForceRaw = window.localStorage.getItem(
      'autolife-vr-gripper-force-ratio'
    );
    const savedForce = savedForceRaw === null ? Number.NaN : Number(savedForceRaw);
    navUi.gripperForceRatio = Number.isFinite(savedForce)
      ? Math.max(navUi.gripperForceMinimumRatio, Math.min(1.0, savedForce))
      : Number(config.navigation?.gripper_force_ratio ?? 1.0);
  } catch (error) {
    console.warn('Navigation configuration unavailable:', error);
  }
  if (!navUi.enabled) return;
  const showroomToggle = document.getElementById('showroomModeToggle');
  if (showroomToggle) {
    showroomToggle.checked = navUi.showroomModeEnabled;
    showroomToggle.disabled = false;
    showroomToggle.addEventListener('change', async () => {
      const requested = showroomToggle.checked;
      showroomToggle.dataset.pending = '1';
      showroomToggle.disabled = true;
      try {
        const response = await fetch('/api/navigation/showroom-mode', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: requested })
        });
        const result = await response.json();
        if (!response.ok || !result.success) {
          throw new Error(result.message || `HTTP ${response.status}`);
        }
        navUi.showroomModeEnabled = result.enabled === true;
        showroomToggle.checked = navUi.showroomModeEnabled;
        navShowNotice(result.message || '展厅模式已更新', 2200, '#72f0b1');
      } catch (error) {
        showroomToggle.checked = navUi.showroomModeEnabled;
        navShowNotice(`展厅模式切换失败：${error.message || error}`, 2800, '#ff8b8b');
      } finally {
        delete showroomToggle.dataset.pending;
        showroomToggle.disabled = false;
        window.setTimeout(refreshNavigationState, 80);
      }
    });
  }
  const scene = document.querySelector('a-scene');
  const attach = () => scene.setAttribute('autolife-vr-navigation', '');
  if (scene.hasLoaded) attach();
  else scene.addEventListener('loaded', attach, { once: true });
});
