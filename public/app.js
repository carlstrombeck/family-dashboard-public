// Kitchen dashboard front end. Plain ES modules, no build step, written for Safari on iPad.

const CALENDAR_DAYS = 7;
const WEEK_MIN_HOURS = 6; // the week view never zooms in closer than this
const WEEK_DEFAULT_HOURS = [8, 18]; // when the week has no timed events
const VIEW_KEY = "calendarView";
const REFRESH = { calendar: 5 * 60_000, lists: 30_000, weather: 15 * 60_000, accounts: 30 * 60_000 };
const TOMORROW_WEATHER_FROM = 20; // from 20:00 the header shows tomorrow's weather
const REQUEST_TIMEOUT = 20_000;
const LINGER_MS = 4_000; // a ticked item stays in place briefly so a wrong tap is easy to undo

const STRINGS = {
  en: {
    calendar: "Calendar", agenda: "Agenda", today: "Today", tomorrow: "Tomorrow", allDay: "All day",
    nothing: "Nothing planned", noTitle: "(No title)", week: "Week", addItem: "Add item…",
    checked: (n) => `✓ ${n} checked`, hideChecked: "Hide checked",
    allDone: "All done", updated: (t) => `Updated ${t}`, offline: (t) => `Offline · data from ${t}`,
    signInTitle: "Family dashboard", signInGoogle: "Sign in with Google", accessKey: "Access key",
    unlock: "Unlock", demo: "Demo data",
    connectGoogle: "Google isn't connected yet.", openSetup: "Open setup", setup: "Setup",
    listMissing: (t) => `No Keep checklist called “${t}”.`,
    notChecklist: (t) => `“${t}” is a note, not a checklist.`,
    keepMissing: "Google Keep isn't set up yet.", lists: "Lists",
    noSharedNotes: "No Keep notes are shared with the family yet.",
    accounts: "Points & balances", finances: "Family finances", points: "points", updatedOn: (d) => `updated ${d}`,
    owes: (debtor, creditor) => `${debtor} owes ${creditor}`, settled: "All square",
    couldNotLoad: "Couldn't load", saveFailed: "Couldn't save. Try again.",
    addEvent: "Add event", newEvent: "New event", eventTitle: "What's happening?",
    eventPlace: "Where (optional)", cancel: "Cancel", add: "Add", undo: "Undo",
    added: (t) => `Added “${t}”`, removed: "Removed", needTitle: "Give the event a name.",
    voice: "Voice command", handsFree: (name) => `Listen for “${name}”`, listening: "Listening…",
    sayName: (name) => `Say “${name}” and a command`, thinking: "On it…", paidBy: (who) => `${who} paid`,
    close: "Close", refresh: "Refresh", voiceLog: "Voice commands", noVoiceLog: "No voice commands yet.", undone: "Undone",
    answered: "Answered", weatherShown: "Showed the weather", micBlocked: "The microphone is blocked. Allow it in Safari's settings for this site.",
    heardNothing: "Didn't catch that.", voiceFailed: "Speech recognition failed",
  },
  sv: {
    calendar: "Kalender", agenda: "Schema", today: "Idag", tomorrow: "Imorgon", allDay: "Heldag",
    nothing: "Inget inplanerat", noTitle: "(Ingen titel)", week: "Vecka", addItem: "Lägg till…",
    checked: (n) => `✓ ${n} avbockade`, hideChecked: "Dölj avbockade",
    allDone: "Allt klart", updated: (t) => `Uppdaterad ${t}`, offline: (t) => `Offline · data från ${t}`,
    signInTitle: "Familjens tavla", signInGoogle: "Logga in med Google", accessKey: "Åtkomstnyckel",
    unlock: "Lås upp", demo: "Demodata",
    connectGoogle: "Google är inte kopplat än.", openSetup: "Öppna inställningar", setup: "Inställningar",
    listMissing: (t) => `Hittar ingen Keep-checklista som heter ”${t}”.`,
    notChecklist: (t) => `”${t}” är en anteckning, inte en checklista.`,
    keepMissing: "Google Keep är inte inställt än.", lists: "Listor",
    noSharedNotes: "Inga Keep-anteckningar är delade med familjen än.",
    accounts: "Poäng & saldon", finances: "Familjens ekonomi", points: "poäng", updatedOn: (d) => `uppdaterad ${d}`,
    owes: (debtor, creditor) => `${debtor} är skyldig ${creditor}`, settled: "Kvitt",
    couldNotLoad: "Kunde inte ladda", saveFailed: "Kunde inte spara. Försök igen.",
    addEvent: "Lägg till händelse", newEvent: "Ny händelse", eventTitle: "Vad händer?",
    eventPlace: "Var (valfritt)", cancel: "Avbryt", add: "Lägg till", undo: "Ångra",
    added: (t) => `Lade till ”${t}”`, removed: "Borttagen", needTitle: "Ge händelsen ett namn.",
    voice: "Röstkommando", handsFree: (name) => `Lyssna efter ”${name}”`, listening: "Lyssnar…",
    sayName: (name) => `Säg ”${name}” och vad du vill`, thinking: "Fixar…", paidBy: (who) => `${who} betalade`,
    close: "Stäng", refresh: "Uppdatera", voiceLog: "Röstkommandon", noVoiceLog: "Inga röstkommandon än.", undone: "Ångrat",
    answered: "Svarade", weatherShown: "Visade vädret", micBlocked: "Mikrofonen är blockerad. Tillåt den i Safaris inställningar för sidan.",
    heardNothing: "Jag hörde inget.", voiceFailed: "Taligenkänningen fungerade inte",
  },
};

const $ = (id) => document.getElementById(id);

const state = {
  config: {},
  calendar: null, calendarError: null,
  view: "agenda", // or "week"
  lists: null, listsError: null,
  pendingWrites: 0,
  lingering: new Map(), // item id -> time it may leave the open list
  showChecked: new Set(),
  lastSuccess: null,
  offline: false,
  locked: false,
  dayKey: "",
  minute: -1,
  loadedAt: Date.now(),
};

let t = STRINGS.en;
let fmt = makeFormats("en-GB");

// ---------- helpers ----------

function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style") for (const [k, v] of Object.entries(value)) el.style.setProperty(k, v);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

function makeFormats(locale) {
  return {
    time: new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }),
    weekday: new Intl.DateTimeFormat(locale, { weekday: "long" }),
    shortWeekday: new Intl.DateTimeFormat(locale, { weekday: "short" }),
    date: new Intl.DateTimeFormat(locale, { day: "numeric", month: "long" }),
    shortDate: new Intl.DateTimeFormat(locale, { day: "numeric", month: "short" }),
    weekdayDate: new Intl.DateTimeFormat(locale, { weekday: "long", day: "numeric", month: "short" }),
  };
}

function setLanguage(preferred) {
  const lang = (preferred || navigator.language || "en").toLowerCase().startsWith("sv") ? "sv" : "en";
  const locale = lang === "sv" ? "sv-SE" : navigator.language.toLowerCase().startsWith("en") ? navigator.language : "en-GB";
  t = STRINGS[lang];
  fmt = makeFormats(locale);
  document.documentElement.lang = lang;
  for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = t[el.dataset.i18n];
  for (const el of document.querySelectorAll("[data-i18n-label]")) el.setAttribute("aria-label", t[el.dataset.i18nLabel]);
  for (const el of document.querySelectorAll("[data-i18n-placeholder]")) el.placeholder = t[el.dataset.i18nPlaceholder];
}

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const pad = (n) => String(n).padStart(2, "0");

