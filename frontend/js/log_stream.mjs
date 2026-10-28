// Pure classification of frames arriving on the instance-log WebSocket.
//
// The backend interleaves real log records with heartbeats and a deliberately
// visible "logs were dropped" control frame. Treating the latter as a normal
// line produced a blank row and hid the lost-line count.

export function classifyLogFrame(message) {
  if (!message || typeof message !== 'object') return { kind: 'ignore' };
  if (message.type === 'ping') return { kind: 'heartbeat' };
  if (message.type === 'logs_dropped') {
    const count = Number(message.count);
    return {
      kind: 'dropped',
      count: Number.isFinite(count) && count > 0 ? Math.floor(count) : 0,
    };
  }
  return { kind: 'line' };
}
