import { API_BASE } from './api.js';

const SENSITIVE_KEY = /authorization|proxyauthorization|cookie|setcookie|password|secret|token|apikey/i;
const BEARER_VALUE = /(?<prefix>\bbearer\s+)\S+/gi;
const API_KEY_VALUE = /(?<prefix>sk-(?:or-)?)(?:[a-z0-9_-]{12,})/gi;
const HEADER_VALUE = /(?<prefix>\b(?:proxy[\s-]*authorization|set[\s-]*cookie|authorization|cookie)\s*:\s*)[^\r\n]*/gi;
const SEVERITY = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

function redactBrowserString(value) {
  return String(value)
    .replace(HEADER_VALUE, (...args) => {
      const groups = args[args.length - 1];
      return `${groups.prefix}[REDACTED]`;
    })
    .replace(BEARER_VALUE, (...args) => {
      const groups = args[args.length - 1];
      return `${groups.prefix}[REDACTED]`;
    })
    .replace(API_KEY_VALUE, (...args) => {
      const groups = args[args.length - 1];
      return `${groups.prefix}[REDACTED]`;
    });
}

function truncateBrowserText(value, maxBytes) {
  const encoded = new TextEncoder().encode(value);
  if (encoded.byteLength <= maxBytes) return value;
  return `${new TextDecoder().decode(encoded.slice(0, maxBytes))}[truncated:${encoded.byteLength}]`;
}

function sanitizeBrowserPage(value) {
  try {
    const url = new URL(value);
    return `${url.origin}${url.pathname}`;
  } catch {
    return String(value).split(/[?#]/, 1)[0];
  }
}

function serializedEventBytes(event) {
  return new TextEncoder().encode(JSON.stringify(event)).byteLength;
}

function serializedBatchBytes(events) {
  return new TextEncoder().encode(JSON.stringify({ events })).byteLength;
}

function wrappedEventBytes(event) {
  return serializedEventBytes({ events: [event] });
}

function takeTransportBatch(queue, batchSize, eventMaxBytes) {
  const events = [];
  while (events.length < batchSize && queue.length > 0) {
    const candidate = [...events, queue[0]];
    if (serializedBatchBytes(candidate) <= eventMaxBytes) {
      events.push(queue.shift());
    } else if (events.length === 0) {
      queue.shift();
    } else {
      break;
    }
  }
  return events;
}

function compactEventField(event, field, eventMaxBytes) {
  const original = event[field];
  const originalBytes = new TextEncoder().encode(original).byteLength;
  const suffix = `[truncated:${originalBytes}]`;
  let low = 0;
  let high = original.length;
  let compacted = '';

  const fallback = field === 'page' ? '/' : '[truncated]';
  event[field] = '';
  if (wrappedEventBytes(event) > eventMaxBytes) return false;

  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const candidate = middle === original.length
      ? original
      : `${original.slice(0, middle)}${suffix}`;
    event[field] = candidate;
    if (wrappedEventBytes(event) <= eventMaxBytes) {
      compacted = candidate;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  event[field] = compacted || fallback;
  return wrappedEventBytes(event) <= eventMaxBytes;
}

function truncateBrowserEvent(event, eventMaxBytes) {
  if (!Number.isInteger(eventMaxBytes) || eventMaxBytes <= 0) return null;
  if (wrappedEventBytes(event) <= eventMaxBytes) return event;

  const truncatedEvent = {
    ...event,
    message: '[truncated]',
    details: { truncated: true },
  };
  if (!compactEventField(truncatedEvent, 'page', eventMaxBytes)) return null;
  if (wrappedEventBytes(truncatedEvent) <= eventMaxBytes) return truncatedEvent;
  if (!compactEventField(truncatedEvent, 'message', eventMaxBytes)) return null;
  return wrappedEventBytes(truncatedEvent) <= eventMaxBytes ? truncatedEvent : null;
}

export function sanitizeBrowserValue(value, { eventMaxBytes, seen }) {
  if (typeof value === 'string') {
    return truncateBrowserText(redactBrowserString(value), eventMaxBytes);
  }
  if (value === null || ['boolean', 'number'].includes(typeof value)) return value;
  if (value instanceof Error) {
    return sanitizeBrowserValue(
      { name: value.name, message: value.message, stack: value.stack },
      { eventMaxBytes, seen },
    );
  }
  if (typeof value !== 'object') return truncateBrowserText(String(value), eventMaxBytes);
  if (seen.has(value)) return '[Circular]';
  seen.add(value);
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeBrowserValue(item, { eventMaxBytes, seen }));
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      SENSITIVE_KEY.test(String(key).toLowerCase().replace(/[^a-z0-9]/g, ''))
        ? '[REDACTED]'
        : sanitizeBrowserValue(item, { eventMaxBytes, seen }),
    ]),
  );
}

function createFetchTransport() {
  return async (endpoint, body) => {
    if (typeof globalThis.fetch !== 'function') return;
    await globalThis.fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      credentials: 'omit',
    });
  };
}

function defaultSessionId() {
  return globalThis.crypto?.randomUUID?.() ?? `browser-${Date.now()}-${Math.random()}`;
}