function isoLocal(d) {
  const offset = -d.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const abs = Math.abs(offset);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:00`
    + `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

function isoWeek(d) {
  const date = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = date.getUTCDay() || 7;
  date.setUTCDate(date.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  return Math.ceil(((date - yearStart) / 86_400_000 + 1) / 7);
}

function parseWhen(value, allDay) {
  if (allDay) {
    const [y, m, d] = value.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  return new Date(value);
}

let toastTimer;
function toast(message, action = null) {
  const el = $("toast");
  el.replaceChildren(message);
  if (action) {
    el.append(h("button", { type: "button", onclick: () => { el.hidden = true; action.run(); } }, action.label));
  }
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, action ? 8000 : 3500);
}

// ---------- API ----------

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function api(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeout || REQUEST_TIMEOUT);
  let response;
  try {
    response = await fetch(path, {
      ...options,
      signal: controller.signal,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
    });
  } catch (err) {
    state.offline = true;
    renderStatus();
    throw new ApiError(0, "network", err.message);
  } finally {
    clearTimeout(timer);
  }
  const body = await response.json().catch(() => ({}));
  state.offline = false;
  if (response.status === 401 && body.error && body.error.code === "signin_required") showSignIn(body.error.methods);
  if (!response.ok) {
    const error = body.error || {};
    throw new ApiError(response.status, error.code || "http", error.message || response.statusText);
  }
  state.lastSuccess = new Date();
  renderStatus();
  return body;
}

function problem(error, fallbackTitle) {
  if (error.code === "google_not_connected") {
    return h("div", { class: "problem" }, t.connectGoogle, " ", h("a", { href: "/setup" }, t.openSetup));
  }
  if (error.code === "keep_not_configured") {
    return h("div", { class: "problem" }, t.keepMissing, " ", h("a", { href: "/setup" }, t.openSetup));
  }
  return h("div", { class: "problem" }, `${fallbackTitle || t.couldNotLoad}: ${error.message}`);
}

// ---------- header ----------

function tickClock() {
  const now = new Date();
  $("time").textContent = fmt.time.format(now);
  $("weekday").textContent = cap(fmt.weekday.format(now));
  $("fulldate").textContent = `${fmt.date.format(now)} · ${t.week} ${isoWeek(now)}`;

  const dayKey = now.toDateString();
  if (state.dayKey && dayKey !== state.dayKey) loadCalendar(); // midnight: move the week along
  state.dayKey = dayKey;

  if (now.getMinutes() !== state.minute) {
    state.minute = now.getMinutes();
    renderCalendar(); // past / ongoing events
    renderWeather(); // switches to tomorrow at 20:00
    renderStatus();
  }

  // Reload once a night so the iPad picks up new versions and never runs for weeks straight.
  if (now.getHours() === 3 && Date.now() - state.loadedAt > 3_600_000) location.reload();
}

function renderStatus() {
  const el = $("status");
  const text = el.querySelector(".status-text");
  const at = state.lastSuccess ? fmt.time.format(state.lastSuccess) : "–";
  el.classList.toggle("bad", state.offline);
  el.classList.toggle("ok", !state.offline && !!state.lastSuccess);
  text.textContent = state.offline ? t.offline(at) : state.lastSuccess ? t.updated(at) : "";
}

// ---------- calendar ----------

async function loadCalendar() {
  const start = startOfDay(new Date());
  const end = addDays(start, CALENDAR_DAYS);
  const query = new URLSearchParams({ start: isoLocal(start), end: isoLocal(end) });
  try {
    state.calendar = await api(`/api/calendar?${query}`);
    state.calendarError = null;
  } catch (err) {
    // Keep showing what we have if the network blipped.
    if (!(err.code === "network" && state.calendar)) state.calendarError = err;
  }
  renderCalendar();
}

function eventRow(event, dayStart, dayEnd, now, color) {
  const coversDay = event.allDay || (event.s <= dayStart && event.e >= dayEnd);
  let when;
  let detail = null;
  if (coversDay) {
    when = t.allDay;
  } else if (event.s < dayStart) {
    when = `–${fmt.time.format(event.e)}`;
  } else {
    when = fmt.time.format(event.s);
    if (event.e > dayEnd) detail = "→";
    else if (event.e > event.s) detail = `–${fmt.time.format(event.e)}`;
  }
  const classes = ["event"];
  if (coversDay) classes.push("allday");
  if (!coversDay && event.e <= now) classes.push("past");
  if (!coversDay && event.s <= now && now < event.e) classes.push("now");

  return h("li", { class: classes.join(" "), style: { "--c": color } },
    h("span", { class: "when" }, when, detail && h("small", {}, detail)),
    h("span", { class: "what" },
      h("span", { class: "title" }, event.title || t.noTitle),
      event.location && h("span", { class: "where" }, event.location.split(",")[0])));
}

// Events on one day, all-day and day-covering ones first, then by start time.
function eventsOn(events, dayStart, dayEnd) {
  return events
    .filter((e) => e.s < dayEnd && (e.e > dayStart || (+e.e === +e.s && e.s >= dayStart)))
    .map((e) => ({ ...e, whole: e.allDay || (e.s <= dayStart && e.e >= dayEnd) }))
    .sort((a, b) => (b.whole - a.whole) || (a.s - b.s) || String(a.title).localeCompare(String(b.title)));
}

function renderCalendar() {
  const agenda = $("agenda");
  const legend = $("legend");
  const week = state.view === "week";
  $("calendar-panel").classList.toggle("week-mode", week);
  document.body.classList.toggle("week-view", week);
  for (const button of $("view-switch").children) {
    button.classList.toggle("on", button.dataset.view === state.view);
    button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
  }
  const data = state.calendar;
  if (!data) {
    agenda.replaceChildren(state.calendarError ? problem(state.calendarError) : "");
    legend.replaceChildren();
    return;
  }

  const colors = {};
  for (const cal of data.calendars) colors[cal.id] = cal.color;
  legend.replaceChildren(...(data.calendars.length > 1
    ? data.calendars.map((c) => h("span", {}, h("i", { style: { "--c": c.color } }), c.name))
    : []));
  $("add-event").hidden = !(data.canAdd && data.calendars.some((c) => c.writable));

  const events = data.events.map((e) => ({ ...e, s: parseWhen(e.start, e.allDay), e: parseWhen(e.end, e.allDay) }));
  const now = new Date();
  const today = startOfDay(now);
  const colorOf = (e) => colors[e.calendar] || "#8e8e93";
  const days = [];
  for (let i = 0; i < CALENDAR_DAYS; i++) {
    const dayStart = addDays(today, i);
    const dayEnd = addDays(today, i + 1);
    days.push({ i, dayStart, dayEnd, events: eventsOn(events, dayStart, dayEnd) });
  }

  const content = week ? [weekView(days, now, colorOf)] : days.map((day) => agendaDay(day, now, colorOf));
  if (state.calendarError) content.unshift(problem(state.calendarError));
  if (data.errors && data.errors.length) {
    content.push(h("div", { class: "cal-errors" },
      `${t.couldNotLoad}: ${data.errors.map((e) => e.calendar).join(", ")}`));
  }
  agenda.replaceChildren(...content);
}

function agendaDay({ i, dayStart, dayEnd, events }, now, colorOf) {
  const name = i === 0 ? t.today : i === 1 ? t.tomorrow : cap(fmt.weekday.format(dayStart));
  const date = i < 2 ? cap(fmt.weekdayDate.format(dayStart)) : fmt.shortDate.format(dayStart);
  const weekend = dayStart.getDay() === 0 || dayStart.getDay() === 6;
  return h("div", { class: `day${i === 0 ? " today" : ""}${weekend ? " weekend" : ""}` },
    h("div", { class: "day-head" }, h("span", { class: "day-name" }, name), h("span", { class: "day-date" }, date)),
    events.length
      ? h("ul", { class: "events" }, events.map((e) => eventRow(e, dayStart, dayEnd, now, colorOf(e))))
      : h("div", { class: "empty" }, t.nothing));
}

// ---------- week view ----------

const hourOf = (d) => d.getHours() + d.getMinutes() / 60;

// Hours on the day's clock that an event covers, clipped to the day.
function spanOn(event, dayStart, dayEnd) {
  const from = event.s <= dayStart ? 0 : hourOf(event.s);
  const to = event.e >= dayEnd ? 24 : hourOf(event.e);
  return [from, Math.max(to, from + 0.25)];
}

// From the week's earliest start to its latest end, widened to at least WEEK_MIN_HOURS.
function weekHours(days) {
  let first = 24;
  let last = 0;
  for (const day of days) {
    for (const e of day.events) {
      if (e.whole) continue;
      const [from, to] = spanOn(e, day.dayStart, day.dayEnd);
      first = Math.min(first, Math.floor(from));
      last = Math.max(last, Math.ceil(to));
    }
  }
  if (first >= last) [first, last] = WEEK_DEFAULT_HOURS;
  const missing = WEEK_MIN_HOURS - (last - first);
  if (missing > 0) {
    first = Math.max(0, first - Math.floor(missing / 2));
    last = Math.min(24, first + WEEK_MIN_HOURS);
    first = last - WEEK_MIN_HOURS;
  }
  return [first, last];
}

// Side-by-side lanes for overlapping events: each gets .lane and .lanes (lanes in its cluster).
function assignLanes(events) {
  const sorted = [...events].sort((a, b) => a.from - b.from || b.to - a.to);
  let cluster = [];
  let lanes = [];
  let clusterEnd = -1;
  const close = () => {
    for (const e of cluster) e.lanes = lanes.length;
    cluster = [];
    lanes = [];
  };
  for (const e of sorted) {
    if (cluster.length && e.from >= clusterEnd) close();
    let lane = lanes.findIndex((end) => end <= e.from);
    if (lane < 0) lane = lanes.push(e.to) - 1;
    else lanes[lane] = e.to;
    e.lane = lane;
    cluster.push(e);
    clusterEnd = Math.max(clusterEnd, e.to);
  }
  close();
  return sorted;
}

function weekView(days, now, colorOf) {
  const [first, last] = weekHours(days);
  const span = last - first;
  const at = (hour) => `${((Math.min(Math.max(hour, first), last) - first) / span) * 100}%`;
  const labelEvery = span > 14 ? 2 : 1;
  const weekendOf = (d) => d.getDay() === 0 || d.getDay() === 6;

  const heads = days.map(({ i, dayStart }) =>
    h("div", { class: `wk-day${i === 0 ? " today" : ""}${weekendOf(dayStart) ? " weekend" : ""}` },
      h("span", { class: "wk-name" }, i === 0 ? t.today : cap(fmt.shortWeekday.format(dayStart))),
      h("span", { class: "wk-date" }, dayStart.getDate())));

  const allDay = days.map(({ i, events }) => {
    const whole = events.filter((e) => e.whole);
    const shown = whole.length > 3 ? whole.slice(0, 2) : whole;
    return h("div", { class: `wk-cell${i === 0 ? " today" : ""}` },
      shown.map((e) => h("div", { class: "wk-chip", style: { "--c": colorOf(e) } }, e.title || t.noTitle)),
      whole.length > shown.length && h("div", { class: "wk-more" }, `+${whole.length - shown.length}`));
  });

  const hours = [];
  const lines = [];
  for (let hour = first; hour <= last; hour++) {
    lines.push(h("div", { class: "wk-line", style: { top: at(hour) } }));
    if ((hour - first) % labelEvery === 0 && hour < last) {
      hours.push(h("span", { class: "wk-hour", style: { top: at(hour) } }, pad(hour % 24)));
    }
  }

  const columns = days.map(({ i, dayStart, dayEnd, events }) => {
    const timed = assignLanes(events.filter((e) => !e.whole).map((e) => {
      const [from, to] = spanOn(e, dayStart, dayEnd);
      return { ...e, from, to };
    }));
    const blocks = timed.map((e) => {
      const classes = ["wk-event"];
      if (e.e <= now) classes.push("past");
      if (e.s <= now && now < e.e) classes.push("now");
      const top = Math.max(e.from, first);
      const bottom = Math.min(e.to, last);
      return h("div", {
        class: classes.join(" "),
        style: {
          "--c": colorOf(e),
          top: at(top),
          height: `${((bottom - top) / span) * 100}%`,
          left: `${(e.lane / e.lanes) * 100}%`,
          width: `${100 / e.lanes}%`,
        },
      },
      // Title first so short events still show it; the grid already gives the time roughly.
      h("span", { class: "wk-title" }, e.title || t.noTitle),
      e.s >= dayStart && h("span", { class: "wk-time" }, ` ${fmt.time.format(e.s)}`));
    });
    const nowHour = hourOf(now);
    const nowLine = i === 0 && nowHour >= first && nowHour < last && h("div", { class: "wk-now", style: { top: at(nowHour) } });
    return h("div", { class: `wk-col${i === 0 ? " today" : ""}` }, blocks, nowLine);
  });

  return h("div", { class: "week" },
    h("div", { class: "wk-row wk-heads" }, h("div"), heads),
    h("div", { class: "wk-row wk-allday" }, h("div"), allDay),
    h("div", { class: "wk-row wk-body" }, lines, h("div", { class: "wk-hours" }, hours), columns));
}

function savedView() {
  try {
    return localStorage.getItem(VIEW_KEY) === "week" ? "week" : "agenda";
  } catch {
    return "agenda";
  }
}

function setView(view) {
  state.view = view;
  try {
    localStorage.setItem(VIEW_KEY, view);
  } catch {
    // Private browsing: the choice just isn't remembered.
  }
  renderCalendar();
}

// ---------- adding events ----------

function dateInputValue(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function rememberedCalendar() {
  try { return localStorage.getItem("fd:eventCalendar"); } catch { return null; }
}

function openEventSheet() {
  const calendars = ((state.calendar && state.calendar.calendars) || []).filter((c) => c.writable);
  if (!calendars.length) return;
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours() + 1);
  const end = new Date(start.getTime() + 3_600_000);
  $("ev-title").value = "";
  $("ev-location").value = "";
  $("ev-date").value = dateInputValue(start); // after 23:00 the next full hour is tomorrow
  $("ev-allday").checked = false;
  $("ev-start").value = `${pad(start.getHours())}:00`;
  $("ev-end").value = `${pad(end.getHours())}:00`;
  $("ev-error").hidden = true;
  const remembered = rememberedCalendar();
  const selected = calendars.some((c) => c.id === remembered) ? remembered : calendars[0].id;
  $("ev-calendars").replaceChildren(...calendars.map((c) => h("button", {
    type: "button", class: `chip${c.id === selected ? " on" : ""}`, "data-id": c.id,
    onclick: (event) => {
      for (const chip of $("ev-calendars").children) chip.classList.remove("on");
      event.currentTarget.classList.add("on");
    },
  }, h("i", { style: { "--c": c.color } }), c.name)));
  syncEventSheet();
  $("event-sheet").hidden = false;
  $("ev-title").focus();
}

function syncEventSheet() {
  $("ev-times").hidden = $("ev-allday").checked;
  const today = dateInputValue(new Date());
  const tomorrow = dateInputValue(addDays(new Date(), 1));
  for (const chip of $("ev-days").querySelectorAll(".chip")) {
    const value = chip.dataset.offset === "0" ? today : tomorrow;
    chip.classList.toggle("on", $("ev-date").value === value);
  }
}

function closeEventSheet() {
  $("event-sheet").hidden = true;
}

async function saveEvent(event) {
  event.preventDefault();
  const error = $("ev-error");
  const title = $("ev-title").value.trim();
  const day = $("ev-date").value;
  const chip = $("ev-calendars").querySelector(".chip.on");
  error.hidden = true;
  if (!title) {
    error.textContent = t.needTitle;
    error.hidden = false;
    $("ev-title").focus();
    return;
  }
  const payload = { calendar: chip.dataset.id, title, location: $("ev-location").value.trim() };
  if ($("ev-allday").checked) {
    Object.assign(payload, { allDay: true, start: day });
  } else {
    const start = new Date(`${day}T${$("ev-start").value}`);
    let end = new Date(`${day}T${$("ev-end").value}`);
    if (end <= start) end = new Date(end.getTime() + 86_400_000); // e.g. 23:00–01:00
    Object.assign(payload, { allDay: false, start: isoLocal(start), end: isoLocal(end) });
  }
  $("ev-save").disabled = true;
  try {
    const created = await api("/api/events", { method: "POST", body: JSON.stringify(payload) });
    try { localStorage.setItem("fd:eventCalendar", payload.calendar); } catch { /* private mode */ }
    closeEventSheet();
    toast(t.added(title), { label: t.undo, run: () => removeEvent(created) });
    loadCalendar();
  } catch (err) {
    error.textContent = err.message || t.saveFailed;
    error.hidden = false;
  } finally {
    $("ev-save").disabled = false;
  }
}

async function removeEvent(created) {
  const query = new URLSearchParams({ calendar: created.calendar, id: created.eventId });
  try {
    await api(`/api/events?${query}`, { method: "DELETE" });
    toast(t.removed);
  } catch (err) {
    toast(err.message || t.saveFailed);
  }
  loadCalendar();
}

$("add-event").addEventListener("click", openEventSheet);
$("ev-cancel").addEventListener("click", closeEventSheet);
$("event-sheet").addEventListener("click", (event) => { if (event.target === $("event-sheet")) closeEventSheet(); });
$("event-form").addEventListener("submit", saveEvent);
$("ev-allday").addEventListener("change", syncEventSheet);
$("ev-date").addEventListener("change", syncEventSheet);
for (const chip of $("ev-days").querySelectorAll(".chip")) {
  chip.addEventListener("click", () => {
    $("ev-date").value = dateInputValue(addDays(new Date(), Number(chip.dataset.offset)));
    syncEventSheet();
  });
}

// ---------- weather ----------

const WEATHER_ICONS = [
  [[0], "☀️", "🌙"], [[1], "🌤️", "🌙"], [[2], "⛅", "☁️"], [[3], "☁️"], [[45, 48], "🌫️"],
  [[51, 53, 55, 56, 57], "🌦️"], [[61, 63, 65, 66, 67], "🌧️"], [[80, 81, 82], "🌦️"],
  [[71, 73, 75, 77, 85, 86], "🌨️"], [[95, 96, 99], "⛈️"],
];

function weatherIcon(code, isDay = true) {
  const entry = WEATHER_ICONS.find(([codes]) => codes.includes(code));
  if (!entry) return "🌡️";
  return !isDay && entry[2] ? entry[2] : entry[1];
}

const degrees = (value) => (value == null ? "–" : `${Math.round(value)}°`);

async function loadWeather() {
  if (!state.config.weather) return;
  try {
    state.weather = await api("/api/weather");
  } catch {
    // Keep the last forecast on a blip; the header just shows what it has.
  }
  renderWeather();
}

function renderWeather() {
  const el = $("weather");
  const data = state.weather;
  const now = new Date();
  const tomorrow = now.getHours() >= TOMORROW_WEATHER_FROM;
  const day = data && data.days[tomorrow ? 1 : 0];
  if (!day) {
    el.hidden = true;
    return;
  }

  const icon = tomorrow ? weatherIcon(day.code) : weatherIcon(data.now.code, data.now.isDay);
  const extras = [];
  if (day.rainChance >= 30) extras.push(`☔ ${day.rainChance}%`);
  if (day.wind >= 10) extras.push(`💨 ${Math.round(day.wind)} m/s`);
  const slots = [8, 12, 16, 20]
    .map((hour) => data.hours.find((x) => x.time === `${day.date}T${pad(hour)}:00`))
    .filter((x) => x && (tomorrow || Number(x.time.slice(11, 13)) > now.getHours()));

  el.replaceChildren(
    h("div", { class: "weather-main" },
      h("span", { class: "weather-icon" }, icon),
      !tomorrow && h("span", { class: "weather-temp" }, degrees(data.now.temp)),
      h("div", { class: "weather-text" },
        h("div", { class: "weather-label" }, tomorrow ? t.tomorrow : t.today),
        h("div", { class: "weather-range" }, `↑${degrees(day.max)} ↓${degrees(day.min)}`),
        extras.length > 0 && h("div", { class: "weather-extra" }, extras.join("  ")))),
    slots.length > 0 && h("div", { class: "weather-hours" }, slots.map((x) => h("div", { class: "weather-hour" },
      h("div", { class: "h" }, x.time.slice(11, 13)),
      h("div", { class: "i" }, weatherIcon(x.code, x.isDay)),
      h("div", {}, degrees(x.temp))))));
  el.hidden = false;
}

// ---------- points & balances ----------

async function loadAccounts() {
  if (state.config.accounts === false) return;
  try {
    state.accounts = await api("/api/accounts");
  } catch {
    // Not important enough to show an error on the kitchen screen.
  }
  renderAccounts();
}

function renderAccounts() {
  const items = (state.accounts && state.accounts.items) || [];
  $("accounts-panel").hidden = items.length === 0 && !state.config.financesUrl;
  fitSide();
  const locale = document.documentElement.lang === "sv" ? "sv-SE" : "en-GB";
  const amount = (item) => {
    if (item.unit === "points") return `${new Intl.NumberFormat(locale).format(item.value)} ${t.points}`;
    try {
      const decimals = item.decimals && !Number.isInteger(item.value) ? item.decimals : 0; // Split shows öre
      return new Intl.NumberFormat(locale, {
        style: "currency", currency: item.unit, minimumFractionDigits: decimals, maximumFractionDigits: decimals,
      }).format(item.value);
    } catch {
      return `${Math.round(item.value)} ${item.unit}`;
    }
  };
  const detail = (item) => {
    if (item.debtor) return t.owes(item.debtor, item.creditor);
    if (item.name === "Split") return t.settled;
    return [item.detail, item.updated && t.updatedOn(fmt.shortDate.format(parseWhen(item.updated, true)))]
      .filter(Boolean).join(" · ");
  };
  $("accounts").replaceChildren(...items.map((item) => h("div", {},
    h("div", { class: "account" },
      h("span", { class: "name" }, item.name),
      h("span", { class: "value" }, amount(item))),
    h("div", { class: "account-detail" }, detail(item)))));
}

// ---------- lists ----------

const listPanels = new Map(); // key -> { panel, count, body, form }

async function loadLists() {
  if (state.pendingWrites) return; // don't clobber an optimistic update
  try {
    const data = await api("/api/lists");
    if (state.pendingWrites) return;
    state.lists = data.lists;
    state.listsError = null;
  } catch (err) {
    if (!(state.lists && (err.code === "network" || err.code === "keep_error"))) state.listsError = err;
  }
  renderLists();
}

function replaceList(updated) {
  if (!state.lists) return;
  state.lists = state.lists.map((l) => (l.id === updated.id ? updated : l));
}

async function toggleItem(list, item) {
  const checked = !item.checked;
  item.checked = checked;
  if (checked) {
    state.lingering.set(item.id, Date.now() + LINGER_MS);
    setTimeout(renderLists, LINGER_MS + 50);
  } else {
    state.lingering.delete(item.id);
  }
  state.pendingWrites++;
  renderLists();
  try {
    replaceList(await api(`/api/lists/${encodeURIComponent(list.id)}/items/${encodeURIComponent(item.id)}`, {
      method: "PUT", body: JSON.stringify({ checked }),
    }));
  } catch (err) {
    item.checked = !checked;
    state.lingering.delete(item.id);
    toast(err.code === "not_found" ? err.message : t.saveFailed);
  } finally {
    state.pendingWrites--;
    renderLists();
  }
}

async function addItem(list, input) {
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  state.pendingWrites++;
  try {
    replaceList(await api(`/api/lists/${encodeURIComponent(list.id)}/items`, {
      method: "POST", body: JSON.stringify({ text }),
    }));
  } catch (err) {
    if (!input.value) input.value = text;
    toast(t.saveFailed);
  } finally {
    state.pendingWrites--;
    renderLists();
  }
}

function itemButton(list, item) {
  const li = h("li", { "data-id": item.id, "data-indented": item.indented ? "1" : null },
    h("button", {
      type: "button",
      class: `item${item.checked ? " checked" : ""}${item.indented ? " indented" : ""}`,
      onclick: () => toggleItem(list, item),
    }, h("span", { class: "box" }), h("span", { class: "text" }, item.text)));
  if (!item.checked && !item.indented) {
    li.append(h("span", { class: "grip", "aria-hidden": "true", onpointerdown: (e) => dragItem(e, list, item, li) }, "⠿"));
  }
  return li;
}

// Drag an unchecked item by its grip to a new place; the order is saved to Keep on release.
function dragItem(event, list, item, li) {
  event.preventDefault();
  const ul = li.parentElement;
  const grip = event.currentTarget;
  const before = [...ul.children].map((el) => el.dataset.id).join("|");
  grip.setPointerCapture(event.pointerId);
  state.dragging = true;
  li.classList.add("dragging");

  const middle = (el) => { const r = el.getBoundingClientRect(); return r.top + r.height / 2; };
  const move = (e) => {
    let prev;
    while ((prev = li.previousElementSibling) && e.clientY < middle(prev)) ul.insertBefore(li, prev);
    let next;
    while ((next = li.nextElementSibling) && e.clientY > middle(next)) ul.insertBefore(next, li);
  };
  const end = (e) => {
    grip.removeEventListener("pointermove", move);
    grip.removeEventListener("pointerup", end);
    grip.removeEventListener("pointercancel", end);
    li.classList.remove("dragging");
    state.dragging = false;
    if (e.type === "pointercancel" || [...ul.children].map((el) => el.dataset.id).join("|") === before) {
      renderLists();
      return;
    }
    // Keep orders top-level items; land below the nearest top-level item above.
    let above = li.previousElementSibling;
    while (above && above.dataset.indented) above = above.previousElementSibling;
    moveItem(list, item, above ? above.dataset.id : null);
  };
  grip.addEventListener("pointermove", move);
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
}

async function moveItem(list, item, afterId) {
  const current = (state.lists || []).find((l) => l.id === list.id);
  if (current) {
    const moved = current.items.find((it) => it.id === item.id);
    const items = current.items.filter((it) => it !== moved);
    if (moved) items.splice(afterId ? items.findIndex((it) => it.id === afterId) + 1 : 0, 0, moved);
    current.items = items;
  }
  state.pendingWrites++;
  renderLists();
  try {
    replaceList(await api(`/api/lists/${encodeURIComponent(list.id)}/items/${encodeURIComponent(item.id)}/position`, {
      method: "PUT", body: JSON.stringify({ after: afterId }),
    }));
  } catch (err) {
    toast(err.code === "not_found" ? err.message : t.saveFailed);
    state.pendingWrites--;
    await loadLists();
    return;
  }
  state.pendingWrites--;
  renderLists();
}

function listPanel(key) {
  let entry = listPanels.get(key);
  if (entry) return entry;
  const title = h("h2");
  const count = h("span", { class: "badge", hidden: true });
  const input = h("input", { type: "text", placeholder: t.addItem, enterkeyhint: "done", autocomplete: "off", autocorrect: "on" });
  const form = h("form", { class: "add" }, input, h("button", { type: "submit", "aria-label": t.addItem }, "+"));
  const body = h("div", { class: "panel-body" });
  const panel = h("section", { class: "panel list" }, h("header", { class: "panel-head" }, title, count), form, body);
  entry = { panel, title, count, form, input, body, list: null };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (entry.list) addItem(entry.list, input);
  });
  listPanels.set(key, entry);
  return entry;
}

