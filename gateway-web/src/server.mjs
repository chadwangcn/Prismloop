/*
 * Private, local-only client-SDK gateway. It is deliberately not a public
 * Harness API. The parent service resolves Pod/product ids and short-lived
 * STS credentials, then injects one JSON bootstrap through the child process
 * environment. This process never logs or persists that bootstrap.
 */
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const listenHost = process.env.PRISMLOOP_GATEWAY_LISTEN_HOST || '127.0.0.1';
const listenPort = Number.parseInt(process.env.PRISMLOOP_GATEWAY_LISTEN_PORT || '8791', 10);
const bootstrapRaw = process.env.PRISMLOOP_VEPHONE_SESSION_BOOTSTRAP || '';
const maxBodyBytes = 12 * 1024 * 1024;
const state = { phase: 'capability_unavailable', reason: 'client_session_bootstrap_not_configured', page: null, browser: null };

function json(response, status, value) {
  response.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  response.end(JSON.stringify(value));
}

async function requestBody(request) {
  const chunks = [];
  let length = 0;
  for await (const chunk of request) {
    length += chunk.length;
    if (length > maxBodyBytes) throw new Error('request_body_too_large');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function parseBootstrap(value) {
  const bootstrap = JSON.parse(value);
  const required = ['accountId', 'productId', 'podId', 'userId', 'token'];
  if (!required.every((key) => bootstrap[key])) throw new Error('gateway_bootstrap_incomplete');
  const tokenFields = ['AccessKeyID', 'SecretAccessKey', 'SessionToken', 'CurrentTime', 'ExpiredTime'];
  if (!tokenFields.every((key) => bootstrap.token[key])) throw new Error('gateway_token_incomplete');
  return bootstrap;
}

async function startSdkSession() {
  if (!bootstrapRaw) return;
  const bootstrap = parseBootstrap(bootstrapRaw);
  const { chromium } = await import('playwright');
  const sdkPath = resolve('node_modules/@volcengine/vephone/dist/index.umd.min.js');
  await readFile(sdkPath); // fail closed when the pinned official SDK is missing
  state.browser = await chromium.launch({ headless: true, args: ['--use-gl=swiftshader'] });
  state.page = await state.browser.newPage();
  await state.page.setContent('<!doctype html><html><body><div id="player"></div></body></html>');
  await state.page.addScriptTag({ path: sdkPath });
  await state.page.evaluate(async (config) => {
    const sdk = new window.vePhoneSDK({ accountId: config.accountId, userId: config.userId, domId: 'player', isPC: true });
    await sdk.start({ productId: config.productId, podId: config.podId, token: config.token, mute: true, audioAutoPlay: true });
    window.__prismloopGateway = { sdk, video: new Map(), audio: new Map() };
  }, bootstrap);
  state.phase = 'ready';
  state.reason = null;
}

async function evaluateGateway(operation, value) {
  if (state.phase !== 'ready' || !state.page) throw new Error(state.reason || 'gateway_not_ready');
  return state.page.evaluate(async ({ operation, value }) => {
    const gateway = window.__prismloopGateway;
    if (!gateway) throw new Error('sdk_gateway_not_initialized');
    if (operation === 'open-video') {
      const { sessionId, width, height, fps } = value;
      if (gateway.video.has(sessionId)) throw new Error('video_session_already_exists');
      const canvas = document.createElement('canvas');
      canvas.width = width; canvas.height = height;
      const track = canvas.captureStream(fps).getVideoTracks()[0];
      await gateway.sdk.setVideoSourceType(0, 0); // MAIN + EXTERNAL (official Web SDK enum)
      await gateway.sdk.startExternalVideoTrack(0, track);
      gateway.video.set(sessionId, { canvas, context: canvas.getContext('2d'), track, width, height });
      return { sessionId };
    }
    if (operation === 'push-video') {
      const entry = gateway.video.get(value.sessionId);
      if (!entry) throw new Error('unknown_video_session');
      if (entry.width !== value.width || entry.height !== value.height) throw new Error('video_profile_changed');
      const data = Uint8Array.from(atob(value.payloadBase64), (char) => char.charCodeAt(0));
      const frame = new VideoFrame(data, { format: 'I420', codedWidth: value.width, codedHeight: value.height, timestamp: value.timestampUs });
      entry.context.drawImage(frame, 0, 0, value.width, value.height);
      frame.close();
      return { accepted: true };
    }
    if (operation === 'close-video') {
      const entry = gateway.video.get(value.sessionId);
      if (entry) { entry.track.stop(); gateway.video.delete(value.sessionId); }
      await gateway.sdk.stopExternalVideoTrack();
      return { closed: true };
    }
    if (operation === 'open-audio') {
      const { sessionId, sampleRateHz, channels } = value;
      if (gateway.audio.has(sessionId)) throw new Error('audio_session_already_exists');
      const context = new AudioContext({ sampleRate: sampleRateHz });
      const destination = context.createMediaStreamDestination();
      const track = destination.stream.getAudioTracks()[0];
      await gateway.sdk.setAudioSourceType(0, 0); // MAIN + EXTERNAL (official Web SDK enum)
      await gateway.sdk.startExternalAudioTrack(0, track);
      gateway.audio.set(sessionId, { context, destination, track, sampleRateHz, channels, nextTime: context.currentTime });
      return { sessionId };
    }
    if (operation === 'push-audio') {
      const entry = gateway.audio.get(value.sessionId);
      if (!entry) throw new Error('unknown_audio_session');
      if (entry.sampleRateHz !== value.sampleRateHz || entry.channels !== value.channels) throw new Error('audio_profile_changed');
      const raw = Uint8Array.from(atob(value.payloadBase64), (char) => char.charCodeAt(0));
      const sampleCount = raw.byteLength / 2 / value.channels;
      const audioBuffer = entry.context.createBuffer(value.channels, sampleCount, value.sampleRateHz);
      const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
      for (let channel = 0; channel < value.channels; channel += 1) {
        const out = audioBuffer.getChannelData(channel);
        for (let index = 0; index < sampleCount; index += 1) out[index] = view.getInt16((index * value.channels + channel) * 2, true) / 32768;
      }
      const source = entry.context.createBufferSource();
      source.buffer = audioBuffer; source.connect(entry.destination);
      entry.nextTime = Math.max(entry.nextTime, entry.context.currentTime + 0.02);
      source.start(entry.nextTime); entry.nextTime += audioBuffer.duration;
      return { accepted: true };
    }
    if (operation === 'close-audio') {
      const entry = gateway.audio.get(value.sessionId);
      if (entry) { entry.track.stop(); await entry.context.close(); gateway.audio.delete(value.sessionId); }
      await gateway.sdk.stopExternalAudioTrack();
      return { closed: true };
    }
    throw new Error('unknown_gateway_operation');
  }, { operation, value });
}

const server = createServer(async (request, response) => {
  try {
    if (request.method === 'GET' && request.url === '/health') return json(response, 200, { phase: state.phase, reason: state.reason });
    if (request.method !== 'POST') return json(response, 404, { error: 'not_found' });
    const body = JSON.parse((await requestBody(request)).toString('utf8'));
    const routes = {
      '/v1/private/video-sessions': 'open-video',
      '/v1/private/video-frames': 'push-video',
      '/v1/private/video-sessions:close': 'close-video',
      '/v1/private/audio-sessions': 'open-audio',
      '/v1/private/audio-frames': 'push-audio',
      '/v1/private/audio-sessions:close': 'close-audio',
    };
    if (!routes[request.url]) return json(response, 404, { error: 'not_found' });
    return json(response, 200, await evaluateGateway(routes[request.url], body));
  } catch (error) {
    const message = error instanceof Error ? error.message : 'gateway_error';
    return json(response, state.phase === 'ready' ? 400 : 503, { error: message.replace(/(?:AccessKey|Secret|SessionToken)[^,\s]*/gi, '[redacted]') });
  }
});

await startSdkSession().catch(async (error) => {
  state.phase = 'capability_unavailable';
  state.reason = error instanceof Error ? error.message : 'gateway_initialization_failed';
  if (state.browser) await state.browser.close();
  state.browser = null; state.page = null;
});
server.listen(listenPort, listenHost);

async function shutdown() {
  server.close();
  if (state.page) await state.page.evaluate(() => window.__prismloopGateway?.sdk?.stop()).catch(() => {});
  if (state.browser) await state.browser.close();
}
process.once('SIGINT', shutdown); process.once('SIGTERM', shutdown);
