"use strict";

// The token can open the user's private chats, so it stays in memory only:
// reloading the page signs out. All page text is set through textContent.
const state = {
  token: null, username: "", roles: [], view: "chat", thread: null, threads: [], counts: {}, poll: null,
  bellPoll: null, unread: 0,
};
const root = document.getElementById("app");

const OUTCOMES = {
  ok: ["Completed", "good"],
  forwarded: ["Sent to accounts", "warn"],
  pending_approval: ["Waiting for approval", "warn"],
  flagged_nsfw: ["Blocked and reported", "bad"],
  denied: ["Denied", "bad"],
  denied_auth: ["Not allowed", "bad"],
  denied_quota: ["Rate limited", "bad"],
  denied_busy: ["Busy", "warn"],
  error: ["Failed", "bad"],
};

const REQUEST_LABELS = {
  manage_bill: "Record a bill",
  initiate_payment: "Make a payment",
  manage_order: "Change an order",
  book_service: "Book a service",
  order: "Order",
  service: "Service booking",
  bill: "Bill",
};

const REQUEST_STATES = {
  pending: ["Waiting for approval", "warn"],
  approved: ["Approved", "good"],
  denied: ["Denied", "bad"],
  failed: ["Failed", "bad"],
  queued: ["With the accounts team", "warn"],
  processing: ["In progress", "warn"],
  awaiting: ["Waiting for approval", "warn"],
  done: ["Done", "good"],
};
const FINISHED = new Set(["approved", "denied", "failed", "done"]);

const DEPARTMENTS = ["account", "hr", "logistics", "researcher", "superuser"];
const DEPARTMENT_NAMES = {
  account: "Accounts", hr: "HR", logistics: "Logistics", researcher: "Research", superuser: "Superuser", admin: "Admin",
};
const departmentName = (name) => DEPARTMENT_NAMES[name] || name;

const IDEAS = {
  account: "Show the ledger summary",
  logistics: "Track shipment SHP-8842",
  researcher: "Summarize the Q3 research notes",
  hr: "How many career emails came in?",
  superuser: "Check stock of laptops",
};

const ICONS = {
  plus: "M12 5v14M5 12h14",
  send: "M12 19V5M6 11l6-6 6 6",
  menu: "M4 7h16M4 12h16M4 17h16",
  pen: "M4 20l1-4L16 5l3 3L8 19l-4 1z",
  spark: "M12 3l1.9 5.6L19.5 10.5l-5.6 1.9L12 18l-1.9-5.6L4.5 10.5l5.6-1.9zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
  lock: "M7 11V8a5 5 0 0 1 10 0v3M5 11h14v10H5z",
  bell: "M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0",
  globe: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c2.5 2.5 3.8 5.5 3.8 9s-1.3 6.5-3.8 9c-2.5-2.5-3.8-5.5-3.8-9S9.5 5.5 12 3z",
};

// ---- small DOM helpers ----
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key in el) el[key] = value;
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({
    width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true",
  })) svg.setAttribute(k, v);
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

// The brand mark: a spark on the accent gradient. Size comes from the CSS class.
function logo(size = "") {
  return h("span", { class: `logo ${size}`, "aria-hidden": "true" }, icon("spark"));
}

function initials(name) {
  return (name || "?").trim().slice(0, 1).toUpperCase();
}

function show(...nodes) {
  root.replaceChildren(...nodes);
}

// replaceChildren would print `false` from a `condition && node` as text.
function fill(el, ...nodes) {
  el.replaceChildren(...nodes.flat().filter(Boolean));
}

function outcomePill(outcome) {
  const [text, kind] = OUTCOMES[outcome] || [outcome, ""];
  return h("span", { class: `pill ${kind}` }, text);
}

function errorBox() {
  const box = h("div", { class: "notice error", hidden: true, role: "alert" });
  box.fail = (err) => {
    box.textContent = err.message || String(err);
    box.hidden = false;
  };
  box.clear = () => { box.hidden = true; };
  return box;
}

function table(headers, rows, emptyText) {
  if (!rows.length) return h("div", { class: "empty" }, emptyText);
  const align = (index) => (typeof rows[0][index] === "number" ? "num" : null);
  return h("div", { class: "table-wrap" }, h("table", null,
    h("thead", null, h("tr", null, headers.map((text, i) => h("th", { scope: "col", class: align(i) }, text)))),
    h("tbody", null, rows.map((cells) => h("tr", null, cells.map((cell, i) => h("td", { class: align(i) }, cell))))),
  ));
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}


// ---- Markdown, rendered safely ----
// Replies come from the model and from web pages it read, so they are never put into innerHTML:
// this builds elements directly. Links open only http(s) and mailto, in a new tab.
const MD_BLOCK_START = /^\s*(```|~~~|#{1,6}\s|>|([-*+]|\d+[.)])\s+)/;
const MD_LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const MD_INLINE = /(`+)([^`]+?)\1|\*\*([^*]+?)\*\*|__([^_]+?)__|\*([^*\s][^*]*?)\*|(?<![\w])_([^_\s][^_]*?)_(?![\w])|~~([^~]+?)~~|\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

function mdLink(href, label) {
  if (!/^(https?:|mailto:)/i.test(href)) return label;
  return h("a", { href, target: "_blank", rel: "noopener noreferrer nofollow" }, label);
}

function mdInline(text) {
  const frag = document.createDocumentFragment();
  const re = new RegExp(MD_INLINE.source, "g");
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    if (m[2] !== undefined) frag.append(h("code", null, m[2]));
    else if (m[3] !== undefined || m[4] !== undefined) frag.append(h("strong", null, mdInline(m[3] ?? m[4])));
    else if (m[5] !== undefined || m[6] !== undefined) frag.append(h("em", null, mdInline(m[5] ?? m[6])));
    else if (m[7] !== undefined) frag.append(h("del", null, mdInline(m[7])));
    else if (m[8] !== undefined) frag.append(mdLink(m[9], mdInline(m[8])));
    else if (m[10] !== undefined) frag.append(mdLink(m[10], m[10]));
    last = re.lastIndex;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}

function mdList(lines, start) {
  const first = lines[start].match(MD_LIST_ITEM);
  const indent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const el = h(ordered ? "ol" : "ul");
  if (ordered && parseInt(first[2], 10) !== 1) el.start = parseInt(first[2], 10);
  let i = start;
  let li = null;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      let j = i + 1;
      while (j < lines.length && !lines[j].trim()) j++;
      const next = j < lines.length && lines[j].match(MD_LIST_ITEM);
      if (next && next[1].length >= indent) { i = j; continue; }
      break;
    }
    const m = line.match(MD_LIST_ITEM);
    const lead = line.match(/^\s*/)[0].length;
    if (m && m[1].length === indent) {
      li = h("li", null, mdInline(m[3]));
      el.append(li);
      i++;
    } else if (m && m[1].length > indent && li) {
      const [sub, next] = mdList(lines, i);
      li.append(sub);
      i = next;
    } else if (!m && lead > indent && li) {
      li.append(h("br"), mdInline(line.trim()));
      i++;
    } else {
      break;
    }
  }
  return [el, i];
}

function md(source) {
  const out = document.createDocumentFragment();
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    let m = line.match(/^\s*(```|~~~)\s*([\w+-]*)\s*$/);
    if (m) {
      const code = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(m[1])) code.push(lines[i++]);
      i++;
      out.append(h("pre", { class: "md-code" }, h("code", null, code.join("\n"))));
      continue;
    }
    m = line.match(/^\s*(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (m) {
      out.append(h(["h3", "h4", "h5", "h5", "h6", "h6"][m[1].length - 1], null, mdInline(m[2])));
      i++;
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.append(h("hr")); i++; continue; }
    if (/^\s*>/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.append(h("blockquote", null, md(quote.join("\n"))));
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
      const head = cells(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
      out.append(h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, head.map((c) => h("th", null, mdInline(c))))),
        h("tbody", null, rows.map((r) => h("tr", null, r.map((c) => h("td", null, mdInline(c)))))),
      )));
      continue;
    }
    if (MD_LIST_ITEM.test(line)) {
      const [list, next] = mdList(lines, i);
      out.append(list);
      i = next;
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && (!para.length || !MD_BLOCK_START.test(lines[i]))) para.push(lines[i++]);
    const p = h("p");
    para.forEach((l, k) => { if (k) p.append(h("br")); p.append(mdInline(l.trim())); });
    out.append(p);
  }
  return out;
}

function mdBlock(text, extraClass = "") {
  return h("div", { class: `md ${extraClass}` }, md(text));
}

// ---- API ----
async function api(path, { method = "GET", body, form, raw } = {}) {
  const headers = {};
  if (state.token) headers.authorization = `Bearer ${state.token}`;
  if (body) headers["content-type"] = "application/json";
  const response = await fetch(path, {
    method, headers, body: body ? JSON.stringify(body) : form,
  });
  if (response.status === 401 && state.token && path !== "/v1/auth/change-password"
      && path !== "/v1/auth/recovery-code") {
    signOut("Your session ended. Sign in again.");
    throw new Error("Session ended");
  }
  if (!response.ok) {
    let detail = `The server returned an error (${response.status}). Try again.`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") detail = data.detail;
      else if (Array.isArray(data.detail)) detail = data.detail.map((d) => d.msg).join("; ");
    } catch { /* keep the generic message */ }
    throw new Error(detail);
  }
  if (raw) return response.blob();
  return response.status === 204 ? null : response.json();
}