// The right-hand column holds the balances (and the finances link) and the second half of the lists.
function fitSide() {
  const empty = $("accounts-panel").hidden && !$("more-lists").childElementCount;
  $("side").hidden = empty;
  document.body.classList.toggle("no-side", empty);
}

function placeLists(panels) {
  const split = Math.ceil(panels.length / 2);
  $("lists").replaceChildren(...panels.slice(0, split));
  $("more-lists").replaceChildren(...panels.slice(split));
  fitSide();
}

function renderLists() {
  if (state.dragging) return; // don't pull the list out from under a finger
  if (!state.lists) {
    if (state.listsError) {
      placeLists([h("section", { class: "panel" },
        h("header", { class: "panel-head" }, h("h2", {}, t.lists)),
        h("div", { class: "panel-body" }, problem(state.listsError)))]);
      listPanels.clear();
    }
    return;
  }
  if (!state.lists.length) {
    placeLists([h("section", { class: "panel" },
      h("header", { class: "panel-head" }, h("h2", {}, t.lists)),
      h("div", { class: "panel-body" }, h("div", { class: "empty" }, t.noSharedNotes)))]);
    listPanels.clear();
    return;
  }

  const keys = state.lists.map((l, i) => l.id || `missing-${i}`);
  if (keys.join("|") !== [...listPanels.keys()].join("|")) {
    listPanels.clear();
    placeLists(keys.map((key) => listPanel(key).panel));
  }

  const now = Date.now();
  state.lists.forEach((list, i) => {
    const entry = listPanel(keys[i]);
    entry.list = list.error || list.kind === "note" ? null : list;
    entry.title.textContent = list.title;
    if (list.kind === "note") {
      // Shared text notes are shown read-only.
      entry.form.hidden = true;
      entry.count.hidden = true;
      entry.body.replaceChildren(h("div", { class: "note-text" }, list.text || ""));
      return;
    }
    entry.form.hidden = !!list.error;
    if (list.error) {
      entry.count.hidden = true;
      entry.body.replaceChildren(h("div", { class: "problem" },
        list.error === "not_a_checklist" ? t.notChecklist(list.title) : t.listMissing(list.title)));
      return;
    }

    const open = list.items.filter((it) => !it.checked || (state.lingering.get(it.id) || 0) > now);
    const done = list.items.filter((it) => !open.includes(it));
    const remaining = list.items.filter((it) => !it.checked).length;
    entry.count.hidden = remaining === 0;
    entry.count.textContent = String(remaining);

    const showDone = state.showChecked.has(list.id);
    const children = [
      open.length
        ? h("ul", { class: "items" }, open.map((it) => itemButton(list, it)))
        : h("div", { class: "empty" }, t.allDone),
    ];
    if (done.length) {
      children.push(h("button", {
        type: "button",
        class: "done-toggle",
        onclick: () => {
          if (showDone) state.showChecked.delete(list.id);
          else state.showChecked.add(list.id);
          renderLists();
        },
      }, showDone ? t.hideChecked : t.checked(done.length)));
      if (showDone) children.push(h("ul", { class: "items" }, done.map((it) => itemButton(list, it))));
    }
    entry.body.replaceChildren(...children);
  });

  for (const [id, until] of state.lingering) if (until <= now) state.lingering.delete(id);
}

