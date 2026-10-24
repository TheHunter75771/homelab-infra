// Sending pushes from the dashboard itself (the test button and renewal reminders). Dead subscriptions
// (404/410) are removed through the store: the dashboard is the single writer of subscriptions.json.
import { buildPayload, sendToAll } from './sender.js';

export const REHEARSAL_PREFIX = '[REHEARSAL] ';
export const TEST_LABEL = 'test';

export function createPushService({ store, vapid, webpush, log }) {
  // message: {title, body, tag, url}; rehearsal: prefix the title and send only to label "test" (the notifier's
  // rule, CONTRACTS §4). The human-triggered test button passes rehearsal=false (decision 9c).
  async function send(message, { rehearsal = false } = {}) {
    const keys = vapid.get(); // throws VapidError
    let entries = store.list(); // throws StoreCorruptError
    if (rehearsal) entries = entries.filter((e) => e.label === TEST_LABEL);
    if (entries.length === 0) return { total: 0, sent: 0, failed: 0, removed: 0 };
    const title = rehearsal && !message.title.startsWith(REHEARSAL_PREFIX) ? REHEARSAL_PREFIX + message.title : message.title;
    const result = await sendToAll({ entries, payload: buildPayload({ ...message, title }), vapid: keys, webpush, log });
    let removed = 0;
    if (result.gone.length) {
      try {
        removed = store.removeEndpoints(result.gone);
      } catch {
        // logged by the store; retried on the next send
      }
    }
    return { total: result.total, sent: result.sent, failed: result.failed, removed };
  }
  return { send };
}