// Calls onDelta with each piece of the reply as it arrives. Resolves with the finished reply.
async function streamChat(body, onDelta, onStep = () => {}) {
  const response = await fetch("/v1/chat/stream", {
    method: "POST",
    headers: { authorization: `Bearer ${state.token}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.status === 401) {
    signOut("Your session ended. Sign in again.");
    throw new Error("Session ended");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === "string" ? data.detail : `The server returned an error (${response.status}). Try again.`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const line = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      if (!line.startsWith("data: ")) continue;
      const event = JSON.parse(line.slice(6));
      if (event.type === "delta") onDelta(event.text);
      else if (event.type === "step") onStep(event.text);
      else if (event.type === "error") throw new Error(event.detail);
      else if (event.type === "done") finished = event;
    }
  }
  if (!finished) throw new Error("The reply was cut off. Try again.");
  return finished;
}

function useToken(result, username) {
  state.token = result.access_token;
  const payload = result.access_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  // For choosing which pages to show until /v1/dashboard answers. The server checks every action itself.
  state.roles = JSON.parse(atob(payload)).roles || [];
  state.username = username;
}

function signOut(message) {
  clearInterval(state.poll);
  clearInterval(state.bellPoll);
  Object.assign(state, {
    token: null, username: "", roles: [], view: "chat", thread: null, threads: [], counts: {}, poll: null,
    bellPoll: null, unread: 0,
  });
  loginScreen(message);
}

const has = (...roles) => roles.some((role) => state.roles.includes(role));

// ---- sign-in screens ----
function loginScreen(message) {
  const error = errorBox();
  const username = h("input", { autocomplete: "username", required: true });
  const password = h("input", { type: "password", autocomplete: "current-password", required: true });
  const submit = h("button", { class: "primary", type: "submit" }, "Sign in");
  show(h("div", { class: "center" }, h("form", {
    class: "card auth",
    onsubmit: async (event) => {
      event.preventDefault();
      error.clear();
      submit.disabled = true;
      try {
        const result = await api("/v1/auth/login", {
          method: "POST", body: { username: username.value, password: password.value },
        });
        useToken(result, username.value);
        if (result.must_change_password) firstPasswordScreen(password.value);
        else shell();
      } catch (err) {
        error.fail(err);
        submit.disabled = false;
      }
    },
  },
    h("div", { class: "auth-brand" }, logo("lg"), h("span", { class: "auth-name" }, "AI Platform")),
    h("h1", null, "Welcome back"),
    h("p", { class: "muted auth-sub" }, "Sign in to your company's private AI workspace."),
    message && h("div", { class: "notice info" }, message),
    error,
    h("label", null, "Username", username),
    h("label", null, "Password", password),
    h("div", { class: "row spread auth-actions" },
      submit,
      h("button", { class: "link", type: "button", onclick: recoverScreen }, "Forgot password?"),
    ),
    h("p", { class: "auth-foot" }, icon("lock"), "Private chats are encrypted with a key only you can unlock."),
  )));
  username.focus();
}

function firstPasswordScreen(oldPassword) {
  const error = errorBox();
  const next = h("input", { type: "password", autocomplete: "new-password", minLength: 12, required: true });
  const again = h("input", { type: "password", autocomplete: "new-password", required: true });
  show(h("div", { class: "center" }, h("form", {
    class: "card auth",
    onsubmit: async (event) => {
      event.preventDefault();
      error.clear();
      if (next.value !== again.value) return error.fail(new Error("The passwords don't match."));
      try {
        const result = await api("/v1/auth/change-password", {
          method: "POST", body: { old_password: oldPassword, new_password: next.value },
        });
        useToken(result, state.username);
        recoveryCodeScreen(result.recovery_code);
      } catch (err) {
        error.fail(err);
      }
    },
  },
    h("h1", null, "Set your own password"),
    h("p", { class: "muted" }, "Your administrator chose your first password. Set your own to start chatting. "
      + "Only your password can unlock your private chats, so your administrator can't read them."),
    error,
    h("label", null, "New password (at least 12 characters)", next),
    h("label", null, "New password again", again),
    h("button", { class: "primary", type: "submit" }, "Save password"),
  )));
  next.focus();
}

function recoveryCodeScreen(code) {
  const saved = h("input", { type: "checkbox" });
  const proceed = h("button", { class: "primary", disabled: true, onclick: shell }, "Continue");
  saved.addEventListener("change", () => { proceed.disabled = !saved.checked; });
  const copy = h("button", {
    type: "button",
    onclick: async () => {
      await navigator.clipboard.writeText(code);
      copy.textContent = "Copied";
    },
  }, "Copy code");
  show(h("div", { class: "center" }, h("div", { class: "card auth" },
    h("h1", null, "Save your recovery code"),
    h("p", null, "If you forget your password, this code is the only way back into your private chats. "
      + "It won't be shown again, and nobody can reset it for you."),
    h("div", { class: "code", id: "recovery-code" }, code),
    h("p", null, copy),
    h("div", { class: "notice warn" }, "Keep it somewhere private. Anyone with this code can read your private chats."),
    h("label", { class: "check" }, saved, h("span", null, "I have saved my recovery code")),
    proceed,
  )));
}

function recoverScreen() {
  const error = errorBox();
  const username = h("input", { autocomplete: "username", required: true });
  const code = h("input", { autocomplete: "off", required: true, placeholder: "ABCD-EFGH-..." });
  const next = h("input", { type: "password", autocomplete: "new-password", minLength: 12, required: true });
  show(h("div", { class: "center" }, h("form", {
    class: "card auth",
    onsubmit: async (event) => {
      event.preventDefault();
      error.clear();
      try {
        const result = await api("/v1/auth/recover", {
          method: "POST",
          body: { username: username.value, recovery_code: code.value, new_password: next.value },
        });
        useToken(result, username.value);
        recoveryCodeScreen(result.recovery_code);
      } catch (err) {
        error.fail(err);
      }
    },
  },
    h("h1", null, "Recover your account"),
    h("p", { class: "muted" }, "Your recovery code sets a new password and keeps your chats. "
      + "The code works once, and you'll get a new one."),
    error,
    h("label", null, "Username", username),
    h("label", null, "Recovery code", code),
    h("label", null, "New password (at least 12 characters)", next),
    h("div", { class: "row spread" },
      h("button", { class: "primary", type: "submit" }, "Recover account"),
      h("button", { class: "link", type: "button", onclick: () => loginScreen() }, "Back to sign in"),
    ),
  )));
  username.focus();
}

// ---- shell ----
function morePages() {
  return [
    has("admin") && ["approvals", "Approvals", approvalsView, "pending_approvals"],
    has("admin", "account") && ["inbox", "Accounts inbox", inboxView, "inbox_queued"],
    has("admin", "hr", "account") && ["reports", "Reports", reportsView, "open_reports"],
    has("admin") && ["users", "Users", usersView],
    has("admin") && ["credentials", "Credentials", credentialsView],
    ["notifications", "Notifications", notificationsView, "unread_notifications"],
    ["files", "My files", filesView],
    ["templates", "Templates", templatesView],
    ["reminders", "Reminders", remindersView],
    ["overview", "Overview", overviewView],
    ["account", "My account", accountView],
  ].filter(Boolean);
}

function newThreadId() {
  return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

async function refreshCounts() {
  try {
    state.counts = { ...(await api("/v1/dashboard")), unread_notifications: state.unread };
    state.roles = state.counts.roles;
  } catch { /* the page itself reports errors */ }
  return state.counts;
}

// The bell: checks for new notifications every minute, and pops up a browser notification for
// new ones while this tab is open (if the person allowed it under Notifications).
async function checkBell(paint) {
  try {
    const { unread } = await api("/v1/notifications/count");
    if (unread > state.unread && "Notification" in window && Notification.permission === "granted") {
      const fresh = await api("/v1/notifications?unread_only=true");
      fresh.slice(0, unread - state.unread).reverse().forEach((n) => {
        try { new Notification(n.title, { body: n.body.slice(0, 200), tag: n.id }); } catch { /* not supported here */ }
      });
    }
    state.unread = unread;
    state.counts.unread_notifications = unread;
    if (state.token) paint();
  } catch { /* try again next time */ }
}

function shell() {
  const view = h("div", { class: "view" });
  const chats = h("div", { class: "chats", "aria-label": "Your chats" });
  const more = h("div", { class: "side-more" });
  const side = h("nav", { class: "side", "aria-label": "Main" });
  const scrim = h("button", { class: "scrim", "aria-label": "Close menu", onclick: () => toggleSide(false) });
  const toggleSide = (open) => {
    side.classList.toggle("open", open);
    scrim.classList.toggle("open", open);
  };

  const paint = () => {
    fill(chats,
      !state.threads.length && h("div", { class: "chats-empty" }, "Your chats will appear here."),
      state.threads.map((t) => h("button", {
        class: `side-item ${state.view === "chat" && t.thread_id === state.thread ? "active" : ""}`,
        onclick: () => openChat(t.thread_id),
      }, h("span", { class: "title", title: t.title }, t.title || "Untitled chat"))),
    );
    fill(more, morePages().map(([id, label, , badge]) => {
      const count = badge && state.counts[badge];
      return h("button", {
        class: `side-item ${state.view === id ? "active" : ""}`,
        "aria-current": state.view === id ? "page" : null,
        onclick: () => go(id),
      }, h("span", { class: "title" }, label), count > 0 && h("span", { class: "badge" }, count));
    }));
  };

  const reloadThreads = async () => {
    try {
      state.threads = await api("/v1/chat/threads");
    } catch (err) {
      state.threads = [];
      if (!err.message.includes("change your password")) throw err;
    }
    paint();
  };

  const go = async (id) => {
    clearInterval(state.poll);
    state.view = id;
    toggleSide(false);
    paint();
    const [, , render] = morePages().find(([page]) => page === id);
    view.replaceChildren(h("div", { class: "page muted" }, "Loading..."));
    try {
      const content = await render(() => go(id));
      view.replaceChildren(h("div", { class: "page" }, content));
    } catch (err) {
      if (state.token) view.replaceChildren(h("div", { class: "page" }, h("div", { class: "notice error", role: "alert" }, err.message)));
    }
    refreshCounts().then(() => { if (state.token) paint(); });
  };

  const openChat = async (threadId) => {
    clearInterval(state.poll);
    state.view = "chat";
    state.thread = threadId;
    toggleSide(false);
    paint();
    try {
      view.replaceChildren(await chatView(threadId, { reloadThreads, paint }));
    } catch (err) {
      if (state.token) view.replaceChildren(h("div", { class: "page" }, h("div", { class: "notice error", role: "alert" }, err.message)));
    }
  };

  state.go = go;
  fill(side,
    h("div", { class: "brand" }, logo(), h("span", null, "AI Platform")),
    h("button", { class: "new-chat", onclick: () => openChat(newThreadId()) }, icon("pen"), "New chat"),
    h("div", { class: "side-label" }, "Recent chats"),
    chats,
    h("div", { class: "side-label" }, "Workspace"),
    more,
    h("div", { class: "who" },
      h("span", { class: "avatar", "aria-hidden": "true" }, initials(state.username)),
      h("span", { class: "who-text" },
        h("span", { class: "who-name" }, state.username),
        h("span", { class: "who-role" }, state.roles.map(departmentName).join(" · ") || "Member"),
      ),
      h("button", { class: "link", onclick: () => signOut() }, "Sign out"),
    ),
  );
  show(h("div", { class: "shell" },
    side,
    scrim,
    h("div", { class: "main" },
      h("div", { class: "topbar" },
        h("button", { class: "icon-button", "aria-label": "Open menu", onclick: () => toggleSide(true) }, icon("menu")),
        h("span", { class: "brand" }, logo(), h("span", null, "AI Platform")),
      ),
      view,
    ),
  ));
  reloadThreads().catch(() => {}).then(() => {
    openChat(newThreadId());
    refreshCounts().then(() => { if (state.token) paint(); });
  });
  clearInterval(state.bellPoll);
  checkBell(paint);
  state.bellPoll = setInterval(() => { if (state.token) checkBell(paint); }, 60000);
}

function heading(text, subtitle) {
  return h("div", { class: "head" }, h("h1", null, text), subtitle && h("p", { class: "muted" }, subtitle));
}

// ---- chat ----
async function chatView(thread, { reloadThreads, paint }) {
  const known = () => state.threads.some((t) => t.thread_id === thread);
  const messages = h("div", { class: "messages column", "aria-live": "polite" });
  const requests = h("div", { class: "reqs column" });
  const scroll = h("div", { class: "scroll" }, messages, requests);
  const files = h("div", { class: "files" });
  const error = errorBox();
  const input = h("textarea", { placeholder: "Message the assistant", "aria-label": "Message", rows: 1 });
  const send = h("button", { class: "primary round", type: "submit", "aria-label": "Send" }, icon("send"));
  const picker = h("input", { type: "file", class: "hidden-input", id: "file-picker", onchange: () => upload() });

  const toBottom = () => { scroll.scrollTop = scroll.scrollHeight; };
  const setEmpty = (empty) => content.classList.toggle("is-empty", empty);

  const bubble = (role, text, handledBy) => {
    const node = h("div", { class: `msg ${role} ${role === "assistant" ? "md" : ""}` }, role === "assistant" ? md(text) : text,
      role === "assistant" && handledBy && h("span", { class: "handled" }, `Handled as work by ${departmentName(handledBy)}`));
    messages.append(node);
    toBottom();
    return node;
  };

  const paintFiles = async () => {
    const items = await api(`/v1/chat/attachments?thread_id=${encodeURIComponent(thread)}`);
    files.replaceChildren(...items.map((item) => h("div", { class: "file" },
      h("span", { title: item.name }, `${item.name} (${formatSize(item.size)})`),
      h("button", {
        type: "button",
        onclick: async () => {
          const blob = await api(`/v1/chat/attachments/${item.id}`, { raw: true });
          const link = h("a", { href: URL.createObjectURL(blob), download: item.name });
          link.click();
          URL.revokeObjectURL(link.href);
        },
      }, "Download"),
      h("button", {
        type: "button",
        class: "danger",
        "aria-label": `Delete ${item.name}`,
        onclick: async () => {
          await api(`/v1/chat/attachments/${item.id}`, { method: "DELETE" });
          paintFiles();
        },
      }, "Delete"),
    )));
  };

  const paintRequests = async () => {
    const items = await api(`/v1/chat/threads/${encodeURIComponent(thread)}/requests`);
    fill(requests, items.map((item) => {
      const [text, kind] = REQUEST_STATES[item.state] || [item.state, ""];
      return h("div", { class: "req" },
        h("div", { class: "row spread" },
          h("span", null, `${REQUEST_LABELS[item.label] || item.label} (${departmentName(item.department)})`),
          h("span", { class: `pill ${kind}` }, text),
        ),
        item.result && mdBlock(item.result),
      );
    }));
    return items.some((item) => !FINISHED.has(item.state));
  };

  // The assistant asks before sending mail or deleting a file in a private chat.
  const confirmCard = (pending) => {
    const error = errorBox();
    const card = h("div", { class: "confirm card" });
    const decide = (approve, asWork = false) => async () => {
      error.clear();
      card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try {
        const result = await api(`/v1/chat/threads/${encodeURIComponent(thread)}/confirm`, { method: "POST", body: { approve, as_work: asWork } });
        card.remove();
        showResult(result);
      } catch (err) {
        error.fail(err);
        card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      }
    };
    const LABELS = { to: "To", subject: "Subject", body: "Message", path: "File", category: "Mailbox", in_reply_to: "In reply to",
      summary: "What happens", label: "Button" };
    delete pending.details.ref;
    fill(card,
      h("strong", null, pending.title),
      h("dl", { class: "facts" }, Object.entries(pending.details).flatMap(([k, v]) => [
        h("dt", null, LABELS[k] || k), h("dd", { class: k === "body" ? "pre" : null }, v),
      ])),
      error,
      pending.purchase && h("p", { class: "small muted" }, "Is this for personal use or for work? Work orders go to the Accounts "
        + "team, who pay with the company's details."),
      pending.purchase
        ? h("div", { class: "row" },
          h("button", { class: "primary", type: "button", onclick: decide(true) }, "Personal: place it"),
          h("button", { type: "button", onclick: decide(false, true) }, "Work: send to Accounts"),
          h("button", { type: "button", onclick: decide(false) }, "Cancel"))
        : h("div", { class: "row" },
          h("button", { class: "primary", type: "button", onclick: decide(true) },
            pending.tool === "send_mail" ? "Send" : pending.tool === "browser_commit" ? "Yes, go ahead" : "Yes, go ahead"),
          h("button", { type: "button", onclick: decide(false) }, "No")),
    );
    messages.append(card);
    toBottom();
  };

  const say = (text) => { input.value = text; grow(); composer.requestSubmit(); };
  const secretBox = (scope, ask) => {
    const value = h("input", { type: "password", autocomplete: "one-time-code", "aria-label": ask.what });
    const reveal = h("input", { type: "checkbox", onchange: () => { value.type = reveal.checked ? "text" : "password"; } });
    const err = errorBox();
    const box = h("form", {
      class: "card confirm",
      onsubmit: async (event) => {
        event.preventDefault();
        err.clear();
        try {
          await api(`/v1/browser/${encodeURIComponent(scope)}/secret`, { method: "POST", body: { ref: ask.ref, value: value.value, site: ask.site } });
          box.replaceChildren(h("div", { class: "notice good" }, `Entered on ${ask.site}.`));
          say("I've entered it, please carry on.");
        } catch (e) { err.fail(e); }
      },
    },
      h("strong", null, icon("lock"), ` ${ask.what}`),
      h("p", { class: "small muted" }, `Typed straight into the page on ${ask.site}. It isn't saved, shown to the assistant, or kept in this chat.`),
      err,
      h("div", { class: "row" }, value, h("button", { class: "primary", type: "submit" }, "Enter")),
      h("label", { class: "check small" }, reveal, h("span", null, "Show")),
    );
    messages.append(box);
    toBottom();
    value.focus();
  };
  const openBrowser = (scope, reason) => {
    content.querySelectorAll(".live").forEach((el) => el.remove());
    messages.append(browserPanel(scope, reason, { onDone: () => say("Done, please carry on.") }));
    setEmpty(false);
    toBottom();
  };

  const handover = (b) => {
    if (!b) return;
    if (b.secret) secretBox(b.scope, b.secret);
    else openBrowser(b.scope, b.reason);
  };

  const showResult = (result) => {
    if (result.outcome === "confirm") confirmCard(result.confirm);
    else if (result.reply) bubble("assistant", result.reply);
    handover(result.browser);
  };

  const watchRequests = async () => {
    clearInterval(state.poll);
    if (await paintRequests()) {
      state.poll = setInterval(async () => {
        if (!state.token || state.thread !== thread || !(await paintRequests())) clearInterval(state.poll);
      }, 10000);
    }
  };

  const upload = async () => {
    const file = picker.files[0];
    if (!file) return;
    error.clear();
    const form = new FormData();
    form.append("thread_id", thread);
    form.append("file", file);
    try {
      const result = await api("/v1/chat/attachments", { method: "POST", form });
      if (result.outcome === "flagged_nsfw") {
        setEmpty(false);
        bubble("flagged", `"${file.name}" was blocked and reported to HR and the admin.`);
      }
      await paintFiles();
    } catch (err) {
      error.fail(err);
    }
    picker.value = "";
  };

  const grow = () => {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  };

  const composer = h("form", {
    class: "composer",
    onsubmit: async (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text || send.disabled) return;
      error.clear();
      input.value = "";
      grow();
      send.disabled = true;
      setEmpty(false);
      const mine = bubble("user", text);
      const thinking = h("div", { class: "msg assistant thinking" }, "Thinking...");
      messages.append(thinking);
      toBottom();
      // What the agents are doing, as it happens: earlier steps get a tick, the current one pulses.
      const steps = h("ol", { class: "steps", "aria-live": "polite" });
      const onStep = (textOfStep) => {
        if (!steps.isConnected) {
          thinking.classList.remove("thinking");
          thinking.replaceChildren(steps);
        }
        steps.querySelectorAll("li.now").forEach((li) => li.classList.replace("now", "done"));
        steps.append(h("li", { class: "now" }, textOfStep));
        toBottom();
      };
      try {
        let started = false;
        let streamed = "";
        let drawing = false;
        const result = await streamChat({ message: text, thread_id: thread }, (piece) => {
          if (!started) {
            started = true;
            thinking.classList.remove("thinking");
            thinking.classList.add("md");
          }
          streamed += piece;
          if (!drawing) {  // at most one redraw per frame
            drawing = true;
            requestAnimationFrame(() => { drawing = false; thinking.replaceChildren(md(streamed)); toBottom(); });
          }
        }, onStep);
        thinking.remove();
        if (result.outcome === "flagged_nsfw") {
          mine.remove();
          bubble("flagged", "This message was blocked and reported to HR and the admin. It wasn't saved in this chat.");
        } else if (result.outcome === "confirm") {
          confirmCard(result.confirm);
          await reloadThreads();
        } else {
          bubble("assistant", result.reply, result.route === "work" ? result.department : null);
          handover(result.browser);
          await reloadThreads();
          if (result.route === "work") await watchRequests();
          toBottom();
        }
      } catch (err) {
        thinking.remove();
        mine.remove();
        input.value = text;
        grow();
        error.fail(err);
        if (!messages.children.length) setEmpty(true);
      }
      send.disabled = false;
      input.focus();
    },
  },
    files,
    input,
    h("div", { class: "composer-actions" },
      h("button", { type: "button", class: "round", "aria-label": "Attach a file", title: "Attach a file", onclick: () => picker.click() }, icon("plus")),
      h("button", { type: "button", class: "round", "aria-label": "Open the live browser", title: "Live browser (sign in to shops, see what the assistant sees)", onclick: () => openBrowser("private", "") }, icon("globe")),
      picker,
      send,
    ),
  );
  input.addEventListener("input", grow);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });

  const ideaTexts = [
    ...state.roles.filter((role) => IDEAS[role]).map((role) => IDEAS[role]),
    "Help me write a polite email",
  ].slice(0, 4);

  const content = h("div", { class: "chat is-empty" },
    h("h1", { class: "sr-only" }, "Chat"),
    h("div", { class: "greeting column" },
      logo("xl"),
      h("h1", null, "What can I help with, ", h("span", { class: "accent-text" }, state.username), "?"),
      h("p", { class: "muted greeting-sub" }, "Ask anything. Personal questions stay private; work goes to the right team."),
    ),
    scroll,
    h("div", { class: "dock column" },
      error,
      composer,
      h("div", { class: "ideas" }, ideaTexts.map((text) => h("button", {
        type: "button",
        onclick: () => {
          input.value = text;
          grow();
          input.focus();
        },
      }, icon("spark"), h("span", null, text)))),
      h("p", { class: "fineprint" }, "Private chats are encrypted with your key. Work is visible to the department "
        + "that handles it. Explicit or illegal content is reported."),
    ),
  );

  if (known()) {
    const history = await api(`/v1/chat/threads/${encodeURIComponent(thread)}/messages`);
    history.forEach((m) => bubble(m.role, m.content, m.handled_by));
    if (history.length) setEmpty(false);
    const pending = await api(`/v1/chat/threads/${encodeURIComponent(thread)}/pending`).catch(() => null);
    if (pending) confirmCard(pending);
    await watchRequests();
  }
  await paintFiles();
  setTimeout(() => { input.focus(); toBottom(); }, 0);
  return content;
}