// ---------- sign-in ----------

function nextPath() {
  const next = new URLSearchParams(location.search).get("next");
  return next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
}

function showSignIn(methods = {}) {
  if (state.locked) return;
  state.locked = true;
  $("unlock").hidden = false;
  const google = $("google-signin");
  google.hidden = !methods.google;
  google.href = `/auth/login?next=${encodeURIComponent(nextPath())}`;
  $("unlock-form").hidden = !methods.key;
  if (methods.key && !methods.google) $("unlock-key").focus();
}

function showSignInError() {
  const params = new URLSearchParams(location.search);
  const message = params.get("signin_error");
  if (!message) return;
  const error = $("unlock-error");
  error.textContent = message;
  error.hidden = false;
  params.delete("signin_error"); // keep ?next= for the sign-in button
  history.replaceState(null, "", params.toString() ? `/?${params}` : "/");
}

$("unlock-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("unlock-error");
  error.hidden = true;
  try {
    await api("/api/unlock", { method: "POST", body: JSON.stringify({ key: $("unlock-key").value }) });
    location.replace(nextPath());
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
});

// ---------- voice commands ----------
//
// Tap the mic and speak, or switch on hands-free and start with "ASCA". Safari turns speech into
// text (sv-SE); the server has Claude turn the text into list items, events or Split expenses.

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
// The wake word (ASSISTANT_NAME, default "ASCA") and how speech recognition mishears it; set from /api/config.
let WAKE = /\b(asca)\b[\s,.:!]*/i;
const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const HANDS_FREE_KEY = "voiceHandsFree";
const AWAIT_COMMAND_MS = 8000; // after a bare "ASCA", the next sentence is the command
const NOTHING_HEARD_MS = 8000; // tap-to-talk gives up if Safari sends nothing at all
const PAUSE_ENDS_MS = 2000; // Safari doesn't always mark a sentence final; a pause ends it

