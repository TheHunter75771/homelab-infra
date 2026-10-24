// Subscription renewal reminders (CONTRACTS §11.3): one push 7 days before each renewal date, sent by the
// dashboard. Checked at start, every hour and whenever settings change, so a reminder fires within the hour
// after local midnight of the reminder day, or on the first check after that if the dashboard was down
// (up to and including the renewal date). Each (subscription, date) pair is reminded once; changing the date
// re-arms it. Follows rehearsal like every automated push.
import path from 'node:path';
import { readJson, writeJsonAtomic } from './util/fsx.js';
import { addDays, daysBetween, localDate, longDate } from './util/time.js';

export const REMIND_DAYS_BEFORE = 7;

export function reminderDue(sub, today) {
  const remindOn = addDays(sub.date, -REMIND_DAYS_BEFORE);
  return today >= remindOn && today <= sub.date;
}

export function reminderMessage(sub, today) {
  const days = daysBetween(today, sub.date);
  const when = days === 0 ? 'today' : days === 1 ? 'tomorrow' : `in ${days} days`;
  return {
    title: `${sub.name} renews ${when}`,
    body: `Renewal date: ${longDate(sub.date)}. Edit dates in Settings.`,
    tag: `renewal-${sub.key}`,
    url: '/',
  };
}

export function createReminders({ stateDir, settings, push, isRehearsal, clock = () => Date.now(), timeZone, log }) {
  const file = path.join(stateDir, 'reminders.json');
  let sent = readJson(file).value ?? {};
  if (typeof sent !== 'object' || Array.isArray(sent)) sent = {};
  let running = null;

  const id = (sub) => `${sub.key}|${sub.date}`;

  async function checkOnce() {
    const today = localDate(clock(), timeZone);
    let changed = false;
    for (const sub of settings.get().subscriptions) {
      if (!reminderDue(sub, today) || sent[id(sub)]) continue;
      try {
        const r = await push.send(reminderMessage(sub, today), { rehearsal: isRehearsal() });
        if (r.sent > 0) {
          sent[id(sub)] = new Date(clock()).toISOString();
          changed = true;
          log?.info('renewal reminder sent', { subscription: sub.key, sent: r.sent });
        } else {
          log?.warn('renewal reminder not delivered; will retry', { subscription: sub.key, total: r.total });
        }
      } catch (err) {
        log?.warn('renewal reminder failed; will retry', { subscription: sub.key, error: err.message });
      }
    }
    // forget pairs more than 60 days past their date
    for (const k of Object.keys(sent)) {
      const date = k.split('|')[1];
      if (!date || daysBetween(date, today) > 60) {
        delete sent[k];
        changed = true;
      }
    }
    if (changed) {
      try {
        writeJsonAtomic(file, sent, { mode: 0o640 });
      } catch (err) {
        log?.error('could not save reminders.json', { code: err.code ?? null });
      }
    }
  }

  return {
    // Serialized: overlapping triggers share one run.
    check() {
      running ??= checkOnce().finally(() => {
        running = null;
      });
      return running;
    },
    wasSent: (sub) => Boolean(sent[id(sub)]),
  };
}
