// Wall-screen calendar view (docs/CONTRACTS.md §14.2). Renders what /api/agenda gives it: every label is
// pre-formatted by the server, so there is no date logic here. Text only through textContent. The clock
// ticks locally, in the server's time zone; the agenda is fetched again every 60 s.
"use strict";
(() => {
  const POLL_MS = 60000;
  const CAP_NOW = 2; // events shown before "+N more" on the wall layout (the CSS hides the rest there)
  const CAP_TODAY = 7;
  const CAP_DAY = 5;
  const COLOR = /^#[0-9a-f]{6}$/i;
  const $ = (id) => document.getElementById(id);

  let clockFormat = null;
  let clockZone = "";
  let lastGood = null; // {staleSeconds, at}: when the last agenda arrived, and how old its data was then

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function eventItem(ev) {
    const item = el("li", ev.all_day ? "event all-day" : "event");
    const dot = el("span", "dot");
    if (COLOR.test(ev.color)) dot.style.backgroundColor = ev.color;
    const when = el("span", "time");
    when.append(el("span", "start", ev.start_label));
    if (ev.end_label) when.append(el("span", "end", ev.end_label));
    item.append(dot, when, el("span", "title", ev.title));
    return item;
  }

  function fill(list, events, cap, emptyText) {
    const items = events.map(eventItem);
    if (events.length > cap) items.push(el("li", "more", "+" + (events.length - cap) + " more"));
    if (!events.length && emptyText) items.push(el("li", "empty", emptyText));
    list.dataset.cap = String(cap); // app.css shows the first `cap` on the wall layout, all of them on a phone
    list.replaceChildren(...items);
  }

  function render(data) {
    $("today-date").textContent = data.today.date_label;
    document.title = data.today.date_label + " · Household Calendar";
    fill($("now-list"), data.today.now, CAP_NOW, "");
    $("now-block").hidden = data.today.now.length === 0;
    fill($("next-list"), data.today.next ? [data.today.next] : [], 1, "Nothing else today");
    fill($("today-list"), data.days[0].events, CAP_TODAY, "Nothing today");

    const days = data.days.slice(1).map((day) => {
      const card = el("article", "day");
      card.append(el("h2", "day-label", day.label));
      const list = el("ul", "events");
      fill(list, day.events, CAP_DAY, "Nothing scheduled");
      card.append(list);
      return card;
    });
    $("week").replaceChildren(...days);

    $("legend").replaceChildren(...(data.calendars || []).map((cal) => {
      const item = el("li", "legend-item");
      const dot = el("span", "dot");
      if (COLOR.test(cal.color)) dot.style.backgroundColor = cal.color;
      item.append(dot, el("span", "", cal.label));
      return item;
    }));

    if (data.timezone && data.timezone !== clockZone) setClock(data.timezone);
  }

  function setClock(zone) {
    const options = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
    try {
      clockFormat = new Intl.DateTimeFormat("en-GB", { ...options, timeZone: zone });
    } catch (_) {
      clockFormat = new Intl.DateTimeFormat("en-GB", options); // an unknown zone: the browser's own
    }
    clockZone = zone;
    tick();
  }

  function tick() {
    $("clock").textContent = clockFormat.format(new Date());
  }

  function age(seconds) {
    const minutes = Math.max(1, Math.floor(seconds / 60));
    if (minutes < 120) return minutes + " min";
    const hours = Math.floor(minutes / 60);
    return hours < 48 ? hours + " h" : Math.floor(hours / 24) + " days";
  }

  function banner(text) {
    const node = $("banner");
    node.textContent = text || "";
    node.hidden = !text;
    document.body.classList.toggle("is-stale", Boolean(text));
  }

  function failed(data) {
    if (lastGood) {
      const seconds = lastGood.staleSeconds + (performance.now() - lastGood.at) / 1000;
      banner("Not updated for " + age(seconds));
    } else if (data && data.error) {
      banner("Waiting for the calendar server");
    } else {
      banner("Calendar view not reachable");
    }
  }

  async function load() {
    let data = null;
    try {
      const response = await fetch("/api/agenda", { cache: "no-store", credentials: "same-origin" });
      try {
        data = await response.json();
      } catch (_) {
        data = null;
      }
      if (!response.ok || !data || !data.today || !Array.isArray(data.days) || !data.days.length) {
        failed(data);
        return;
      }
    } catch (_) {
      failed(null);
      return;
    }
    render(data);
    lastGood = { staleSeconds: data.stale_seconds || 0, at: performance.now() };
    banner(data.stale ? "Not updated for " + age(data.stale_seconds || 0) : "");
  }

  setClock("");
  setInterval(tick, 1000);
  load();
  setInterval(load, POLL_MS);
})();