const voice = { recognizer: null, mode: null, busy: false, awaitingUntil: 0, handsFree: false };

function voiceBubble(text, kind = "") {
  const el = $("voice-bubble");
  el.className = `voice-bubble ${kind}`;
  el.textContent = text;
  el.hidden = !text;
}

function renderVoiceButtons() {
  $("mic").classList.toggle("active", voice.mode === "once");
  $("hands-free").classList.toggle("active", voice.handsFree);
  $("hands-free").setAttribute("aria-pressed", String(voice.handsFree));
}

function stopListening() {
  const r = voice.recognizer;
  const wasOnce = voice.mode === "once";
  voice.recognizer = null;
  voice.mode = null;
  clearTimeout(voice.timer);
  if (r) { r.onend = null; r.onerror = null; r.onresult = null; r.abort(); }
  if (wasOnce && !voice.busy) voiceBubble("");
  renderVoiceButtons();
}

// Tell the server what went wrong, so it shows up in the logs (no audio or text is sent).
function reportVoice(problem) {
  const body = { problem, standalone: !!(navigator.standalone || matchMedia("(display-mode: standalone)").matches) };
  api("/api/voice/report", { method: "POST", body: JSON.stringify(body) }).catch(() => {});
}

function voiceProblem(message, problem) {
  voiceBubble(message, "error");
  setTimeout(() => { if (!voice.busy && !voice.recognizer) voiceBubble(""); }, 5000);
  if (problem) reportVoice(problem);
}

