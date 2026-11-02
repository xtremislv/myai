"use strict";

// The token can open the user's private chats, so it stays in memory only:
// reloading the page signs out. All page text is set through textContent.
const state = {
  token: null, username: "", roles: [], view: "chat", thread: null, threads: [], counts: {}, poll: null,
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
  Object.assign(state, {
    token: null, username: "", roles: [], view: "chat", thread: null, threads: [], counts: {}, poll: null,
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
    ["overview", "Overview", overviewView],
    ["account", "My account", accountView],
  ].filter(Boolean);
}

function newThreadId() {
  return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

async function refreshCounts() {
  try {
    state.counts = await api("/v1/dashboard");
    state.roles = state.counts.roles;
  } catch { /* the page itself reports errors */ }
  return state.counts;
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
    const node = h("div", { class: `msg ${role}` }, text,
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
        item.result && h("div", { class: "pre" }, item.result),
      );
    }));
    return items.some((item) => !FINISHED.has(item.state));
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
        const result = await streamChat({ message: text, thread_id: thread }, (piece) => {
          if (!started) {
            started = true;
            thinking.classList.remove("thinking");
            thinking.textContent = "";
          }
          thinking.append(piece);
          toBottom();
        }, onStep);
        thinking.remove();
        if (result.outcome === "flagged_nsfw") {
          mine.remove();
          bubble("flagged", "This message was blocked and reported to HR and the admin. It wasn't saved in this chat.");
        } else {
          bubble("assistant", result.reply, result.route === "work" ? result.department : null);
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
    await watchRequests();
  }
  await paintFiles();
  setTimeout(() => { input.focus(); toBottom(); }, 0);
  return content;
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
  return h("div", { class: `notice ${result.outcome === "error" ? "error" : "good"} pre` }, text);
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
      isMe && h("span", { class: "small muted" }, "Change your own password under My account."),
    ),
    reset,
    toggle,
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

loginScreen();
