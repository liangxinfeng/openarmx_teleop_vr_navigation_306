/* Read-only monitor: reproduce the VR 1200x900 HUD locally, never restream it. */

const DESIGN_WIDTH = 1200;
const DESIGN_HEIGHT = 900;
const canvas = document.getElementById('monitor-canvas');
const context = canvas.getContext('2d', { alpha: false, desynchronized: true });
const loading = document.getElementById('loading');
const monitor = {
  running: true, image: null, imageFrameId: -1, imageFrames: 0, imageFps: 0,
  fpsStartedAt: performance.now(), map: null, mapVersion: -1,
  navigation: null, vr: null, status: null, corridor: null,
  corridorReceivedAt: 0, websocket: null, reconnectTimer: null, lastError: ''
};

function sleep(milliseconds) {
  return new Promise(resolve => window.setTimeout(resolve, milliseconds));
}

function resizeCanvas() {
  const ratio = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
  const width = Math.max(640, Math.round(window.innerWidth * ratio));
  const height = Math.max(360, Math.round(window.innerHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
}

function fitRect(sourceWidth, sourceHeight, area) {
  const scale = Math.min(area.w / sourceWidth, area.h / sourceHeight);
  const w = sourceWidth * scale;
  const h = sourceHeight * scale;
  return { x: area.x + (area.w - w) / 2, y: area.y + (area.h - h) / 2, w, h };
}

function designRect() {
  return fitRect(DESIGN_WIDTH, DESIGN_HEIGHT, { x: 0, y: 0, w: canvas.width, h: canvas.height });
}

function roundRect(ctx, x, y, width, height, radius, fillStyle) {
  const r = Math.max(0, Math.min(radius, width / 2, height / 2));
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + r);
  ctx.lineTo(x + width, y + height - r);
  ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  ctx.lineTo(x + r, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
  ctx.fillStyle = fillStyle;
  ctx.fill();
}

function withDesignTransform(rect, callback) {
  context.save();
  context.translate(rect.x, rect.y);
  context.scale(rect.w / DESIGN_WIDTH, rect.h / DESIGN_HEIGHT);
  callback();
  context.restore();
}

function drawCamera(rect) {
  context.fillStyle = '#000';
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = '#101820';
  context.fillRect(rect.x, rect.y, rect.w, rect.h);
  if (monitor.image) {
    context.drawImage(monitor.image, rect.x, rect.y, rect.w, rect.h);
    loading.style.display = 'none';
  }
}

function drawPolyline(points, colour, width) {
  let started = false;
  context.beginPath();
  for (const point of Array.isArray(points) ? points : []) {
    const x = Number(point?.[0]) * DESIGN_WIDTH;
    const y = Number(point?.[1]) * DESIGN_HEIGHT;
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    if (!started) context.moveTo(x, y); else context.lineTo(x, y);
    started = true;
  }
  if (!started) return;
  context.strokeStyle = colour;
  context.lineWidth = width;
  context.lineCap = 'round';
  context.lineJoin = 'round';
  context.stroke();
}

function drawDirectionalObstacleDistances(obstacles) {
  if (!obstacles?.valid || !obstacles.directions) return;
  const positions = {
    front: [0.50, 0.10], front_left: [0.23, 0.17], left: [0.10, 0.47],
    rear_left: [0.23, 0.86], rear: [0.50, 0.92], rear_right: [0.77, 0.86],
    right: [0.90, 0.47], front_right: [0.77, 0.17]
  };
  const labels = {
    front: '前', front_left: '左前', left: '左', rear_left: '左后',
    rear: '后', rear_right: '右后', right: '右', front_right: '右前'
  };
  const colours = { green: '#64e8a2', yellow: '#ffd166', red: '#ff625d' };
  context.save();
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  for (const [key, position] of Object.entries(positions)) {
    const item = obstacles.directions[key];
    const distance = Number(item?.distance_m);
    if (!Number.isFinite(distance) || distance < 0 || distance > 1.0) continue;
    const x = position[0] * DESIGN_WIDTH;
    const y = position[1] * DESIGN_HEIGHT;
    const colour = colours[item.level] || colours.green;
    const text = `${labels[key]} ${distance.toFixed(2)}m`;
    context.font = 'bold 19px sans-serif';
    const width = Math.max(76, context.measureText(text).width + 24);
    roundRect(context, x - width / 2, y - 15, width, 30, 11, 'rgba(3,14,21,0.58)');
    context.strokeStyle = colour;
    context.lineWidth = item.level === 'red' ? 2.0 : 1.2;
    context.strokeRect(x - width / 2 + 1, y - 14, width - 2, 28);
    context.fillStyle = colour;
    context.fillText(text, x, y + 1);
  }
  context.restore();
}

function drawCorridor() {
  const payload = monitor.corridor;
  if (!payload || performance.now() - monitor.corridorReceivedAt > 450) return;
  drawDirectionalObstacleDistances(payload.obstacles);
  if (!payload.valid) return;
  drawPolyline(payload.left, 'rgba(92,240,198,0.76)', 2.2);
  drawPolyline(payload.right, 'rgba(92,240,198,0.76)', 2.2);
  context.setLineDash([10, 12]);
  drawPolyline(payload.centre, 'rgba(225,255,247,0.42)', 1.2);
  context.setLineDash([]);
}

function mapPoint(meta, rect, pose) {
  if (!meta || !pose) return null;
  const dx = Number(pose.x) - Number(meta.origin_x);
  const dy = Number(pose.y) - Number(meta.origin_y);
  const yaw = Number(meta.origin_yaw || 0);
  const cosine = Math.cos(yaw);
  const sine = Math.sin(yaw);
  const mapX = (cosine * dx + sine * dy) / Number(meta.resolution);
  const mapY = (-sine * dx + cosine * dy) / Number(meta.resolution);
  return {
    x: rect.x + mapX * rect.scale,
    y: rect.y + (Number(meta.height) - mapY) * rect.scale,
    heading: Number(pose.yaw || 0) - yaw
  };
}

function drawPoseArrow(point, size, fill, stroke, ring = false) {
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

function mapDisplayArea() {
  const maximumWidth = 540;
  const maximumHeight = 380;
  let aspect = monitor.map?.width > 0 && monitor.map?.height > 0
    ? monitor.map.width / monitor.map.height : 1.45;
  if (!Number.isFinite(aspect) || aspect <= 0) aspect = 1.45;
  let width = maximumWidth;
  let height = width / aspect;
  if (height > maximumHeight) { height = maximumHeight; width = height * aspect; }
  return { x: 24, y: 876 - height, w: width, h: height };
}

function waypointPose(name) {
  const poses = Array.isArray(monitor.navigation?.waypoint_poses)
    ? monitor.navigation.waypoint_poses : [];
  return poses.find(pose => pose?.name === name) || null;
}

function drawMap(area, view) {
  roundRect(context, area.x, area.y, area.w, area.h, 18, '#14232b');
  const meta = monitor.navigation?.map;
  if (!monitor.map || !meta) {
    context.fillStyle = '#879aa3';
    context.font = '28px sans-serif';
    context.fillText('等待 /map 地图…', area.x + 75, area.y + area.h / 2);
    return;
  }
  const scale = Math.min(area.w / monitor.map.width, area.h / monitor.map.height);
  const width = monitor.map.width * scale;
  const height = monitor.map.height * scale;
  const left = area.x + (area.w - width) / 2;
  const top = area.y + (area.h - height) / 2;
  context.save();
  roundRect(context, area.x, area.y, area.w, area.h, 14, 'rgba(0,0,0,0)');
  context.clip();
  context.drawImage(monitor.map, left, top, width, height);
  context.restore();
  const mapRect = { x: left, y: top, scale };
  drawPoseArrow(mapPoint(meta, mapRect, waypointPose(view.selected_waypoint)), 18,
    '#ff4fd8', '#fff1fb', true);
  drawPoseArrow(mapPoint(meta, mapRect, view.manual_relocalization),
    view.manual_relocalization?.stage === 'heading' ? 21 : 17,
    '#ffc247', '#fff7d6', true);
  drawPoseArrow(mapPoint(meta, mapRect, monitor.navigation?.pose), 13,
    '#087a45', monitor.navigation?.pose_fresh ? '#d7ffeb' : '#ffcf70');
}

const WAYPOINT_NAME_KEYS = [
  ...['前台', '吧台', '客房', '洗衣房', '充电桩', '门口',
    '厨房', '餐厅', '走廊', '电梯', '房间', '大厅'],
  ...'0123456789_-'.split(''), ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split(''),
  '退格', '清空', '取消', '保存'
];

function drawWaypointNameEditor(view) {
  roundRect(context, 570, 78, 606, 720, 18, 'rgba(4,18,27,0.94)');
  context.fillStyle = '#eafaff'; context.font = 'bold 24px sans-serif';
  context.fillText('输入航点名称', 594, 116);
  roundRect(context, 590, 136, 566, 58, 11, 'rgba(13,46,60,0.96)');
  context.fillStyle = '#fff'; context.font = 'bold 25px sans-serif';
  context.fillText(String(view.naming_draft || '请输入名称').slice(0, 28), 608, 174);
  WAYPOINT_NAME_KEYS.forEach((label, index) => {
    const x = 590 + (index % 6) * 95;
    const y = 214 + Math.floor(index / 6) * 56;
    const selected = index === Number(view.name_key_index);
    roundRect(context, x, y, 86, 48, 8,
      selected ? 'rgba(10,130,165,0.98)' : 'rgba(15,54,69,0.96)');
    context.fillStyle = selected ? '#fff' : '#d7edf4';
    context.font = label.length > 3 ? 'bold 14px sans-serif' : 'bold 17px sans-serif';
    context.fillText(label, x + (86 - context.measureText(label).width) / 2, y + 31);
  });
  context.fillStyle = '#9edbea'; context.font = '16px sans-serif';
  context.fillText('右摇杆选择 · A 输入 · X 退格 · Y 取消', 690, 780);
}

function drawMenu(view) {
  if (view.manual_relocalization) {
    roundRect(context, 846, 88, 330, 248, 17, 'rgba(4,18,27,0.90)');
    context.fillStyle = '#ffd66b'; context.font = 'bold 24px sans-serif';
    context.fillText('手动重定位', 866, 128);
    context.fillStyle = '#eafaff'; context.font = 'bold 20px sans-serif';
    const heading = view.manual_relocalization.stage === 'heading';
    context.fillText(heading ? '② 确定机器人朝向' : '① 确定机器人位置', 866, 172);
    context.fillStyle = '#b8d9e4'; context.font = '17px sans-serif';
    context.fillText(heading ? '右摇杆：调整黄色箭头方向' : '右摇杆：移动黄色定位光标', 866, 215);
    context.fillText(heading ? 'A：确认方位并重定位' : 'A：确认位置', 866, 249);
    context.fillText('X：取消', 866, 294);
    return;
  }
  if (view.naming_active) { drawWaypointNameEditor(view); return; }
  if (!view.menu_visible) {
    roundRect(context, 891, 91, 284, 43, 11, 'rgba(4,18,27,0.78)');
    context.fillStyle = '#d9f8ff'; context.font = '18px sans-serif';
    context.fillText('按右摇杆键打开航点菜单', 910, 119);
    return;
  }
  roundRect(context, 846, 88, 330, 446, 17, 'rgba(4,18,27,0.88)');
  context.fillStyle = '#eafaff'; context.font = 'bold 23px sans-serif';
  context.fillText(String(view.menu_title || '航点与操作'), 866, 126);
  for (const [offset, row] of (view.menu_rows || []).slice(0, 5).entries()) {
    const y = 147 + offset * 50;
    if (row.selected) roundRect(context, 860, y, 302, 42, 9, 'rgba(10,102,129,0.94)');
    context.fillStyle = row.selected ? '#fff' : '#bdd0d8';
    context.font = row.selected ? 'bold 19px sans-serif' : '18px sans-serif';
    const raw = String(row.label || '');
    const label = raw.length > 15 ? `${raw.slice(0, 14)}…` : raw;
    context.fillText(`${row.selected ? '› ' : '  '}${label}`, 872, y + 28);
  }
  context.fillStyle = '#68e1ff'; context.font = '16px sans-serif';
  context.fillText('右摇杆上下选择 · 力度项左右调节', 864, 423);
  context.fillText('按右摇杆键关闭菜单', 864, 452);
  context.fillText('X 全局重定位', 864, 481);
}

function drawTaskDispatch() {
  const dispatch = monitor.navigation?.task_dispatch;
  if (!dispatch || ['idle', 'cancelled', 'completed'].includes(dispatch.state)) return;
  const current = dispatch.current;
  const pending = dispatch.state === 'pending_confirmation';
  const awaiting = dispatch.state === 'awaiting_action';
  const adjusting = dispatch.state === 'adjusting_height';
  const completed = dispatch.state === 'completed';
  const navigationFailed = dispatch.state === 'navigation_failed';
  const failed = dispatch.state === 'failed' || navigationFailed;
  roundRect(context, 420, 72, 406, 142, 16,
    pending ? 'rgba(89,55,10,0.94)'
      : (failed ? 'rgba(92,22,30,0.94)' : 'rgba(4,28,39,0.94)'));
  context.fillStyle = pending ? '#ffd276' : (failed ? '#ff91a7' : '#72f0b1');
  context.font = 'bold 22px sans-serif';
  const title = pending ? '任务分发确认' : (completed ? '任务串已完成'
    : `任务 ${Math.max(1, Number(dispatch.current_index) + 1)} / ${dispatch.total}`);
  context.fillText(title, 442, 106);
  if (current) {
    context.fillStyle = '#eefbff'; context.font = 'bold 19px sans-serif';
    context.fillText(`${current.location} · ${current.task}`.slice(0, 23), 442, 140);
    context.fillStyle = '#b8d7e1'; context.font = '16px sans-serif';
    context.fillText(`身体高度：${current.body_height_level}档`, 442, 168);
  }
  context.fillStyle = pending ? '#ffd276' : '#9edbea';
  context.font = 'bold 16px sans-serif';
  const instruction = pending ? `收到 ${dispatch.total} 项任务 · A接受 · 长按X退出`
    : (awaiting ? '长按A完成动作 · 长按X退出任务'
      : (navigationFailed ? '未到达 · 长按A重试 · 长按B跳过 · 长按X退出'
        : (completed ? '任务流程已完成'
          : (adjusting ? '正在确认执行条件 · 长按X退出' : '长按X退出当前任务'))));
  context.fillText(String(instruction).slice(0, 28), 442, 197);
}

function navigationStatusText(fallback) {
  const payload = monitor.navigation?.navigation;
  if (!payload || typeof payload !== 'object') return fallback;
  return String(payload.detail || payload.message || payload.state || fallback);
}

function drawVrHud() {
  const view = monitor.vr?.view || {};
  if (!view.navigation_visible) return;
  roundRect(context, 18, 14, 188, 50, 12, 'rgba(3,14,21,0.82)');
  context.fillStyle = '#68e1ff'; context.font = 'bold 31px sans-serif';
  context.fillText(String(view.title || '云蝶 V29'), 30, 49);
  roundRect(context, 1015, 18, 161, 40, 10, 'rgba(3,14,21,0.82)');
  context.fillStyle = view.camera_available ? '#72f0b1' : '#ffcf70';
  context.font = 'bold 17px sans-serif';
  context.fillText(view.camera_available ? '● RGB 实时' : '○ RGB 连接中', 1032, 44);
  roundRect(context, 802, 18, 200, 40, 10, 'rgba(3,14,21,0.82)');
  context.fillStyle = view.head_following ? '#72f0b1' : '#ffcf70';
  context.fillText(view.head_following ? 'B · 头部跟随' : 'B · 头部已锁定', 820, 44);

  const mapArea = mapDisplayArea();
  drawMap(mapArea, view);
  roundRect(context, mapArea.x + 10, mapArea.y + 10, 112, 31, 8, 'rgba(4,18,27,0.82)');
  context.fillStyle = '#eafaff'; context.font = 'bold 17px sans-serif';
  context.fillText('地图与定位', mapArea.x + 21, mapArea.y + 31);
  roundRect(context, mapArea.x + 10, mapArea.y + mapArea.h - 40,
    Math.min(mapArea.w - 20, 206), 30, 8, 'rgba(4,18,27,0.82)');
  context.fillStyle = monitor.navigation?.pose_fresh ? '#61f2a7' : '#ffcf70';
  context.font = '17px sans-serif';
  context.fillText(monitor.navigation?.pose_fresh ? '● 定位实时'
    : (monitor.navigation?.pose ? '● 显示最近位置' : '● 等待首次定位'),
  mapArea.x + 20, mapArea.y + mapArea.h - 19);

  drawMenu(view);
  drawTaskDispatch();
  const relocalizationState = String(monitor.navigation?.relocalization?.state || '').toLowerCase();
  if (!monitor.navigation?.pose_fresh
      && ['failed', 'failure', 'aborted', 'rejected'].includes(relocalizationState)) {
    roundRect(context, 398, 226, 492, 54, 13, 'rgba(107,65,13,0.96)');
    context.fillStyle = '#ffe19a'; context.font = 'bold 17px sans-serif';
    context.fillText('⚠ 自动重定位失败 · 请打开菜单选择“手动重定位”', 417, 259);
  }
  roundRect(context, 586, 825, 590, 48, 11, 'rgba(4,18,27,0.79)');
  context.fillStyle = view.base_control_latched ? '#72f0b1' : '#a9bbc5';
  context.font = '18px sans-serif';
  const navText = view.base_control_latched ? '● 底盘已接管（再按左摇杆键取消）'
    : navigationStatusText('导航状态已连接');
  context.fillText(navText.slice(0, 27), 606, 856);
  const actualFps = Number(view.vr_camera_fps || 0);
  context.save(); context.textAlign = 'right';
  context.fillStyle = actualFps >= 20 ? '#68e1ff' : (actualFps > 0 ? '#ffcf70' : '#a9bbc5');
  context.font = 'bold 17px sans-serif';
  context.fillText(`画面 ${actualFps.toFixed(1)} FPS`, 1155, 856);
  context.restore();
}

function render() {
  resizeCanvas();
  const rect = designRect();
  drawCamera(rect);
  withDesignTransform(rect, () => {
    if (monitor.vr?.view?.navigation_visible) drawCorridor();
    drawVrHud();
  });
  window.requestAnimationFrame(render);
}

async function decodeImage(response) {
  const blob = await response.blob();
  if (typeof createImageBitmap === 'function') return createImageBitmap(blob);
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const image = new Image();
    image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
    image.onerror = error => { URL.revokeObjectURL(url); reject(error); };
    image.src = url;
  });
}

async function cameraLoop() {
  while (monitor.running) {
    if (document.hidden) { await sleep(250); continue; }
    const started = performance.now();
    try {
      const response = await fetch(
        `/api/camera/frame.jpg?camera=rgbd_head_color&after=${monitor.imageFrameId}`,
        { cache: 'no-store' }
      );
      if (response.status === 204) continue;
      if (!response.ok) throw new Error(`camera HTTP ${response.status}`);
      const frameId = Number(response.headers.get('X-Frame-Id'));
      const image = await decodeImage(response);
      if (Number.isFinite(frameId) && frameId <= monitor.imageFrameId) image.close?.();
      else {
        const oldImage = monitor.image;
        monitor.image = image;
        monitor.imageFrameId = Number.isFinite(frameId) ? frameId : monitor.imageFrameId + 1;
        monitor.imageFrames += 1;
        oldImage?.close?.();
        const now = performance.now();
        const elapsed = now - monitor.fpsStartedAt;
        if (elapsed >= 1000) {
          monitor.imageFps = 1000 * monitor.imageFrames / elapsed;
          monitor.imageFrames = 0;
          monitor.fpsStartedAt = now;
        }
      }
      monitor.lastError = '';
    } catch (error) {
      monitor.lastError = String(error.message || error);
      await sleep(300);
    }
    // At most 20 FPS on the spectator page; latest frame only, independent of VR.
    const remaining = 50 - (performance.now() - started);
    if (remaining > 0) await sleep(remaining);
  }
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

async function refreshMap(version) {
  if (!Number.isFinite(version) || version === monitor.mapVersion) return;
  const response = await fetch(`/api/navigation/map.png?v=${version}`, { cache: 'no-store' });
  if (!response.ok) return;
  const bitmap = await createImageBitmap(await response.blob());
  monitor.map?.close?.();
  monitor.map = bitmap;
  monitor.mapVersion = version;
}

async function stateLoop() {
  let statusCounter = 0;
  while (monitor.running) {
    try {
      const [navigation, vr] = await Promise.all([
        fetchJson('/api/navigation/state'), fetchJson('/api/monitor/state')
      ]);
      monitor.navigation = navigation;
      monitor.vr = vr;
      await refreshMap(Number(navigation.map_version));
      if (statusCounter % 2 === 0) monitor.status = await fetchJson('/api/status');
      statusCounter += 1;
    } catch (error) { monitor.lastError = String(error.message || error); }
    await sleep(300);
  }
}

function monitorWebsocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/monitor/ws`;
}

function connectMonitorWebsocket() {
  if (monitor.websocket?.readyState === WebSocket.OPEN
      || monitor.websocket?.readyState === WebSocket.CONNECTING) return;
  const socket = new WebSocket(monitorWebsocketUrl());
  monitor.websocket = socket;
  socket.onmessage = event => {
    if (monitor.websocket !== socket) return;
    try {
      const payload = JSON.parse(event.data);
      if (payload?.type !== 'corridor_projection') return;
      monitor.corridor = payload;
      monitor.corridorReceivedAt = performance.now();
    } catch (_error) { /* optional overlay only */ }
  };
  socket.onclose = () => {
    if (monitor.websocket !== socket) return;
    monitor.websocket = null;
    monitor.corridor = null;
    window.clearTimeout(monitor.reconnectTimer);
    monitor.reconnectTimer = window.setTimeout(connectMonitorWebsocket, 1000);
  };
  socket.onerror = () => socket.close();
}

window.addEventListener('resize', resizeCanvas);
window.addEventListener('beforeunload', () => {
  monitor.running = false;
  monitor.websocket?.close();
  monitor.image?.close?.();
  monitor.map?.close?.();
});
resizeCanvas();
connectMonitorWebsocket();
void cameraLoop();
void stateLoop();
window.requestAnimationFrame(render);