function listen(mode) {
  stopListening();
  const r = new Recognition();
  r.lang = "sv-SE";
  r.interimResults = true;
  r.continuous = mode === "wake";
  voice.recognizer = r;
  voice.mode = mode;
  let heard = ""; // the latest words not yet marked final
  let gotAnything = false;
  let failed = false;
  const finish = () => {
    // Use what was heard even if Safari never marked it final.
    const said = heard;
    heard = "";
    if (said) onHeard(said, mode);
  };
  if (mode === "once") {
    voice.timer = setTimeout(() => {
      if (voice.recognizer !== r || gotAnything) return;
      stopListening();
      voiceProblem(t.heardNothing, "no-result-in-8s");
    }, NOTHING_HEARD_MS);
  }
  r.onresult = (event) => {
    gotAnything = true;
    let interim = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      if (result.isFinal) {
        heard = "";
        onHeard(result[0].transcript, mode);
      } else {
        interim += result[0].transcript;
      }
    }
    if (!interim || voice.recognizer !== r) return;
    heard = interim;
    clearTimeout(voice.timer);
    voice.timer = setTimeout(() => { if (voice.recognizer === r) finish(); }, PAUSE_ENDS_MS);
    if (mode === "once" || voice.awaitingUntil > Date.now() || WAKE.test(interim)) {
      voiceBubble(interim.replace(WAKE, "") || t.listening, "listening");
    }
  };
  r.onerror = (event) => {
    failed = event.error !== "aborted";
    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      voice.handsFree = false;
      saveHandsFree();
      voiceProblem(t.micBlocked, event.error);
    } else if (mode === "once" && event.error !== "aborted") {
      voiceProblem(event.error === "no-speech" ? t.heardNothing : `${t.voiceFailed} (${event.error})`, event.error);
    } else if (event.error !== "no-speech" && event.error !== "aborted") {
      reportVoice(event.error);
    }
  };
  r.onend = () => {
    if (voice.recognizer !== r) return;
    clearTimeout(voice.timer);
    voice.recognizer = null;
    voice.mode = null;
    if (heard) finish();
    else if (mode === "once" && !voice.busy && !gotAnything && !failed) voiceProblem(t.heardNothing, "ended-without-result");
    renderVoiceButtons();
    // Safari ends continuous recognition every so often; pick it up again while hands-free is on.
    if (voice.handsFree && !voice.busy) setTimeout(resumeHandsFree, 300);
  };
  try {
    r.start();
  } catch (err) {
    voice.recognizer = null;
    voice.mode = null;
    clearTimeout(voice.timer);
    voiceProblem(t.voiceFailed, `start-failed: ${err && err.name}`);
  }
  renderVoiceButtons();
  if (mode === "once") voiceBubble(t.listening, "listening");
}

function onHeard(transcript, mode) {
  const said = transcript.trim();
  if (!said) return;
  if (mode === "once") {
    stopListening();
    runCommand(said.replace(WAKE, ""));
    return;
  }
  const wake = said.match(WAKE);
  if (wake) {
    const command = said.slice(wake.index + wake[0].length).trim();
    if (command) runCommand(command);
    else {
      voice.awaitingUntil = Date.now() + AWAIT_COMMAND_MS;
      voiceBubble(t.listening, "listening");
      setTimeout(() => { if (voice.awaitingUntil <= Date.now() && !voice.busy) voiceBubble(""); }, AWAIT_COMMAND_MS + 100);
    }
  } else if (voice.awaitingUntil > Date.now()) {
    runCommand(said);
  }
}

function resumeHandsFree() {
  if (voice.handsFree && !voice.recognizer && !voice.busy && document.visibilityState === "visible") listen("wake");
}

function saveHandsFree() {
  try { localStorage.setItem(HANDS_FREE_KEY, voice.handsFree ? "1" : ""); } catch { /* private mode */ }
  renderVoiceButtons();
}

function formatKronor(ore) {
  const locale = document.documentElement.lang === "sv" ? "sv-SE" : "en-GB";
  return new Intl.NumberFormat(locale, { style: "currency", currency: "SEK", maximumFractionDigits: 0 }).format(ore / 100);
}

