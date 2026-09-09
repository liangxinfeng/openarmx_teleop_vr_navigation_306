/* Dedicated latest-frame transport for chassis control.

   Rendering, camera decoding and map drawing stay on the page's main thread.
   This worker only keeps one current joystick sample and transmits it at a
   stable 50 Hz.  A stale input sample is never replayed indefinitely.
*/

'use strict';

const SEND_PERIOD_MS = 20;
const INPUT_STALE_MS = 320;
const IDLE_HEARTBEAT_MS = 100;
const MAX_BUFFERED_BYTES = 4096;

let socket = null;
let socketUrl = '';
let reconnectTimer = null;
let latest = { deadman: false };
let latestReceivedAt = 0;
let sequence = 0;
let lastIdleSentAt = 0;

function notify(state, detail = '') {
  self.postMessage({ type: 'drive_link_state', state, detail });
}

function scheduleReconnect() {
  if (!socketUrl || reconnectTimer !== null) return;
  reconnectTimer = self.setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 350);
}

function connect() {
  if (!socketUrl) return;
  if (socket && (socket.readyState === WebSocket.OPEN
      || socket.readyState === WebSocket.CONNECTING)) return;
  const candidate = new WebSocket(socketUrl);
  socket = candidate;
  notify('connecting');
  candidate.onopen = () => {
    if (socket !== candidate) return;
    notify('connected');
    transmit(false, true);
  };
  candidate.onclose = () => {
    if (socket !== candidate) return;
    socket = null;
    notify('disconnected');
    scheduleReconnect();
  };
  candidate.onerror = () => candidate.close();
}

function activeSample(now) {
  return Boolean(
    latest.deadman
    && latestReceivedAt > 0
    && now - latestReceivedAt <= INPUT_STALE_MS
  );
}

function transmit(forceInactive = false, forceSend = false) {
  const now = performance.now();
  const active = !forceInactive && activeSample(now);
  if (!forceSend && !active && now - lastIdleSentAt < IDLE_HEARTBEAT_MS) return;
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  // TCP must never accumulate old velocity commands. Reconnect to discard a
  // congested send queue instead of delivering stale motion later.
  if (socket.bufferedAmount > MAX_BUFFERED_BYTES) {
    notify('congested', String(socket.bufferedAmount));
    socket.close(4000, 'stale drive queue');
    return;
  }
  const payload = active ? { ...latest } : { deadman: false };
  payload.type = 'base_drive';
  payload.sequence = ++sequence;
  payload.timestamp = Date.now();
  socket.send(JSON.stringify(payload));
  if (!active) lastIdleSentAt = now;
}

self.onmessage = event => {
  const message = event.data || {};
  if (message.type === 'configure') {
    socketUrl = String(message.url || '');
    connect();
    return;
  }
  if (message.type === 'sample') {
    latest = message.payload && typeof message.payload === 'object'
      ? message.payload
      : { deadman: false };
    latestReceivedAt = performance.now();
    if (!latest.deadman) transmit(true, true);
    return;
  }
  if (message.type === 'release') {
    latest = { deadman: false };
    latestReceivedAt = performance.now();
    transmit(true, true);
  }
};

self.setInterval(() => transmit(false, false), SEND_PERIOD_MS);

