/* Public read-only view. No VR, controller, command or point-cloud links. */

const view = {
  running: true,
  navigation: null,
  map: null,
  mapVersion: -1,
  frameId: -1,
  frame: null,
  frames: 0,
  fpsStartedAt: performance.now(),
  cameraFps: 0,
  lastStateAt: 0,
  lastCameraAt: 0
};

const cameraCanvas = document.getElementById('camera-canvas');
const cameraContext = cameraCanvas.getContext('2d', { alpha: false });
const mapCanvas = document.getElementById('map-canvas');
const mapContext = mapCanvas.getContext('2d');
const $ = id => document.getElementById(id);
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

function taskPhase(dispatch) {
  const state = String(dispatch?.state || 'idle').toLowerCase();
  const current = dispatch?.current || {};
  const location = String(current.location || '目标区域');
  const task = String(current.task || '服务任务');
  const table = {
    pending_confirmation: ['任务链解析完成', `正在生成前往${location}的执行路径`],
    navigating: ['自主导航执行中', `正在前往${location}`],
    adjusting_height: ['执行条件校验中', `正在确认${location}任务环境`],
    awaiting_action: ['自主任务执行中', `${location} · ${task}`],
    navigation_failed: ['自主路径重构中', `正在重新评估前往${location}的路径`],
    completed: ['任务链执行完成', '本轮服务任务已全部完成'],
    cancelled: ['任务流程已结束', '自主系统已返回待命状态'],
    rejected: ['任务流程已结束', '自主系统已返回待命状态'],
    idle: ['自主系统待命', '等待任务链下发']
  };
  return table[state] || ['自主任务处理中', String(dispatch?.message || `${location} · ${task}`)];
}

function navigationText(payload) {
  const state = String(payload?.state || '').toLowerCase();
  const name = String(payload?.active_waypoint || '目标区域');
  if (state.includes('succeeded') || state.includes('arrived')) return `${name}已到达`;
  if (state.includes('navigat') || state.includes('short_distance')) return `正在自主前往${name}`;
  if (state.includes('fail') || state.includes('abort')) return '正在重新计算可行路径';
  return '自主定位与路径系统在线';
}