function describeAction(action) {
  if (action.type === "undo") return `${t.undone}: ${action.what}`;
  if (action.type === "list_items") return `${action.items.join(", ")} → ${action.list}`;
  if (action.type === "event") {
    const first = parseWhen(action.start, action.allDay);
    let when = fmt.shortDate.format(first);
    if (action.allDay && action.end !== action.start) when += `–${fmt.shortDate.format(parseWhen(action.end, true))}`;
    if (!action.allDay) when += ` ${fmt.time.format(first)}`;
    return `${action.title} · ${when} → ${action.calendar}`;
  }
  const who = splitName(action.paid_by);
  return `${action.description} ${formatKronor(action.amount)} · ${t.paidBy(who)} → Split`;
}

// ---- weather and answers in a card ----

const ADDING = ["list_items", "event", "expense", "undo"];
const WEATHER_SV = [
  [[0], "klart"], [[1], "mestadels klart"], [[2], "halvklart"], [[3], "mulet"], [[45, 48], "dimma"],
  [[51, 53, 55, 56, 57], "duggregn"], [[61, 63, 65, 66, 67], "regn"], [[80, 81, 82], "regnskurar"],
  [[71, 73, 75, 77, 85, 86], "snö"], [[95, 96, 99], "åska"],
];
const weatherSv = (code) => (WEATHER_SV.find(([codes]) => codes.includes(code)) || [null, "växlande väder"])[1];
const round = (value) => Math.round(value);
let sheetTimer;

function showSheet(title, ...body) {
  $("vs-title").textContent = title;
  $("vs-body").replaceChildren(...body);
  $("voice-sheet").hidden = false;
  clearTimeout(sheetTimer);
  sheetTimer = setTimeout(closeSheet, 3 * 60_000); // don't leave it over the dashboard all day
}

function closeSheet() {
  clearTimeout(sheetTimer);
  $("voice-sheet").hidden = true;
}

async function showAnswer(action) {
  if (action.type === "answer") {
    showSheet(action.title || t.voice, h("div", { class: "answer-text" }, action.text));
    return action.spoken;
  }
  if (!state.weather) await loadWeather();
  const data = state.weather;
  const tomorrow = action.day === "tomorrow";
  const day = data && data.days[tomorrow ? 1 : 0];
  if (!day) return "Jag kan inte se någon väderprognos just nu.";
  const now = new Date();
  const hours = data.hours.filter((x) => x.time.startsWith(day.date) && Number(x.time.slice(11, 13)) % 3 === 0
    && (tomorrow || Number(x.time.slice(11, 13)) > now.getHours()));
  showSheet(`${tomorrow ? t.tomorrow : t.today} · ${data.place}`,
    h("div", { class: "sheet-weather" },
      h("span", { class: "weather-icon" }, tomorrow ? weatherIcon(day.code) : weatherIcon(data.now.code, data.now.isDay)),
      h("span", { class: "weather-temp" }, degrees(tomorrow ? day.max : data.now.temp)),
      h("div", { class: "weather-text" },
        h("div", { class: "weather-label" }, weatherSv(tomorrow ? day.code : data.now.code)),
        h("div", { class: "weather-range" }, `↑${degrees(day.max)} ↓${degrees(day.min)}`),
        h("div", { class: "weather-extra" }, `☔ ${day.rainChance ?? 0}%  💨 ${round(day.wind ?? 0)} m/s`))),
    hours.length > 0 && h("div", { class: "sheet-hours" }, hours.map((x) => h("div", { class: "weather-hour" },
      h("div", { class: "h" }, x.time.slice(11, 13)),
      h("div", { class: "i" }, weatherIcon(x.code, x.isDay)),
      h("div", {}, degrees(x.temp))))));
  const parts = tomorrow
    ? [`Imorgon i ${data.place} blir det ${weatherSv(day.code)}, mellan ${round(day.min)} och ${round(day.max)} grader.`]
    : [`Just nu är det ${round(data.now.temp)} grader och ${weatherSv(data.now.code)} i ${data.place}.`,
      `Idag blir det som varmast ${round(day.max)} och som kallast ${round(day.min)} grader.`];
  if ((day.rainChance ?? 0) >= 30) parts.push(`Det är ${day.rainChance} procents risk för regn.`);
  if ((day.wind ?? 0) >= 10) parts.push(`Det blåser upp till ${round(day.wind)} meter per sekund.`);
  return parts.join(" ");
}

// ---- the spoken answer, always in Swedish ----