// ---- the live browser: the person takes over for sign-in, OTPs and payment ----
// The page is drawn on a canvas from screenshots (the CSP allows no other image sources). What
// the person types goes straight to the page; the agent never sees it.
function browserPanel(scope, reason, { onDone } = {}) {
  const VIEW = { w: 1280, h: 900 };
  const canvas = h("canvas", { width: VIEW.w, height: VIEW.h, class: "live-screen", tabIndex: 0, "aria-label": "Live browser view: click to interact" });
  const ctx = canvas.getContext("2d");
  const url = h("input", { placeholder: "Open a site, e.g. amazon.in", "aria-label": "Address" });
  const typed = h("input", { type: "password", autocomplete: "off", "aria-label": "Text to type into the page", placeholder: "Type here, then Send (goes to the selected field)" });
  const reveal = h("input", { type: "checkbox", onchange: () => { typed.type = reveal.checked ? "text" : "password"; } });
  const status = h("span", { class: "small muted live-url" }, "Loading...");
  const error = errorBox();
  let timer = null;
  let busy = false;
  const base = `/v1/browser/${encodeURIComponent(scope)}`;

  const draw = async () => {
    if (busy || !panel.isConnected) return;
    busy = true;
    try {
      const blob = await api(`${base}/screen`, { raw: true });
      const bitmap = await createImageBitmap(blob);
      ctx.drawImage(bitmap, 0, 0, VIEW.w, VIEW.h);
      const info = await api(base);
      status.textContent = info.open ? `${info.title || ""} — ${info.url}` : "No page open";
    } catch (err) {
      status.textContent = /no browser/i.test(err.message) ? "No page open yet: enter an address above." : err.message;
    }
    busy = false;
  };
  const act = async (body) => {
    error.clear();
    try {
      await api(`${base}/act`, { method: "POST", body });
    } catch (err) {
      error.fail(err);
    }
    setTimeout(draw, 300);
  };
  canvas.addEventListener("click", (event) => {
    const box = canvas.getBoundingClientRect();
    act({ action: "click", x: Math.round(((event.clientX - box.left) / box.width) * VIEW.w), y: Math.round(((event.clientY - box.top) / box.height) * VIEW.h) });
  });
  canvas.addEventListener("wheel", (event) => { event.preventDefault(); act({ action: "scroll", y: Math.round(event.deltaY) }); }, { passive: false });
  const sendText = () => { if (typed.value) { act({ action: "type", text: typed.value }); typed.value = ""; } };
  typed.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); sendText(); } });
  url.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); act({ action: "open", url: url.value }); } });
  const close = async (forget) => {
    clearInterval(timer);
    try { await api(`${base}/close`, { method: "POST", body: { forget } }); } catch { /* already closed */ }
    panel.remove();
  };

  const panel = h("div", { class: "card live" },
    h("div", { class: "row spread" },
      h("strong", null, icon("globe"), " Live browser", scope !== "private" ? ` (${departmentName(scope)})` : ""),
      h("div", { class: "row" },
        h("button", { type: "button", onclick: () => act({ action: "back" }) }, "Back"),
        h("button", { type: "button", onclick: () => close(false) }, "Close"),
        h("button", { type: "button", class: "danger", title: "Close and forget saved sign-ins", onclick: () => close(true) }, "Sign out & close"),
      ),
    ),
    reason && h("div", { class: "notice" }, reason),
    h("div", { class: "row" }, url, h("button", { type: "button", onclick: () => act({ action: "open", url: url.value }) }, "Go")),
    status,
    canvas,
    error,
    h("div", { class: "row wrap" },
      h("label", { class: "grow" }, "Type into the page", typed),
      h("button", { type: "button", class: "primary", onclick: sendText }, "Send"),
      ["Enter", "Tab", "Backspace"].map((k) => h("button", { type: "button", onclick: () => act({ action: "key", text: k }) }, k)),
    ),
    h("label", { class: "check small" }, reveal, h("span", null, "Show what I type")),
    h("p", { class: "small muted" }, "Click the page to select a field, then type. Passwords, OTPs and card details you type go only to "
      + "the site: the assistant never sees them. Your sign-ins are kept"
      + (scope === "private" ? ", encrypted with your key," : "") + " so you don't have to sign in every time."),
    onDone && h("button", { type: "button", class: "primary", onclick: () => onDone() }, "Done, carry on"),
  );
  timer = setInterval(() => { if (!panel.isConnected) clearInterval(timer); else if (!document.hidden) draw(); }, 1500);
  setTimeout(draw, 50);
  return panel;
}