function renderTasks(dispatch) {
  const timeline = $('task-timeline');
  const tasks = Array.isArray(dispatch?.tasks) ? dispatch.tasks : [];
  const currentIndex = Math.max(0, Number(dispatch?.current_index || 0));
  $('sequence-count').textContent = `${tasks.length} 项任务`;
  if (!tasks.length) {
    timeline.innerHTML = '<li class="empty-task"><span></span><p>等待生成任务路径</p></li>';
    return;
  }
  timeline.replaceChildren(...tasks.map((item, index) => {
    const card = document.createElement('li');
    card.className = `task-card ${index < currentIndex ? 'done' : index === currentIndex ? 'current' : ''}`;
    const node = document.createElement('span');
    node.className = 'task-node';
    node.textContent = index < currentIndex ? '✓' : String(index + 1).padStart(2, '0');
    const copy = document.createElement('div');
    const title = document.createElement('strong');
    title.textContent = String(item.location || '目标区域');
    const detail = document.createElement('p');
    detail.textContent = String(item.task || '服务任务');
    copy.append(title, detail);
    card.append(node, copy);
    return card;
  }));
  timeline.querySelector('.current')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function updateInterface(navigation) {
  const dispatch = navigation?.task_dispatch || {};
  const tasks = Array.isArray(dispatch.tasks) ? dispatch.tasks : [];
  const currentIndex = Math.max(0, Number(dispatch.current_index || 0));
  const current = dispatch.current || tasks[currentIndex] || {};
  const [phase, title] = taskPhase(dispatch);
  $('mission-stage').textContent = phase;
  $('mission-title').textContent = title;
  $('mission-number').textContent = tasks.length ? String(currentIndex + 1).padStart(2, '0') : '00';
  $('mission-location').textContent = current.location ? `当前区域 · ${current.location}` : '当前位置 · 已就绪';
  $('mission-progress').textContent = `任务进度 · ${tasks.length ? Math.min(currentIndex + 1, tasks.length) : 0} / ${tasks.length}`;
  $('active-operation').textContent = current.task ? `正在执行 · ${current.task}` : phase;
  $('navigation-summary').textContent = navigationText(navigation?.navigation);
  $('map-target').textContent = current.location ? `目标区域 · ${current.location}` : '目标区域 · 待命';
  const distance = Number(navigation?.navigation?.distance_remaining);
  $('map-distance').textContent = Number.isFinite(distance) ? `剩余路径 ${distance.toFixed(1)} m` : '路径计算就绪';
  const fresh = navigation?.pose_fresh === true;
  $('localization-state').classList.toggle('online', fresh);
  $('localization-state').innerHTML = `<i></i>${fresh ? '空间定位在线' : '定位状态同步中'}`;
  renderTasks(dispatch);
  drawMap();
}

function mapPoint(meta, pose, rect) {
  if (!meta || !pose) return null;
  const dx = Number(pose.x) - Number(meta.origin_x);
  const dy = Number(pose.y) - Number(meta.origin_y);
  const yaw = Number(meta.origin_yaw || 0);
  const cosine = Math.cos(yaw);
  const sine = Math.sin(yaw);
  const x = (cosine * dx + sine * dy) / Number(meta.resolution);
  const y = (-sine * dx + cosine * dy) / Number(meta.resolution);
  return { x: rect.x + x * rect.scale, y: rect.y + (Number(meta.height) - y) * rect.scale, yaw: Number(pose.yaw || 0) - yaw };
}

function drawArrow(point, size, fill, glow) {
  if (!point) return;
  mapContext.save();
  mapContext.translate(point.x, point.y);
  mapContext.rotate(-point.yaw);
  mapContext.shadowColor = glow;
  mapContext.shadowBlur = 15;
  mapContext.beginPath();
  mapContext.moveTo(size, 0);
  mapContext.lineTo(-size * .72, -size * .62);
  mapContext.lineTo(-size * .35, 0);
  mapContext.lineTo(-size * .72, size * .62);
  mapContext.closePath();
  mapContext.fillStyle = fill;
  mapContext.fill();
  mapContext.restore();
}

function currentTargetPose() {
  const navigation = view.navigation;
  const dispatch = navigation?.task_dispatch || {};
  const target = String(dispatch.current?.location || navigation?.navigation?.active_waypoint || '');
  return (navigation?.waypoint_poses || []).find(item => String(item.name) === target) || null;
}

function fitCanvas(canvas) {
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
}

function drawMap() {
  fitCanvas(mapCanvas);
  const width = mapCanvas.width;
  const height = mapCanvas.height;
  mapContext.clearRect(0, 0, width, height);
  mapContext.fillStyle = '#02080c';
  mapContext.fillRect(0, 0, width, height);
  const meta = view.navigation?.map;
  if (!view.map || !meta) {
    mapContext.fillStyle = '#63808d';
    mapContext.font = `${Math.max(14, width * .026)}px sans-serif`;
    mapContext.fillText('空间地图同步中', width * .34, height * .49);
    return;
  }
  const scale = Math.min(width / view.map.width, height / view.map.height);
  const drawWidth = view.map.width * scale;
  const drawHeight = view.map.height * scale;
  const left = (width - drawWidth) / 2;
  const top = (height - drawHeight) / 2;
  mapContext.globalAlpha = .78;
  mapContext.drawImage(view.map, left, top, drawWidth, drawHeight);
  mapContext.globalAlpha = 1;
  const rect = { x: left, y: top, scale };
  drawArrow(mapPoint(meta, currentTargetPose(), rect), Math.max(8, width * .018), '#ffce72', '#ffce72');
  drawArrow(mapPoint(meta, view.navigation?.pose, rect), Math.max(9, width * .021), '#68e1ff', '#68e1ff');
}

function drawCamera() {
  fitCanvas(cameraCanvas);
  if (!view.frame) return;
  const width = cameraCanvas.width;
  const height = cameraCanvas.height;
  const sourceWidth = view.frame.width || 640;
  const sourceHeight = view.frame.height || 480;
  const scale = Math.max(width / sourceWidth, height / sourceHeight);
  const drawWidth = sourceWidth * scale;
  const drawHeight = sourceHeight * scale;
  cameraContext.drawImage(view.frame, (width - drawWidth) / 2, (height - drawHeight) / 2, drawWidth, drawHeight);
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
  while (view.running) {
    if (document.hidden) { await sleep(350); continue; }
    const started = performance.now();
    try {
      const response = await fetch(`/api/camera/frame.jpg?camera=rgbd_head_color&after=${view.frameId}`, { cache: 'no-store' });
      if (response.status === 204) continue;
      if (!response.ok) throw new Error(`camera ${response.status}`);
      const frameId = Number(response.headers.get('X-Frame-Id'));
      const frame = await decodeImage(response);
      if (Number.isFinite(frameId) && frameId <= view.frameId) frame.close?.();
      else {
        view.frame?.close?.();
        view.frame = frame;
        view.frameId = Number.isFinite(frameId) ? frameId : view.frameId + 1;
        view.lastCameraAt = performance.now();
        view.frames += 1;
        const elapsed = view.lastCameraAt - view.fpsStartedAt;
        if (elapsed >= 1200) {
          view.cameraFps = view.frames * 1000 / elapsed;
          view.frames = 0;
          view.fpsStartedAt = view.lastCameraAt;
        }
        drawCamera();
        $('vision-placeholder').classList.add('hidden');
        $('vision-state').classList.add('online');
        $('vision-state').innerHTML = '<i></i>机器人视觉在线';
      }
    } catch (_error) {
      $('vision-state').classList.remove('online');
      $('vision-state').innerHTML = '<i></i>视觉链路同步中';
      await sleep(500);
    }
    // Public display is intentionally capped at 12 FPS and latest-frame only.
    const remaining = 84 - (performance.now() - started);
    if (remaining > 0) await sleep(remaining);
  }
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

async function refreshMap(version) {
  if (!Number.isFinite(version) || version === view.mapVersion) return;
  const response = await fetch(`/api/navigation/map.png?v=${version}`, { cache: 'no-store' });
  if (!response.ok) return;
  const bitmap = await createImageBitmap(await response.blob());
  view.map?.close?.();
  view.map = bitmap;
  view.mapVersion = version;
  drawMap();
}

async function stateLoop() {
  while (view.running) {
    try {
      const navigation = await fetchJson('/api/navigation/state');
      view.navigation = navigation;
      view.lastStateAt = performance.now();
      await refreshMap(Number(navigation.map_version));
      updateInterface(navigation);
      $('system-pill').classList.add('online');
      $('system-pill').innerHTML = '<span></span><b>自主系统在线</b>';
      $('link-summary').textContent = 'AUTONOMOUS LINK · ONLINE';
    } catch (_error) {
      $('system-pill').classList.remove('online');
      $('system-pill').innerHTML = '<span></span><b>系统状态同步中</b>';
      $('link-summary').textContent = 'AUTONOMOUS LINK · SYNCING';
    }
    await sleep(500);
  }
}

function updateClock() {
  $('clock').textContent = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  if (view.frame) drawCamera();
  drawMap();
}

$('fullscreen-button').addEventListener('click', async () => {
  if (!document.fullscreenElement) await document.documentElement.requestFullscreen?.();
  else await document.exitFullscreen?.();
});
document.addEventListener('fullscreenchange', () => {
  $('fullscreen-button').textContent = document.fullscreenElement ? '退出全屏' : '全屏展示';
});
window.addEventListener('resize', () => { drawCamera(); drawMap(); });
window.addEventListener('beforeunload', () => {
  view.running = false;
  view.frame?.close?.();
  view.map?.close?.();
});

setInterval(updateClock, 1000);
updateClock();
void stateLoop();
void cameraLoop();