const SV_DAY = new Intl.DateTimeFormat("sv-SE", { day: "numeric", month: "long" });
const SV_TIME = new Intl.DateTimeFormat("sv-SE", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

function svList(words) {
  return words.length > 1 ? `${words.slice(0, -1).join(", ")} och ${words[words.length - 1]}` : words[0];
}

function svKronor(ore) {
  const kr = Math.round(ore / 100);
  return `${new Intl.NumberFormat("sv-SE").format(kr)} kronor`;
}

function spokenAnswer(action) {
  if (action.type === "undo") return action.what ? `Okej, jag har ångrat ${action.what}.` : "Okej, det är ångrat.";
  if (action.type === "list_items") {
    const verb = action.items.length > 1 ? "är tillagda" : "är tillagt";
    return `${svList(action.items)} ${verb} i ${action.list}.`;
  }
  if (action.type === "event") {
    const start = parseWhen(action.start, action.allDay);
    let when = `den ${SV_DAY.format(start)}`;
    if (action.allDay && action.end !== action.start) {
      const end = parseWhen(action.end, true);
      when = start.getMonth() === end.getMonth()
        ? `den ${start.getDate()} till ${SV_DAY.format(end)}`
        : `den ${SV_DAY.format(start)} till ${SV_DAY.format(end)}`;
    }
    if (!action.allDay) when += ` klockan ${SV_TIME.format(start)}`;
    return `${action.title} är inlagt ${when} i kalendern ${action.calendar}.`;
  }
  const who = splitName(action.paid_by);
  return `${action.description} på ${svKronor(action.amount)}, betalt av ${who}, är tillagt i Split.`;
}

function swedishVoice() {
  const voices = speechSynthesis.getVoices();
  return voices.find((v) => v.lang === "sv-SE" && v.localService) || voices.find((v) => v.lang.startsWith("sv"));
}

// iPad Safari only speaks after a tap has started speech once; call this in tap handlers.
function unlockSpeech() {
  if (!("speechSynthesis" in window) || voice.speechUnlocked) return;
  const silent = new SpeechSynthesisUtterance(" ");
  silent.volume = 0;
  speechSynthesis.speak(silent);
  voice.speechUnlocked = true;
}

function say(text) {
  return new Promise((resolve) => {
    if (!text || !("speechSynthesis" in window)) return resolve();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "sv-SE";
    const sv = swedishVoice();
    if (sv) utterance.voice = sv;
    const done = () => { clearTimeout(fallback); resolve(); };
    const fallback = setTimeout(done, 3000 + text.length * 90); // in case Safari never fires onend
    utterance.onend = done;
    utterance.onerror = done;
    speechSynthesis.cancel();
    speechSynthesis.speak(utterance);
  });
}

async function runCommand(text) {
  voice.awaitingUntil = 0;
  if (!text) {
    voiceBubble(t.heardNothing, "error");
    setTimeout(() => voiceBubble(""), 2500);
    return;
  }
  voice.busy = true;
  const resume = voice.handsFree;
  stopListening(); // so the answer read aloud isn't heard as a new command
  voiceBubble(`“${text}” · ${t.thinking}`, "thinking");
  try {
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const result = await api("/api/assistant", { method: "POST", body: JSON.stringify({ text, timezone }), timeout: 60_000 });
    voiceBubble("");
    const added = result.done.filter((a) => ADDING.includes(a.type));
    const shown = result.done.filter((a) => !ADDING.includes(a.type));
    const lines = added.map(describeAction);
    // A long text reply is an answer the model forgot to put in a card; show it as one.
    const replyInCard = !result.done.length && (result.reply || "").length > 120;
    if (replyInCard) showSheet(t.answered, h("div", { class: "answer-text" }, result.reply));
    const message = [...lines, ...result.failed, result.done.length || replyInCard ? "" : result.reply].filter(Boolean).join(" · ");
    const undo = result.undoable ? { label: t.undo, run: () => undoVoice(result.log_id) } : null;
    if (message || (!shown.length && !replyInCard)) toast(message || t.heardNothing, undo);
    const touched = result.touched || [];
    if (touched.includes("list_items")) loadLists();
    if (touched.includes("event")) loadCalendar();
    if (touched.includes("expense")) loadAccounts();
    const spoken = [];
    if (added.some((a) => a.type !== "undo")) spoken.push("Visst!");
    spoken.push(...added.map(spokenAnswer));
    for (const action of shown) spoken.push(await showAnswer(action));
    spoken.push(...result.failed);
    if (!result.done.length) spoken.push(result.reply || "Jag förstod inte riktigt.");
    await say(spoken.filter(Boolean).join(" ")); // hands-free listens again only after this, so it doesn't hear itself
  } catch (err) {
    voiceBubble(err.message || t.saveFailed, "error");
    setTimeout(() => voiceBubble(""), 5000);
    await say(err.message);
  } finally {
    voice.busy = false;
    if (resume) setTimeout(resumeHandsFree, 500);
  }
}

async function undoVoice(logId) {
  try {
    await api(`/api/voice/log/${encodeURIComponent(logId)}/undo`, { method: "POST" });
    toast(t.removed);
  } catch (err) {
    toast(err.message || t.saveFailed);
  }
  loadLists();
  loadCalendar();
  loadAccounts();
}

// ---- the log of voice commands ----

async function showVoiceLog() {
  let entries = [];
  try {
    entries = (await api("/api/voice/log")).entries;
  } catch (err) {
    toast(err.message || t.couldNotLoad);
    return;
  }
  const today = new Date().toDateString();
  const when = (iso) => {
    const at = new Date(iso);
    return at.toDateString() === today ? fmt.time.format(at) : `${fmt.shortDate.format(at)} ${fmt.time.format(at)}`;
  };
  const outcome = (entry) => {
    const lines = entry.done.map((a) => (a.type === "answer" ? `${t.answered}: ${a.title}`
      : a.type === "weather" ? `${t.weatherShown}` : describeAction(a)));
    return [...lines, ...entry.failed, entry.done.length ? "" : entry.reply].filter(Boolean).join(" · ") || "–";
  };
  const row = (entry) => h("li", { class: `log-entry${entry.undone ? " undone" : ""}` },
    h("div", { class: "log-when" }, when(entry.at)),
    h("div", { class: "log-main" },
      h("div", { class: "log-said" }, `“${entry.text}”`),
      h("div", { class: "log-did" }, outcome(entry), entry.undone ? ` · ${t.undone}` : "")),
    entry.undoable && h("button", {
      type: "button",
      class: "log-undo",
      onclick: async (event) => {
        event.currentTarget.disabled = true;
        await undoVoice(entry.id);
        showVoiceLog();
      },
    }, t.undo));
  showSheet(t.voiceLog, entries.length
    ? h("ul", { class: "voice-log" }, entries.map(row))
    : h("div", { class: "empty" }, t.noVoiceLog));
}

const splitName = (id) => (state.config.split || {})[id] || id;

function setUpVoice() {
  if (!state.config.assistant || !Recognition) return;
  const { name, aliases } = state.config.assistant;
  WAKE = new RegExp(`\\b(${[name, ...aliases].map(escapeRegExp).join("|")})\\b[\\s,.:!]*`, "i");
  $("hands-free").textContent = name;
  $("hands-free").setAttribute("aria-label", t.handsFree(name));
  $("vs-close").addEventListener("click", () => { closeSheet(); speechSynthesis.cancel(); });
  $("voice-log").addEventListener("click", showVoiceLog);
  $("voice-sheet").addEventListener("click", (event) => { if (event.target === $("voice-sheet")) closeSheet(); });
  $("voice").hidden = false;
  $("mic").addEventListener("click", () => {
    unlockSpeech();
    if (voice.mode === "once") stopListening();
    else listen("once");
  });
  $("hands-free").addEventListener("click", () => {
    unlockSpeech();
    voice.handsFree = !voice.handsFree;
    saveHandsFree();
    if (voice.handsFree) {
      voiceBubble(t.sayName(state.config.assistant.name), "listening");
      setTimeout(() => { if (!voice.busy && voice.awaitingUntil <= Date.now()) voiceBubble(""); }, 3000);
      listen("wake");
    } else {
      stopListening();
    }
  });
  try { voice.handsFree = localStorage.getItem(HANDS_FREE_KEY) === "1"; } catch { /* private mode */ }
  renderVoiceButtons();
  // Safari may need a tap before it listens again after a reload; the first tap anywhere resumes.
  resumeHandsFree();
  document.addEventListener("pointerdown", () => { if (voice.handsFree && !voice.recognizer) resumeHandsFree(); }, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") resumeHandsFree();
    else stopListening();
  });
}

// ---------- screen & lifecycle ----------

let wakeLock = null;
async function keepAwake() {
  if (!("wakeLock" in navigator) || wakeLock || document.visibilityState !== "visible") return;
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => { wakeLock = null; });
  } catch {
    // Needs a user gesture on some versions of Safari; the next tap tries again.
  }
}

function loadAll() {
  if (state.locked) return;
  loadCalendar();
  loadLists();
  loadWeather();
  loadAccounts();
}

function every(ms, fn) {
  setInterval(() => { if (!state.locked && document.visibilityState === "visible") fn(); }, ms);
}

async function init() {
  setLanguage();
  showSignInError();
  tickClock();
  setInterval(tickClock, 1000);

  try {
    state.config = await api("/api/config");
  } catch (err) {
    if (err.code === "signin_required") return;
    state.config = {};
  }

  setLanguage(state.config.language);
  tickClock();
  if (state.config.title) {
    $("family-title").textContent = state.config.title;
    document.title = state.config.title;
  }
  if (state.config.demo) $("family-title").append(" ", h("span", { class: "pill" }, t.demo));
  $("setup-link").hidden = !(state.config.user && state.config.user.role === "admin"); // /setup is admin-only
  setUpVoice();
  if (state.config.financesUrl) {
    $("finances-link").href = state.config.financesUrl;
    $("finances-link").hidden = false;
  }
  renderAccounts();
  state.view = savedView();
  for (const button of $("view-switch").children) button.addEventListener("click", () => setView(button.dataset.view));

  $("refresh").addEventListener("click", async () => {
    $("refresh").classList.add("spinning");
    try { await api("/api/refresh", { method: "POST" }); } catch { /* reload anyway */ }
    location.reload(); // fresh data, and the newest version of the dashboard
  });
  $("clock").addEventListener("click", async () => {
    try { await api("/api/refresh", { method: "POST" }); } catch { /* status shows it */ }
    loadAll();
  });
  document.addEventListener("pointerdown", keepAwake, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      keepAwake();
      loadAll();
    }
  });

  keepAwake();
  loadAll();
  every(REFRESH.calendar, loadCalendar);
  every(REFRESH.lists, loadLists);
  every(REFRESH.weather, loadWeather);
  every(REFRESH.accounts, loadAccounts);
}

init();