// ---- overview ----
function tile(label, value, hint, target) {
  const body = [h("div", { class: "label" }, label), h("div", { class: "value" }, value), hint && h("div", { class: "hint" }, hint)];
  return target
    ? h("button", { class: "tile", onclick: () => state.go(target) }, body)
    : h("div", { class: "tile" }, body);
}

function bindTooltip(el, text) {
  const tip = document.getElementById("tooltip");
  const place = (event) => {
    tip.textContent = text;
    tip.hidden = false;
    const box = el.getBoundingClientRect();
    tip.style.left = `${Math.min((event.clientX || box.left) + 12, window.innerWidth - 270)}px`;
    tip.style.top = `${(event.clientY || box.top) + 14}px`;
  };
  const hide = () => { tip.hidden = true; };
  el.addEventListener("pointermove", place);
  el.addEventListener("pointerleave", hide);
  el.addEventListener("focus", place);
  el.addEventListener("blur", hide);
}

function activityChart(activity) {
  const totals = new Map();
  for (const { department, count } of activity) totals.set(department, (totals.get(department) || 0) + count);
  const rows = [...totals].sort((a, b) => b[1] - a[1]);
  const max = Math.max(...rows.map(([, count]) => count));
  return h("div", { class: "bars" }, rows.map(([department, count]) => {
    const parts = activity
      .filter((a) => a.department === department)
      .map((a) => `${a.count} ${(OUTCOMES[a.outcome] || [a.outcome])[0].toLowerCase()}`)
      .join(", ");
    const bar = h("div", { class: "bar" });
    bar.style.width = `calc((100% - 44px) * ${count / max})`;
    const row = h("div", { class: "bar-row", tabIndex: 0, "aria-label": `${department}: ${count} requests, ${parts}` },
      h("div", { class: "small" }, department === "none" ? "unrouted" : department),
      h("div", { class: "bar-track" }, bar, h("span", { class: "small" }, count)),
    );
    bindTooltip(row, `${department === "none" ? "unrouted" : department}: ${parts}`);
    return row;
  }));
}