export function createBrowserLogger(options) {
  const {
    endpoint,
    level = 'WARNING',
    batchSize,
    flushMs,
    queueLimit,
    eventMaxBytes,
    windowObject,
    consoleObject,
    transport,
    beacon,
    setTimeoutFn = setTimeout,
    clearTimeoutFn = clearTimeout,
    now = () => new Date().toISOString(),
    sessionId = defaultSessionId(),
  } = options;
  const state = {
    queue: [],
    started: false,
    flushing: false,
    flushPromise: null,
    timerId: null,
  };
  const originals = new Map();

  function enabled(eventLevel) {
    return SEVERITY[eventLevel] >= SEVERITY[level];
  }

  function buildEvent(eventLevel, event, message, details = {}) {
    const safe = sanitizeBrowserValue(
      { message: String(message), details },
      { eventMaxBytes, seen: new WeakSet() },
    );
    return truncateBrowserEvent({
      client_timestamp: now(),
      level: eventLevel,
      event,
      message: safe.message || '[empty]',
      browser_session_id: sessionId,
      page: sanitizeBrowserPage(windowObject.location.href),
      details: safe.details,
    }, eventMaxBytes);
  }

  function enqueue(eventPayload) {
    while (state.queue.length >= queueLimit) state.queue.shift();
    state.queue.push(eventPayload);
    if (state.queue.length >= batchSize) void flush();
  }

  function log(eventLevel, event, message, details = {}) {
    try {
      if (!enabled(eventLevel)) return;
      const eventPayload = buildEvent(eventLevel, event, message, details);
      if (eventPayload) enqueue(eventPayload);
    } catch {
      // Logging must never change application behavior.
    }
  }

  async function flush() {
    if (state.flushing) return state.flushPromise;
    if (state.queue.length === 0) return undefined;

    state.flushing = true;
    const events = takeTransportBatch(state.queue, batchSize, eventMaxBytes);
    if (events.length === 0) {
      state.flushing = false;
      return undefined;
    }
    state.flushPromise = (async () => {
      try {
        await transport(endpoint, { events });
      } catch {
        // Failed batches are intentionally dropped after one bounded attempt.
      } finally {
        state.flushing = false;
        state.flushPromise = null;
      }
      if (state.queue.length > 0) await flush();
    })();
    return state.flushPromise;
  }

  function scheduleFlush() {
    if (!state.started) return;
    state.timerId = setTimeoutFn(() => {
      void flush().finally(scheduleFlush);
    }, flushMs);
  }

  function onError(event) {
    log('ERROR', 'browser.unhandled_error', event.message, { stack: event.error?.stack });
  }

  function onUnhandledRejection(event) {
    const reason = event.reason instanceof Error
      ? { message: event.reason.message, stack: event.reason.stack }
      : { reason: event.reason };
    log('ERROR', 'browser.unhandled_rejection', reason.message ?? 'Unhandled rejection', reason);
  }

  function onPageHide() {
    if (state.queue.length === 0 || !beacon) return;
    while (state.queue.length > 0) {
      const events = takeTransportBatch(state.queue, batchSize, eventMaxBytes);
      if (events.length === 0) continue;
      try {
        beacon(endpoint, JSON.stringify({ events }));
      } catch {
        // Final unload diagnostics are best effort only.
      }
    }
  }

  function wrapConsole(method, eventLevel) {
    const originalMethod = consoleObject[method];
    originals.set(method, originalMethod);
    consoleObject[method] = (...args) => {
      originalMethod.apply(consoleObject, args);
      try {
        const message = args.map((arg) => {
          try {
            return String(arg);
          } catch {
            return '[Unserializable]';
          }
        }).join(' ');
        log(eventLevel, `browser.console.${method}`, message, { args });
      } catch {
        // Console capture must never alter the original console call.
      }
    };
  }

  function start() {
    if (state.started) return;
    state.started = true;
    wrapConsole('warn', 'WARNING');
    wrapConsole('error', 'ERROR');
    if (level === 'DEBUG') {
      wrapConsole('debug', 'DEBUG');
      wrapConsole('log', 'DEBUG');
      wrapConsole('info', 'INFO');
    }
    windowObject.addEventListener('error', onError);
    windowObject.addEventListener('unhandledrejection', onUnhandledRejection);
    windowObject.addEventListener('pagehide', onPageHide);
    scheduleFlush();
  }

  function stop() {
    if (!state.started) return;
    state.started = false;
    if (state.timerId !== null) clearTimeoutFn(state.timerId);
    windowObject.removeEventListener('error', onError);
    windowObject.removeEventListener('unhandledrejection', onUnhandledRejection);
    windowObject.removeEventListener('pagehide', onPageHide);
    for (const [method, original] of originals) consoleObject[method] = original;
    originals.clear();
  }

  return { start, stop, log, flush };
}

function validLevel(value) {
  return Object.hasOwn(SEVERITY, value) ? value : 'WARNING';
}

function validPositiveInteger(value, fallback, maximum) {
  const parsed = Number.parseInt(value, 10);
  return Number.isInteger(parsed) && parsed > 0 && parsed <= maximum ? parsed : fallback;
}

export function installBrowserLogging() {
  const environment = import.meta.env;
  const level = validLevel(environment.VITE_LOG_BROWSER_LEVEL);
  const batchSize = validPositiveInteger(environment.VITE_LOG_BROWSER_BATCH_SIZE, 20, 100);
  const flushMs = validPositiveInteger(environment.VITE_LOG_BROWSER_FLUSH_MS, 5000, 60000);
  const queueLimit = validPositiveInteger(environment.VITE_LOG_BROWSER_QUEUE_LIMIT, 100, 1000);
  const eventMaxBytes = validPositiveInteger(environment.VITE_LOG_EVENT_MAX_BYTES, 65536, 65536);

  return createBrowserLogger({
    endpoint: `${API_BASE}/api/logs/browser`,
    level,
    batchSize,
    flushMs,
    queueLimit,
    eventMaxBytes,
    windowObject: globalThis.window,
    consoleObject: globalThis.console,
    transport: createFetchTransport(),
    beacon: globalThis.navigator?.sendBeacon?.bind(globalThis.navigator),
  });
}
