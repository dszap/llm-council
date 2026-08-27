import assert from 'node:assert/strict';
import test from 'node:test';

import { createBrowserLogger } from './logger.js';

function createFakeWindow() {
  const listeners = new Map();
  return {
    location: { href: 'http://localhost:5173/' },
    addEventListener(name, callback) {
      listeners.set(name, callback);
    },
    removeEventListener(name) {
      listeners.delete(name);
    },
    dispatch(name, event) {
      listeners.get(name)?.(event);
    },
  };
}

test('preserves console.error and batches a sanitized event', async () => {
  const sent = [];
  const originalCalls = [];
  const consoleObject = {
    warn() {},
    error(...args) {
      originalCalls.push(args);
    },
    log() {},
    info() {},
    debug() {},
  };
  const logger = createBrowserLogger({
    endpoint: 'http://localhost:8001/api/logs/browser',
    level: 'WARNING',
    batchSize: 1,
    flushMs: 2000,
    queueLimit: 2,
    eventMaxBytes: 65536,
    windowObject: createFakeWindow(),
    consoleObject,
    transport: async (endpoint, body) => sent.push({ endpoint, body }),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.start();
  consoleObject.error('failure', { authorization: 'Bearer secret-value' });
  await logger.flush();
  logger.stop();
  assert.equal(originalCalls.length, 1);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].body.events[0].event, 'browser.console.error');
  assert.doesNotMatch(JSON.stringify(sent), /secret-value/);
});

test('queue overflow drops the oldest event', async () => {
  const sent = [];
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'DEBUG',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 2,
    eventMaxBytes: 65536,
    windowObject: createFakeWindow(),
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.log('INFO', 'first', 'first');
  logger.log('INFO', 'second', 'second');
  logger.log('INFO', 'third', 'third');
  await logger.flush();
  assert.deepEqual(sent[0].events.map((event) => event.event), ['second', 'third']);
});

test('defaults to WARNING when no level is supplied', async () => {
  const sent = [];
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 65536,
    windowObject: createFakeWindow(),
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.log('INFO', 'ignored.event', 'ignored');
  logger.log('WARNING', 'warning.event', 'captured');
  await logger.flush();
  assert.deepEqual(sent[0].events.map((event) => event.event), ['warning.event']);
});

test('captures window errors and unhandled rejections', async () => {
  const sent = [];
  const windowObject = createFakeWindow();
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'WARNING',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 65536,
    windowObject,
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.start();
  windowObject.dispatch('error', { message: 'boom', error: new Error('boom') });
  windowObject.dispatch('unhandledrejection', { reason: new Error('rejected') });
  await logger.flush();
  logger.stop();
  assert.deepEqual(
    sent[0].events.map((event) => event.event),
    ['browser.unhandled_error', 'browser.unhandled_rejection'],
  );
});

test('timer and pagehide flush pending events', async () => {
  const sent = [];
  const beacons = [];
  const windowObject = createFakeWindow();
  let scheduledCallback;
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'DEBUG',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 65536,
    windowObject,
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    beacon: (endpoint, body) => beacons.push({ endpoint, body }),
    setTimeoutFn: (callback) => {
      scheduledCallback = callback;
      return 1;
    },
    clearTimeoutFn: () => {},
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.start();
  logger.log('INFO', 'timer.event', 'timer');
  await scheduledCallback();
  assert.equal(sent.length, 1);
  logger.log('INFO', 'unload.event', 'unload');
  windowObject.dispatch('pagehide', {});
  assert.equal(beacons.length, 1);
  logger.stop();
});

test('sanitizes cyclic and oversized details and filters levels', async () => {
  const sent = [];
  const cyclic = { authorization: 'Bearer secret-value' };
  cyclic.self = cyclic;
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'WARNING',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 256,
    windowObject: createFakeWindow(),
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.log('INFO', 'filtered.event', 'not sent');
  logger.log('ERROR', 'error.event', 'á'.repeat(500), cyclic);
  await logger.flush();
  const serialized = JSON.stringify(sent);
  assert.doesNotMatch(serialized, /secret-value/);
  assert.doesNotMatch(serialized, /filtered\.event/);
  assert.match(serialized, /truncated|error\.event/);
  assert.ok(new TextEncoder().encode(JSON.stringify(sent[0].events[0])).byteLength <= 256);
});

test('stop restores console and transport rejection is contained', async () => {
  const originalErrors = [];
  const originalError = (...args) => originalErrors.push(args);
  const consoleObject = {
    warn() {},
    error: originalError,
    log() {},
    info() {},
    debug() {},
  };
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'WARNING',
    batchSize: 1,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 65536,
    windowObject: createFakeWindow(),
    consoleObject,
    transport: async () => {
      throw new Error('transport unavailable');
    },
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.start();
  consoleObject.error('application error');
  await assert.doesNotReject(logger.flush());
  logger.stop();
  assert.equal(consoleObject.error, originalError);
  assert.equal(originalErrors.length, 1);
});

test('console capture cannot throw for an unstringifiable argument', () => {
  const originalErrors = [];
  const consoleObject = {
    warn() {},
    error(...args) {
      originalErrors.push(args);
    },
    log() {},
    info() {},
    debug() {},
  };
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'WARNING',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 65536,
    windowObject: createFakeWindow(),
    consoleObject,
    transport: async () => {},
    setTimeoutFn: () => 1,
    clearTimeoutFn: () => {},
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.start();
  assert.doesNotThrow(() => consoleObject.error(Object.create(null)));
  logger.stop();
  assert.equal(originalErrors.length, 1);
});

test('removes query parameters and fragments from browser page events', async () => {
  const sent = [];
  const windowObject = createFakeWindow();
  windowObject.location.href = 'https://council.example/conversations/42?api_key=api-secret-value&token=token-secret-value#access_token=fragment-secret';
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'WARNING',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 65536,
    windowObject,
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.log('ERROR', 'page.event', 'failure');
  await logger.flush();
  const serialized = JSON.stringify(sent);
  assert.equal(sent[0].events[0].page, 'https://council.example/conversations/42');
  assert.doesNotMatch(serialized, /api-secret-value|token-secret-value|fragment-secret/);
});

test('caps long-page events and drops events below the metadata floor', async () => {
  const sent = [];
  const windowObject = createFakeWindow();
  windowObject.location.href = `https://council.example/${'x'.repeat(1000)}?token=secret-value`;
  const logger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'WARNING',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 256,
    windowObject,
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => sent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  logger.log('ERROR', 'page.event', 'failure');
  await logger.flush();
  assert.ok(new TextEncoder().encode(JSON.stringify(sent[0].events[0])).byteLength <= 256);
  assert.match(sent[0].events[0].page, /^https:\/\/council\.example\//);

  const tinySent = [];
  const tinyLogger = createBrowserLogger({
    endpoint: '/api/logs/browser',
    level: 'WARNING',
    batchSize: 20,
    flushMs: 2000,
    queueLimit: 20,
    eventMaxBytes: 1,
    windowObject,
    consoleObject: { warn() {}, error() {}, log() {}, info() {}, debug() {} },
    transport: async (endpoint, body) => tinySent.push(body),
    now: () => '2026-08-26T22:07:12.438Z',
    sessionId: 'session-1',
  });
  tinyLogger.log('ERROR', 'tiny.event', 'failure');
  await tinyLogger.flush();
  assert.equal(tinySent.length, 0);
});