async function overviewView() {
  const data = await refreshCounts();
  const total = data.activity.reduce((sum, a) => sum + a.count, 0);
  const tiles = [
    tile(has("admin") ? "Work requests, last 24 hours" : "Your work requests, last 24 hours", total),
    data.pending_approvals !== undefined && tile("Waiting for approval", data.pending_approvals, null, "approvals"),
    data.inbox_queued !== undefined && tile("Accounts inbox", data.inbox_queued, "queued", "inbox"),
    data.open_reports !== undefined && tile("Open reports", data.open_reports, null, "reports"),
    data.users !== undefined && tile("Users", data.users, null, "users"),
  ];
  const outcomeRows = data.activity
    .slice()
    .sort((a, b) => a.department.localeCompare(b.department) || b.count - a.count)
    .map((a) => [a.department === "none" ? "unrouted" : a.department, outcomePill(a.outcome), a.count]);

  return h("div", null,
    heading("Overview", "Work handled by departments. Private chats are never counted here."),
    h("div", { class: "tiles" }, tiles),
    h("div", { class: "card" },
      h("h2", null, "Work requests by department, last 24 hours"),
      total ? activityChart(data.activity) : h("div", { class: "empty" }, "No work requests in the last 24 hours."),
    ),
    total > 0 && h("div", { class: "card" },
      h("h2", null, "Outcomes"),
      table(["Department", "Outcome", "Requests"], outcomeRows),
    ),
    data.departments && h("div", { class: "card" },
      h("h2", null, "Departments"),
      table(
        ["Department", "Requests per minute", "Tools"],
        data.departments.map((d) => [d.name, d.quota_per_min, d.tools.includes("*") ? "All tools" : d.tools.join(", ")]),
      ),
    ),
  );
}

// ---- approvals ----
function resultNotice(result) {
  const text = result.outcome === "pending_approval"
    ? "The agent went on to another action that needs its own approval."
    : result.reply || result.reason || "Done.";
  return h("div", { class: `notice ${result.outcome === "error" ? "error" : "good"}` }, mdBlock(text));
}

async function approvalsView(refresh) {
  const rows = await api("/v1/approvals");
  const error = errorBox();
  const decide = (id, verb) => async () => {
    error.clear();
    try {
      const result = await api(`/v1/approvals/${id}/${verb}`, { method: "POST" });
      const content = await approvalsView(refresh);
      content.prepend(resultNotice(result));
      document.querySelector(".page").replaceChildren(content);
      refreshCounts();
    } catch (err) {
      error.fail(err);
    }
  };
  return h("div", null,
    heading("Approvals", "These actions are paused until you decide. Accounts is notified of each decision on a finance action."),
    error,
    h("div", { class: "card" }, table(
      ["Action", "Details", "Requested by", "Original message", "Decision"],
      rows.map((a) => [
        h("strong", null, a.tool),
        h("div", { class: "mono" }, JSON.stringify(a.tool_args)),
        a.subject_name,
        h("div", { class: "pre" }, a.message),
        h("div", { class: "row" },
          h("button", { class: "primary", onclick: decide(a.id, "approve") }, "Approve"),
          h("button", { class: "danger", onclick: decide(a.id, "deny") }, "Deny"),
        ),
      ]),
      "Nothing is waiting for approval.",
    )),
  );
}

// ---- inbox ----
async function inboxView(refresh) {
  const rows = await api("/v1/inbox?department=account");
  const error = errorBox();
  const processTask = (id) => async (event) => {
    event.target.disabled = true;
    error.clear();
    try {
      const result = await api(`/v1/inbox/${id}/process`, { method: "POST" });
      const content = await inboxView(refresh);
      content.prepend(resultNotice(result));
      document.querySelector(".page").replaceChildren(content);
      refreshCounts();
    } catch (err) {
      error.fail(err);
      event.target.disabled = false;
    }
  };
  return h("div", null,
    heading("Accounts inbox", "Orders, bookings and bills sent to accounts from chats."),
    error,
    h("div", { class: "card" }, table(
      ["Type", "Requested by", "Message", ""],
      rows.map((t) => [
        h("span", { class: "pill" }, t.kind),
        t.requester_name,
        h("div", { class: "pre" }, t.message),
        h("button", { class: "primary", onclick: processTask(t.id) }, "Process"),
      ]),
      "The inbox is empty.",
    )),
  );
}

// ---- reports ----
async function reportsView() {
  const rows = await api("/v1/reports");
  const KINDS = { nsfw: ["Explicit or illegal", "bad"], finance_notice: ["Admin finance activity", "warn"] };
  return h("div", null,
    heading("Reports", "Only what your role is notified about."),
    h("div", { class: "card" }, table(
      ["Type", "User", "Content"],
      rows.map((r) => {
        const [label, kind] = KINDS[r.kind] || [r.kind, ""];
        return [h("span", { class: `pill ${kind}` }, label), r.subject_name, h("div", { class: "pre" }, r.message)];
      }),
      "There are no open reports.",
    )),
  );
}

// ---- users ----
function departmentBoxes(selected, onChange) {
  const boxes = DEPARTMENTS.map((name) => {
    const box = h("input", { type: "checkbox", value: name, checked: selected.includes(name), onchange: onChange });
    return [box, h("label", { class: "check" }, box, h("span", null, departmentName(name)))];
  });
  return {
    node: h("div", { class: "dept-boxes" }, boxes.map(([, label]) => label)),
    value: () => boxes.filter(([box]) => box.checked).map(([box]) => box.value),
  };
}

// An inline "are you sure" step: the viewer has no dialogs, and a second click is clearer anyway.
function confirmPanel(text, actionLabel, onConfirm, { danger = false } = {}) {
  const error = errorBox();
  const go = h("button", { class: danger ? "danger" : "primary", type: "button" }, actionLabel);
  const panel = h("div", { class: "panel", hidden: true },
    h("p", null, text), error,
    h("div", { class: "row" }, go, h("button", { type: "button", onclick: () => { panel.hidden = true; } }, "Cancel")),
  );
  go.addEventListener("click", async () => {
    error.clear();
    go.disabled = true;
    try {
      await onConfirm();
    } catch (err) {
      error.fail(err);
      go.disabled = false;
    }
  });
  return panel;
}

function userCard(user, refresh) {
  const isMe = user.username === state.username;
  const isAdmin = user.roles.includes("admin");
  const error = errorBox();
  const save = h("button", { disabled: true }, "Save departments");
  const boxes = departmentBoxes(user.roles, () => { save.disabled = false; save.textContent = "Save departments"; });
  save.addEventListener("click", async () => {
    error.clear();
    save.disabled = true;
    try {
      await api(`/v1/auth/users/${user.id}/departments`, { method: "PUT", body: { departments: boxes.value() } });
      save.textContent = "Saved";
    } catch (err) {
      error.fail(err);
      save.disabled = false;
    }
  });

  const temporary = h("input", { type: "password", minLength: 12, autocomplete: "new-password", "aria-label": "Temporary password" });
  const resetError = errorBox();
  const reset = h("form", {
    class: "panel",
    hidden: true,
    onsubmit: async (event) => {
      event.preventDefault();
      resetError.clear();
      try {
        await api(`/v1/auth/users/${user.id}/reset-password`, { method: "POST", body: { password: temporary.value } });
        refresh();
      } catch (err) {
        resetError.fail(err);
      }
    },
  },
    h("p", null, `Only for someone who lost both their password and their recovery code. If ${user.username} still has `
      + "their recovery code, they should use \"Forgot password?\" on the sign-in page instead: that keeps their chats."),
    h("p", { class: "small muted" }, "With a temporary password they choose a new one at sign-in. Their earlier "
      + "private chats can't be opened again, by anyone. You still can't read their chats."),
    resetError,
    h("label", null, "Temporary password (at least 12 characters)", temporary),
    h("div", { class: "row" },
      h("button", { class: "primary", type: "submit" }, "Set temporary password"),
      h("button", { type: "button", onclick: () => { reset.hidden = true; } }, "Cancel"),
    ),
  );

  const toggle = confirmPanel(
    user.disabled
      ? `Let ${user.username} sign in again?`
      : `Disable ${user.username}? They're signed out at once and can't sign in. Nothing is deleted, and you can enable them again.`,
    user.disabled ? "Enable" : "Disable",
    async () => {
      await api(`/v1/auth/users/${user.id}/disabled`, { method: "PUT", body: { disabled: !user.disabled } });
      refresh();
    },
    { danger: !user.disabled },
  );

  const eraseName = h("input", { autocomplete: "off", "aria-label": "Type the username to confirm" });
  const eraseAccount = h("input", { type: "checkbox" });
  const eraseError = errorBox();
  const erase = h("form", {
    class: "panel",
    hidden: true,
    onsubmit: async (event) => {
      event.preventDefault();
      eraseError.clear();
      try {
        await api(`/v1/auth/users/${user.id}/erase`, {
          method: "POST", body: { confirm_username: eraseName.value, delete_account: eraseAccount.checked },
        });
        refresh();
      } catch (err) {
        eraseError.fail(err);
      }
    },
  },
    h("p", null, `Permanently delete everything stored for ${user.username}: private chats, attachments, files, `
      + "reminders, notifications, mailbox connection, and the work requests they made. This can't be undone."),
    h("p", { class: "small muted" }, "Kept: reports of explicit or illegal content, and the access log (which holds no message text). "
      + "They will need to set a new password to use private chat again."),
    eraseError,
    h("label", { class: "check" }, eraseAccount, h("span", null, "Also delete the account")),
    h("label", null, `Type ${user.username} to confirm`, eraseName),
    h("div", { class: "row" },
      h("button", { class: "danger", type: "submit" }, "Erase data"),
      h("button", { type: "button", onclick: () => { erase.hidden = true; } }, "Cancel"),
    ),
  );

  const pills = [
    isAdmin && h("span", { class: "pill" }, "Administrator"),
    user.disabled
      ? h("span", { class: "pill bad" }, "Disabled")
      : h("span", { class: "pill good" }, "Active"),
    h("span", { class: `pill ${user.chat_enabled ? "" : "warn"}` }, user.chat_enabled ? "Own password set" : "Waiting for first sign-in"),
  ];

  return h("div", { class: `card user-card ${user.disabled ? "is-disabled" : ""}` },
    h("div", { class: "user-head" },
      h("span", { class: "avatar", "aria-hidden": "true" }, initials(user.username)),
      h("div", { class: "grow" },
        h("div", { class: "user-name" }, user.username, isMe && h("span", { class: "muted small" }, " (you)")),
        h("div", { class: "row pills" }, pills),
      ),
    ),
    error,
    h("div", { class: "user-section" },
      h("div", { class: "small muted" }, "Departments"),
      boxes.node,
    ),
    h("div", { class: "row user-actions" },
      save,
      !isMe && h("button", { type: "button", onclick: () => { reset.hidden = false; toggle.hidden = true; temporary.focus(); } }, "Reset password"),
      !isMe && h("button", {
        type: "button",
        class: user.disabled ? "" : "danger",
        onclick: () => { toggle.hidden = false; reset.hidden = true; },
      }, user.disabled ? "Enable" : "Disable"),
      !isMe && h("button", {
        type: "button", class: "danger", onclick: () => { erase.hidden = false; reset.hidden = true; toggle.hidden = true; eraseName.focus(); },
      }, "Erase data"),
      isMe && h("span", { class: "small muted" }, "Change your own password under My account."),
    ),
    reset,
    toggle,
    erase,
  );
}

async function usersView(refresh) {
  const users = await api("/v1/auth/users");
  const createError = errorBox();
  const username = h("input", { required: true, autocomplete: "off" });
  const password = h("input", { type: "password", required: true, minLength: 12, autocomplete: "new-password" });
  const boxes = departmentBoxes([]);
  const list = h("div", { class: "user-list" });
  const search = h("input", { type: "search", placeholder: "Search people", "aria-label": "Search people" });
  const paint = () => {
    const q = search.value.trim().toLowerCase();
    const shown = users.filter((u) => !q || u.username.toLowerCase().includes(q)
      || u.roles.some((r) => departmentName(r).toLowerCase().includes(q)));
    fill(list, shown.length ? shown.map((u) => userCard(u, refresh)) : h("div", { class: "card empty" }, "Nobody matches."));
  };
  search.addEventListener("input", paint);
  paint();

  const active = users.filter((u) => !u.disabled).length;
  return h("div", null,
    heading("Users", "Add people, choose their departments, and manage their sign-in. Changes apply right away."),
    h("div", { class: "tiles" },
      tile("People", users.length),
      tile("Active", active),
      tile("Disabled", users.length - active),
      tile("Waiting for first sign-in", users.filter((u) => !u.chat_enabled).length),
    ),
    h("div", { class: "admin-grid" },
      h("div", null,
        h("div", { class: "toolbar" }, search),
        list,
      ),
      h("form", {
        class: "card add-user",
        onsubmit: async (event) => {
          event.preventDefault();
          createError.clear();
          try {
            await api("/v1/auth/users", {
              method: "POST",
              body: { username: username.value, password: password.value, roles: boxes.value() },
            });
            refresh();
          } catch (err) {
            createError.fail(err);
          }
        },
      },
        h("h2", null, "Add a person"),
        h("p", { class: "muted small" }, "You choose a first password. They replace it at first sign-in, which creates "
          + "the key for their private chats. You can't read those chats."),
        createError,
        h("label", null, "Username", username),
        h("label", null, "First password (at least 12 characters)", password),
        h("div", { class: "small muted" }, "Departments"),
        h("div", { class: "user-section" }, boxes.node),
        h("button", { class: "primary", type: "submit" }, "Add person"),
      ),
    ),
  );
}

// ---- credentials (vault secrets) ----
async function credentialsView(refresh) {
  const names = await api("/v1/admin/secrets");
  const error = errorBox();
  const name = h("input", {
    required: true, autocomplete: "off", pattern: "[A-Za-z0-9_./-]{1,128}", placeholder: "payment_gateway_key",
    title: "Letters, numbers, and _ . / - only",
  });
  const value = h("input", { type: "password", required: true, autocomplete: "new-password" });
  const submit = h("button", { class: "primary", type: "submit" }, "Save credential");

  const rows = names.map((secret) => {
    const remove = confirmPanel(
      `Delete "${secret}"? Tools that use it stop working until you add it again.`,
      "Delete",
      async () => {
        await api(`/v1/admin/secrets/${encodeURIComponent(secret)}`, { method: "DELETE" });
        refresh();
      },
      { danger: true },
    );
    return h("div", { class: "secret-row" },
      h("div", { class: "row spread" },
        h("div", { class: "row" }, icon("lock"), h("span", { class: "mono" }, secret)),
        h("div", { class: "row" },
          h("button", {
            type: "button",
            onclick: () => { name.value = secret; value.value = ""; value.focus(); submit.textContent = "Replace credential"; },
          }, "Replace"),
          h("button", { type: "button", class: "danger", onclick: () => { remove.hidden = false; } }, "Delete"),
        ),
      ),
      remove,
    );
  });

  return h("div", null,
    heading("Credentials", "API keys and passwords the platform's tools use, such as a payment gateway or email provider."),
    h("div", { class: "notice info" }, "Values are encrypted in the vault and are never shown again, not even here. "
      + "To change one, replace it."),
    h("div", { class: "admin-grid" },
      h("div", { class: "card" },
        h("h2", null, `Stored (${names.length})`),
        rows.length ? h("div", { class: "secret-list" }, rows) : h("div", { class: "empty" }, "No credentials yet."),
      ),
      h("form", {
        class: "card",
        onsubmit: async (event) => {
          event.preventDefault();
          error.clear();
          submit.disabled = true;
          try {
            await api("/v1/admin/secrets", { method: "PUT", body: { name: name.value, value: value.value } });
            refresh();
          } catch (err) {
            error.fail(err);
            submit.disabled = false;
          }
        },
      },
        h("h2", null, "Add or replace"),
        error,
        h("label", null, "Name", name),
        h("label", null, "Value", value),
        submit,
      ),
    ),
  );
}

// ---- my account ----
async function accountView() {
  const me = await api("/v1/auth/me");
  const error = errorBox();
  const note = h("div");
  const oldPassword = h("input", { type: "password", required: true, autocomplete: "current-password" });
  const next = h("input", { type: "password", required: true, minLength: 12, autocomplete: "new-password" });
  const codePassword = h("input", { type: "password", required: true, autocomplete: "current-password" });
  const codeError = errorBox();

  return h("div", null,
    heading("My account"),
    await mailSettingsCard(),
    await walletCard(),
    h("div", { class: "card" },
      h("h2", null, "Profile"),
      h("dl", { class: "facts" },
        h("dt", null, "Username"), h("dd", null, me.username),
        h("dt", null, "Role"), h("dd", null, me.is_admin ? "Administrator" : "Member"),
        h("dt", null, "Departments"),
        h("dd", { class: "row" }, me.departments.length
          ? me.departments.map((d) => h("span", { class: "pill" }, departmentName(d)))
          : "None. Ask your administrator if you need one."),
        h("dt", null, "Private chat"), h("dd", null, me.chat_enabled ? "On, encrypted with your key" : "Off until you set your own password"),
        h("dt", null, "Member since"), h("dd", null, new Date(me.created_at).toLocaleDateString()),
      ),
    ),
    h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        error.clear();
        note.replaceChildren();
        try {
          const result = await api("/v1/auth/change-password", {
            method: "POST", body: { old_password: oldPassword.value, new_password: next.value },
          });
          useToken(result, state.username);
          if (result.recovery_code) return recoveryCodeScreen(result.recovery_code);
          oldPassword.value = "";
          next.value = "";
          note.replaceChildren(h("div", { class: "notice good" }, "Password changed. Your recovery code still works."));
        } catch (err) {
          error.fail(err);
        }
      },
    },
      h("h2", null, "Change password"),
      error,
      note,
      h("label", null, "Current password", oldPassword),
      h("label", null, "New password (at least 12 characters)", next),
      h("button", { class: "primary", type: "submit" }, "Change password"),
    ),
    h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        codeError.clear();
        try {
          const result = await api("/v1/auth/recovery-code", {
            method: "POST", body: { password: codePassword.value },
          });
          recoveryCodeScreen(result.recovery_code);
        } catch (err) {
          codeError.fail(err);
        }
      },
    },
      h("h2", null, "Replace recovery code"),
      h("p", { class: "muted" }, "Lost your code? This creates a new one, and the old one stops working."),
      codeError,
      h("label", null, "Current password", codePassword),
      h("button", { type: "submit" }, "Create new code"),
    ),
  );
}


// ---- saved details: logins, cards, UPI, addresses (encrypted with the person's key) ----
const WALLET_FORMS = {
  login: [["site", "Website, e.g. amazon.in"], ["username", "Username or email"], ["password", "Password", "password"], ["label", "Name (optional)"]],
  card: [["label", "Name, e.g. HDFC credit card"], ["name", "Name on card"], ["number", "Card number", "password"], ["expiry", "Expiry (MM/YY)"]],
  upi: [["upi_id", "UPI ID, e.g. name@okhdfc"], ["label", "Name (optional)"]],
  address: [["label", "Name, e.g. Home"], ["name", "Full name"], ["phone", "Phone"], ["line1", "Address line 1"], ["line2", "Address line 2"],
    ["city", "City"], ["state", "State"], ["pincode", "PIN code"]],
};

async function walletCard() {
  const card = h("div", { class: "card" });
  const items = await api("/v1/me/wallet");
  const error = errorBox();
  const kind = h("select", null, [["login", "Website login"], ["card", "Card"], ["upi", "UPI ID"], ["address", "Address"]]
    .map(([v, t]) => h("option", { value: v }, t)));
  const fields = h("div", { class: "row wrap" });
  let inputs = {};
  const drawFields = () => {
    inputs = {};
    fill(fields, WALLET_FORMS[kind.value].map(([name, label, type]) => {
      const input = h("input", { type: type || "text", autocomplete: "off" });
      inputs[name] = input;
      return h("label", null, label, input);
    }));
  };
  kind.addEventListener("change", drawFields);
  drawFields();
  const redraw = async () => card.replaceWith(await walletCard());
  const reveal = (item, out) => async () => {
    const pw = h("input", { type: "password", autocomplete: "current-password", "aria-label": "Your account password" });
    const err = errorBox();
    out.replaceChildren(h("div", { class: "row" }, pw, h("button", { type: "button", onclick: async () => {
      err.clear();
      try {
        const data = await api(`/v1/me/wallet/${item.id}/reveal`, { method: "POST", body: { password: pw.value } });
        delete data.kind;
        out.replaceChildren(h("dl", { class: "facts" }, Object.entries(data).flatMap(([k, v]) => [h("dt", null, k), h("dd", { class: "mono" }, v)])),
          h("button", { type: "button", onclick: () => out.replaceChildren() }, "Hide"));
      } catch (e) { err.fail(e); }
    } }, "Unlock"), err));
    pw.placeholder = "Your account password";
    pw.focus();
  };
  fill(card,
    h("h2", null, "Saved details"),
    h("p", { class: "muted" }, "Logins, cards, UPI IDs and addresses the assistant can use to sign in and order for you. They're "
      + "encrypted with your key: nobody else can see them, the admin included, and the assistant never sees the values; it "
      + "only fills them in on the right site. CVVs and OTPs are never saved: you're asked at each payment."),
    items.length
      ? h("div", { class: "notes" }, items.map((item) => {
        const out = h("div");
        return h("div", { class: "card note" },
          h("div", { class: "row spread" }, h("span", null, h("span", { class: "pill" }, item.kind), " ", item.summary),
            h("div", { class: "row" },
              h("button", { type: "button", onclick: reveal(item, out) }, "Show"),
              h("button", { type: "button", class: "danger", onclick: async () => { await api(`/v1/me/wallet/${item.id}`, { method: "DELETE" }); redraw(); } }, "Delete"))),
          out);
      }))
      : h("div", { class: "empty" }, "Nothing saved yet."),
    h("form", {
      class: "settings",
      onsubmit: async (event) => {
        event.preventDefault();
        error.clear();
        const data = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
        try { await api("/v1/me/wallet", { method: "POST", body: { kind: kind.value, data } }); redraw(); } catch (e) { error.fail(e); }
      },
    }, h("h3", null, "Add"), error, h("label", null, "Kind", kind), fields, h("button", { class: "primary", type: "submit" }, "Save")),
  );
  return card;
}

// ---- notifications (the bell) ----
const KIND_LABELS = { reminder: ["Reminder", "warn"], digest: ["Mail digest", ""], alert: ["Server alert", "bad"], mail: ["Mail", ""] };

async function notificationsView(refresh) {
  const items = await api("/v1/notifications");
  const allow = "Notification" in window && Notification.permission === "default"
    && h("button", { type: "button", onclick: async () => { await Notification.requestPermission(); refresh(); } }, "Show pop-ups in this browser");
  const readAll = async () => {
    await api("/v1/notifications/read", { method: "POST", body: {} });
    state.unread = 0;
    state.counts.unread_notifications = 0;
    refresh();
  };
  const open = (n, card) => async () => {
    card.classList.toggle("open");
    if (!n.read) {
      const { unread } = await api("/v1/notifications/read", { method: "POST", body: { id: n.id } });
      state.unread = unread;
      state.counts.unread_notifications = unread;
      card.classList.remove("unread");
      n.read = true;
    }
  };
  return h("div", null,
    heading("Notifications", "Reminders, your morning mail digest and, for the admin, server alerts."),
    h("div", { class: "row" }, items.some((n) => !n.read) && h("button", { type: "button", onclick: readAll }, "Mark all read"), allow),
    items.length
      ? h("div", { class: "notes" }, items.map((n) => {
        const [label, kind] = KIND_LABELS[n.kind] || [n.kind, ""];
        const card = h("button", { type: "button", class: `card note ${n.read ? "" : "unread"}` },
          h("div", { class: "row spread" },
            h("span", { class: "row" }, h("span", { class: `pill ${kind}` }, label), h("strong", null, n.title)),
            h("span", { class: "small muted" }, new Date(n.created_at).toLocaleString()),
          ),
          n.body && mdBlock(n.body, "note-body"),
        );
        card.addEventListener("click", open(n, card));
        return card;
      }))
      : h("div", { class: "card empty" }, "Nothing yet. Reminders you set in chat appear here when they're due."),
  );
}

// ---- My files ----
async function filesView(refresh, { trash = false } = {}) {
  const data = await api(`/v1/files${trash ? "?trash=true" : ""}`);
  const error = errorBox();
  const folder = h("input", { placeholder: "Folder (optional), e.g. notes", "aria-label": "Folder" });
  const picker = h("input", { type: "file", class: "hidden-input", multiple: true });
  const page = h("div");
  const redraw = async (opts) => page.replaceChildren(await filesView(refresh, opts));
  picker.addEventListener("change", async () => {
    error.clear();
    for (const file of picker.files) {
      const form = new FormData();
      form.append("file", file);
      form.append("folder", folder.value);
      try {
        await api("/v1/files", { method: "POST", form });
      } catch (err) {
        error.fail(err);
      }
    }
    picker.value = "";
    if (!error.hidden) return;
    redraw({ trash });
  });
  const download = (f) => async () => {
    const blob = await api(`/v1/files/${f.id}`, { raw: true });
    const link = h("a", { href: URL.createObjectURL(blob), download: f.path.split("/").pop() });
    link.click();
    URL.revokeObjectURL(link.href);
  };
  const act = (fn) => async () => {
    error.clear();
    try { await fn(); redraw({ trash }); } catch (err) { error.fail(err); }
  };
  const preview = h("div");
  const view = (f) => async () => {
    const text = await (await api(`/v1/files/${f.id}`, { raw: true })).text();
    preview.replaceChildren(h("div", { class: "card" },
      h("div", { class: "row spread" }, h("strong", { class: "mono" }, f.path), h("button", { type: "button", onclick: () => preview.replaceChildren() }, "Close")),
      /\.md$/i.test(f.path) ? mdBlock(text) : h("pre", { class: "md-code" }, text),
    ));
    preview.scrollIntoView({ behavior: "smooth" });
  };
  page.append(
    heading(trash ? "Trash" : "My files", trash
      ? "Deleted files are kept for 7 days, then removed for good."
      : "Encrypted with your key: only you, and the assistant in your private chats, can open them. "
        + "Ask the assistant to write notes, edit lists or organize files. Every change keeps the old version."),
    error,
    h("div", { class: "row" },
      !trash && folder,
      !trash && h("button", { class: "primary", type: "button", onclick: () => picker.click() }, icon("plus"), "Upload"),
      picker,
      h("button", { type: "button", onclick: () => redraw({ trash: !trash }) }, trash ? "Back to files" : "Trash"),
      h("span", { class: "small muted" }, `${formatSize(data.used)} of ${formatSize(data.quota)} used`),
    ),
    h("div", { class: "card" }, table(
      ["Name", "Size", trash ? "Deleted" : "Changed", ""],
      data.files.map((f) => [
        h("span", { class: "mono" }, f.path),
        formatSize(f.size),
        new Date(trash ? f.deleted_at : f.updated_at).toLocaleString(),
        h("div", { class: "row" }, trash
          ? h("button", { type: "button", onclick: act(() => api(`/v1/files/${f.id}/restore`, { method: "POST" })) }, "Restore")
          : [
            /\.(md|txt|csv|json|html?)$/i.test(f.path) && h("button", { type: "button", onclick: view(f) }, "View"),
            h("button", { type: "button", onclick: download(f) }, "Download"),
            h("button", { type: "button", class: "danger", "aria-label": `Delete ${f.path}`, onclick: act(() => api(`/v1/files/${f.id}`, { method: "DELETE" })) }, "Delete"),
          ]),
      ]),
      trash ? "The trash is empty." : "No files yet. Upload some, or ask the assistant to write one.",
    )),
    preview,
  );
  return page;
}

// ---- reminders ----
async function remindersView(refresh) {
  const items = await api("/v1/reminders");
  const error = errorBox();
  const when = h("input", { required: true, placeholder: "tomorrow 9am, in 2 hours, friday 17:30", "aria-label": "When" });
  const text = h("input", { required: true, maxLength: 500, placeholder: "What to remember", "aria-label": "What" });
  const repeat = h("select", { "aria-label": "Repeat" },
    ["none", "daily", "weekdays", "weekly", "monthly"].map((r) => h("option", { value: r }, r === "none" ? "Once" : r[0].toUpperCase() + r.slice(1))));
  const email = h("input", { type: "checkbox" });
  return h("div", null,
    heading("Reminders", "You can also just ask in chat: \"remind me to call the bank tomorrow at 10\"."),
    h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        error.clear();
        try {
          await api("/v1/reminders", { method: "POST", body: { when: when.value, text: text.value, repeat: repeat.value, email: email.checked } });
          refresh();
        } catch (err) {
          error.fail(err);
        }
      },
    },
      h("h2", null, "New reminder"),
      error,
      h("div", { class: "row wrap" },
        h("label", { class: "grow" }, "What", text),
        h("label", null, "When", when),
        h("label", null, "Repeat", repeat),
      ),
      h("label", { class: "check" }, email, h("span", null, "Also email it to me")),
      h("button", { class: "primary", type: "submit" }, "Add reminder"),
    ),
    h("div", { class: "card" }, table(
      ["When", "What", "Repeats", ""],
      items.map((r) => [
        r.due,
        h("span", null, r.text, r.source === "mail" && h("span", { class: "pill small" }, " from mail")),
        r.repeat === "none" ? "" : r.repeat,
        h("button", { type: "button", onclick: async () => { await api(`/v1/reminders/${r.id}`, { method: "DELETE" }); refresh(); } }, "Cancel"),
      ]),
      "No reminders waiting.",
    )),
  );
}

// ---- My account: mailbox and reminder settings ----
async function mailSettingsCard() {
  const [box, prefs] = await Promise.all([api("/v1/me/mailbox"), api("/v1/me/prefs")]);
  const card = h("div", { class: "card" });
  const error = errorBox();
  const note = h("div");
  const field = (label, props) => {
    const input = h("input", props);
    return [input, h("label", null, label, input)];
  };
  const [address, addressLabel] = field("Email address", { type: "email", required: true, autocomplete: "off", value: box.username || "" });
  const [password, passwordLabel] = field("App password", { type: "password", required: true, autocomplete: "new-password", placeholder: "16 letters for Gmail" });
  const [imapHost, imapHostLabel] = field("IMAP server", { placeholder: "imap.example.com" });
  const [smtpHost, smtpHostLabel] = field("SMTP server (to send)", { placeholder: "smtp.example.com" });
  const [smtpPort, smtpPortLabel] = field("SMTP port", { type: "number", value: 465, min: 1 });
  const other = h("details", null, h("summary", { class: "small" }, "Another provider (enter its servers)"),
    h("div", { class: "row wrap" }, imapHostLabel, smtpHostLabel, smtpPortLabel));

  const tz = h("input", { value: prefs.timezone || "", placeholder: "Asia/Kolkata" });
  const emailAddr = h("input", { type: "email", value: prefs.email || "", placeholder: "Defaults to your mailbox address" });
  const digestOn = h("input", { type: "checkbox", checked: prefs.digest_hour !== null && prefs.digest_hour !== undefined });
  const digestHour = h("select", null, Array.from({ length: 24 }, (_, i) => h("option", { value: i, selected: (prefs.digest_hour ?? 8) === i }, `${String(i).padStart(2, "0")}:00`)));
  const digestEmail = h("input", { type: "checkbox", checked: !!prefs.digest_email });
  const emailReminders = h("input", { type: "checkbox", checked: !!prefs.email_reminders });
  const prefsError = errorBox();
  const prefsNote = h("div");

  const connect = h("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      error.clear();
      note.replaceChildren(h("div", { class: "notice" }, "Checking the login..."));
      try {
        await api("/v1/me/mailbox", {
          method: "PUT",
          body: {
            address: address.value, password: password.value,
            imap_host: imapHost.value, imap_port: 993, smtp_host: smtpHost.value, smtp_port: Number(smtpPort.value) || 465,
          },
        });
        password.value = "";
        card.replaceWith(await mailSettingsCard());
      } catch (err) {
        note.replaceChildren();
        error.fail(err);
      }
    },
  },
    h("p", { class: "muted" }, "Connect your own mailbox so the assistant can read, summarize and answer your mail in your private chats. "
      + "Sending always asks you first. The servers are chosen from your address (Gmail first)."),
    h("p", { class: "small muted" }, "Gmail: your normal password won't work. Turn on 2-Step Verification, then create an app password at ",
      mdLink("https://myaccount.google.com/apppasswords", "myaccount.google.com/apppasswords"),
      " and paste it here. Outlook/Hotmail can't connect with a password (Microsoft blocks it)."),
    error, note,
    h("div", { class: "row wrap" }, addressLabel, passwordLabel),
    other,
    h("button", { class: "primary", type: "submit" }, box.connected ? "Update mailbox" : "Connect mailbox"),
  );

  const digestNow = h("button", { type: "button" }, "Summarize my mail now");
  digestNow.addEventListener("click", async () => {
    prefsError.clear();
    digestNow.disabled = true;
    prefsNote.replaceChildren(h("div", { class: "notice" }, "Reading your mail..."));
    try {
      const { text } = await api("/v1/me/digest", { method: "POST" });
      prefsNote.replaceChildren(h("div", { class: "notice good" }, mdBlock(text || "No new mail.")));
    } catch (err) {
      prefsNote.replaceChildren();
      prefsError.fail(err);
    }
    digestNow.disabled = false;
  });

  fill(card,
    h("h2", null, "Mail and reminders"),
    box.connected && h("div", { class: "row spread" },
      h("span", null, h("span", { class: "pill good" }, "Connected"), ` ${box.username} (${box.imap_host})`),
      h("button", {
        type: "button", class: "danger",
        onclick: async () => { await api("/v1/me/mailbox", { method: "DELETE" }); card.replaceWith(await mailSettingsCard()); },
      }, "Disconnect"),
    ),
    connect,
    h("form", {
      class: "settings",
      onsubmit: async (event) => {
        event.preventDefault();
        prefsError.clear();
        try {
          await api("/v1/me/prefs", {
            method: "PUT",
            body: {
              timezone: tz.value.trim(), email: emailAddr.value.trim(),
              digest_hour: digestOn.checked ? Number(digestHour.value) : null,
              digest_email: digestEmail.checked, email_reminders: emailReminders.checked,
            },
          });
          prefsNote.replaceChildren(h("div", { class: "notice good" }, "Saved."));
        } catch (err) {
          prefsError.fail(err);
        }
      },
    },
      h("h3", null, "Daily digest and reminders"),
      prefsError, prefsNote,
      h("div", { class: "row" },
        h("label", { class: "check" }, digestOn, h("span", null, "Every morning, summarize my new mail and set reminders for deadlines in it, at")),
        digestHour,
      ),
      h("label", { class: "check" }, digestEmail, h("span", null, "Also email me the digest")),
      h("label", { class: "check" }, emailReminders, h("span", null, "Email me every reminder, too")),
      h("div", { class: "row wrap" },
        h("label", null, "Time zone", tz),
        h("label", { class: "grow" }, "Email address for reminders", emailAddr),
      ),
      h("div", { class: "row" },
        h("button", { class: "primary", type: "submit" }, "Save"),
        box.connected && digestNow,
      ),
    ),
  );
  return card;
}

// ---- document templates ----
async function templatesView(refresh) {
  const rows = await api("/v1/templates");
  const error = errorBox();
  const listError = errorBox();
  const scopes = [...state.roles.filter((r) => r !== "admin"), ...(has("admin") ? [...DEPARTMENTS, "global"] : [])]
    .filter((v, i, a) => a.indexOf(v) === i);
  const name = h("input", { required: true, maxLength: 128, placeholder: "e.g. Offer letter" });
  const description = h("input", { maxLength: 512, placeholder: "What it's for (optional)" });
  const scope = h("select", null, scopes.map((sc) => h("option", { value: sc }, sc === "global" ? "Everyone (global)" : departmentName(sc))));
  const file = h("input", { type: "file", required: true, accept: ".docx,.md,.txt,.html,.csv,.json,.pdf" });
  const download = (t) => async () => {
    const blob = await api(`/v1/templates/${t.id}`, { raw: true });
    const link = h("a", { href: URL.createObjectURL(blob), download: t.filename });
    link.click();
    URL.revokeObjectURL(link.href);
  };
  return h("div", null,
    heading("Templates", "Company document templates. Ask the assistant at work, e.g. \"make offer letters for these "
      + "three people from the Offer letter template\". Your own templates go in My files, folder templates/, or attach one to a chat."),
    listError,
    h("div", { class: "card" }, table(
      ["Name", "For", "Format", "Fills in", ""],
      rows.map((t) => [
        h("span", null, h("strong", null, t.name), t.description && h("div", { class: "small muted" }, t.description)),
        t.scope === "global" ? "Everyone" : departmentName(t.scope),
        t.filename.split(".").pop().toUpperCase(),
        t.fields.length ? h("span", { class: "mono small" }, t.fields.join(", ")) : h("span", { class: "muted small" }, "Example: imitated"),
        h("div", { class: "row" },
          h("button", { type: "button", onclick: download(t) }, "Download"),
          h("button", { type: "button", class: "danger", onclick: async () => {
            listError.clear();
            try { await api(`/v1/templates/${t.id}`, { method: "DELETE" }); refresh(); } catch (err) { listError.fail(err); }
          } }, "Delete"),
        ),
      ]),
      "No templates yet.",
    )),
    scopes.length > 0 && h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        error.clear();
        const form = new FormData();
        form.append("file", file.files[0]);
        form.append("name", name.value);
        form.append("scope", scope.value);
        form.append("description", description.value);
        try { await api("/v1/templates", { method: "POST", form }); refresh(); } catch (err) { error.fail(err); }
      },
    },
      h("h2", null, "Add a template"),
      error,
      h("div", { class: "row wrap" },
        h("label", null, "Name", name),
        h("label", null, "For", scope),
        h("label", { class: "grow" }, "Description", description),
      ),
      h("label", null, "File (Word, Markdown, text, HTML, CSV, JSON; a PDF works as an example)", file),
      h("p", { class: "small muted" }, "Mark the parts to fill with {{ name }}, {{ joining_date }} and so on. In Word, "
        + "a table row using {{ item.description }} and {{ item.amount }} repeats once per line item. A file "
        + "without placeholders works too: new documents copy its structure and style with the new facts."),
      h("button", { class: "primary", type: "submit" }, "Upload"),
    ),
  );
}

loginScreen();
