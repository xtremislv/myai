"use strict";

// The token can open the user's private chats, so it stays in memory only:
// reloading the page signs out. All page text is set through textContent.
const state = { token: null, username: "", roles: [], view: "overview", counts: {}, requests: [] };
const root = document.getElementById("app");

const OUTCOMES = {
  ok: ["Completed", "good"],
  forwarded: ["Sent to accounts", "warn"],
  pending_approval: ["Waiting for approval", "warn"],
  flagged_nsfw: ["Blocked and reported", "bad"],
  denied: ["Denied", "bad"],
  denied_auth: ["Not allowed", "bad"],
  denied_quota: ["Rate limited", "bad"],
  error: ["Failed", "bad"],
};

// ---- small DOM helper ----
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

function show(...nodes) {
  root.replaceChildren(...nodes);
}

// replaceChildren would print `false` from a `condition && node` as text.
function fill(el, ...nodes) {
  el.replaceChildren(...nodes.flat().filter(Boolean));
}

function field(label, props) {
  return h("label", null, label, h("input", props));
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
    signOut("Your session ended. Please sign in again.");
    throw new Error("Session ended");
  }
  if (!response.ok) {
    let detail = `Something went wrong (${response.status}).`;
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

function useToken(result, username) {
  state.token = result.access_token;
  const payload = result.access_token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  // For choosing which pages to show. The server checks every action itself.
  state.roles = JSON.parse(atob(payload)).roles || [];
  state.username = username;
}

function signOut(message) {
  Object.assign(state, { token: null, username: "", roles: [], view: "overview", counts: {}, requests: [] });
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
    h("h1", null, "AI Platform"),
    message && h("div", { class: "notice info" }, message),
    error,
    h("label", null, "Username", username),
    h("label", null, "Password", password),
    h("div", { class: "row spread" },
      submit,
      h("button", { class: "link", type: "button", onclick: recoverScreen }, "Forgot password?"),
    ),
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
      if (next.value !== again.value) return error.fail(new Error("The passwords do not match."));
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
    h("p", { class: "muted" }, "Your administrator chose your first password. Set your own to turn on private chat. "
      + "Only your password can unlock your chats, so your administrator will not be able to read them."),
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
      + "It will not be shown again, and nobody can reset it for you."),
    h("div", { class: "code", id: "recovery-code" }, code),
    h("div", { class: "row" }, copy),
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
      + "The code can be used once, and you will get a new one."),
    error,
    h("label", null, "Username", username),
    h("label", null, "Recovery code", code),
    h("label", null, "New password (at least 12 characters)", next),
    h("div", { class: "row spread" },
      h("button", { class: "primary", type: "submit" }, "Recover"),
      h("button", { class: "link", type: "button", onclick: () => loginScreen() }, "Back to sign in"),
    ),
  )));
  username.focus();
}

// ---- shell ----
function pages() {
  return [
    ["overview", "Overview", overviewView],
    ["chat", "Private chat", chatView],
    ["requests", "Requests", requestsView],
    has("admin") && ["approvals", "Approvals", approvalsView],
    has("admin", "account") && ["inbox", "Accounts inbox", inboxView],
    has("admin", "hr", "account") && ["reports", "Reports", reportsView],
    has("admin") && ["users", "Users", usersView],
    ["account", "My account", accountView],
  ].filter(Boolean);
}

const BADGES = { approvals: "pending_approvals", inbox: "inbox_queued", reports: "open_reports" };

async function refreshCounts() {
  try {
    state.counts = await api("/v1/dashboard");
  } catch { /* the page itself reports errors */ }
  return state.counts;
}

function shell() {
  const main = h("main", { class: "main" });
  const nav = h("nav", { class: "nav", "aria-label": "Main" });

  const paint = () => {
    nav.replaceChildren(
      h("div", { class: "brand" }, "AI Platform"),
      ...pages().map(([id, label]) => {
        const count = state.counts[BADGES[id]];
        return h("button", {
          class: state.view === id ? "active" : "",
          "aria-current": state.view === id ? "page" : null,
          onclick: () => go(id),
        }, h("span", null, label), count > 0 && h("span", { class: "badge" }, count));
      }),
      h("div", { class: "who small" },
        h("div", null, state.username),
        h("div", { class: "muted" }, state.roles.join(", ") || "no department"),
        h("button", { class: "link", onclick: () => signOut() }, "Sign out"),
      ),
    );
  };

  const go = async (id) => {
    state.view = id;
    const [, , view] = pages().find(([page]) => page === id);
    paint();
    main.replaceChildren(h("div", { class: "muted" }, "Loading..."));
    try {
      const content = await view(() => refresh());
      main.replaceChildren(content);
    } catch (err) {
      if (state.token) main.replaceChildren(h("div", { class: "notice error", role: "alert" }, err.message));
    }
    refreshCounts().then(() => { if (state.token) paint(); });
  };
  const refresh = () => go(state.view);
  state.go = go;

  show(h("div", { class: "shell" }, nav, main));
  go(state.view);
}

function heading(title, subtitle) {
  return h("div", { class: "head" }, h("h1", null, title), subtitle && h("p", { class: "muted" }, subtitle));
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
    tile(has("admin") ? "Requests, last 24 hours" : "Your requests, last 24 hours", total),
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
    heading("Overview", `Signed in as ${state.username}. Private chats are never counted here.`),
    h("div", { class: "tiles" }, tiles),
    h("div", { class: "card" },
      h("h2", null, "Requests by department, last 24 hours"),
      total ? activityChart(data.activity) : h("div", { class: "empty" }, "No requests in the last 24 hours."),
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

// ---- private chat ----
function newThreadId() {
  return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
}

function chatDisabled() {
  return h("div", null,
    heading("Private chat"),
    h("div", { class: "notice warn" }, "Set your own password to turn on private chat."),
    h("button", { class: "primary", onclick: () => state.go("account") }, "Go to My account"),
  );
}

async function chatView() {
  let threads;
  try {
    threads = await api("/v1/chat/threads");
  } catch (err) {
    if (err.message.includes("change your password")) return chatDisabled();
    throw err;
  }

  let current = threads[0] ? threads[0].thread_id : newThreadId();
  const list = h("div", { class: "threads" });
  const messages = h("div", { class: "messages", "aria-live": "polite" });
  const files = h("div", { class: "files" });
  const error = errorBox();
  const input = h("textarea", { placeholder: "Write a message", "aria-label": "Message", required: true });
  const mode = h("select", { "aria-label": "How the AI works" },
    h("option", { value: "single" }, "Quick answer"),
    h("option", { value: "team" }, "Plan, research, write"),
  );
  mode.classList.add("grow");
  const send = h("button", { class: "primary", type: "submit" }, "Send");
  const picker = h("input", { type: "file", class: "hidden-input", id: "file-picker", onchange: () => upload() });

  const bubble = (role, text) => {
    const node = h("div", { class: `msg ${role}` }, text);
    messages.append(node);
    messages.scrollTop = messages.scrollHeight;
    return node;
  };

  const paintThreads = () => {
    const known = threads.some((t) => t.thread_id === current);
    fill(list,
      h("button", { class: "primary", onclick: () => open(newThreadId()) }, "New chat"),
      !known && h("button", { class: "item active" }, h("span", { class: "title" }, "New chat")),
      ...threads.map((t) => h("button", {
        class: `item ${t.thread_id === current ? "active" : ""}`,
        onclick: () => open(t.thread_id),
      },
        h("span", { class: "title" }, t.title || "Untitled"),
        h("span", { class: "small muted" }, new Date(t.updated_at).toLocaleDateString()),
      )),
    );
  };

  const paintFiles = async () => {
    const items = await api(`/v1/chat/attachments?thread_id=${encodeURIComponent(current)}`);
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

  const open = async (threadId) => {
    current = threadId;
    error.clear();
    paintThreads();
    messages.replaceChildren();
    if (threads.some((t) => t.thread_id === threadId)) {
      const history = await api(`/v1/chat/threads/${encodeURIComponent(threadId)}/messages`);
      history.forEach((m) => bubble(m.role, m.content));
    }
    await paintFiles();
    input.focus();
  };

  const upload = async () => {
    const file = picker.files[0];
    if (!file) return;
    error.clear();
    const form = new FormData();
    form.append("thread_id", current);
    form.append("file", file);
    try {
      const result = await api("/v1/chat/attachments", { method: "POST", form });
      if (result.outcome === "flagged_nsfw") {
        bubble("flagged", `"${file.name}" was blocked and reported to HR and the admin.`);
      }
      await paintFiles();
    } catch (err) {
      error.fail(err);
    }
    picker.value = "";
  };

  const composer = h("form", {
    class: "composer",
    onsubmit: async (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      error.clear();
      input.value = "";
      send.disabled = true;
      bubble("user", text);
      const thinking = bubble("assistant", "Thinking...");
      try {
        const result = await api("/v1/chat", {
          method: "POST", body: { message: text, thread_id: current, mode: mode.value },
        });
        thinking.remove();
        if (result.outcome === "flagged_nsfw") {
          messages.lastChild.remove();
          bubble("flagged", "This message was blocked and reported to HR and the admin. It was not saved in this chat.");
        } else {
          bubble("assistant", result.reply);
          threads = await api("/v1/chat/threads");
          paintThreads();
        }
      } catch (err) {
        thinking.remove();
        input.value = text;
        messages.lastChild.remove();
        error.fail(err);
      }
      send.disabled = false;
      input.focus();
    },
  },
    files,
    error,
    input,
    h("div", { class: "row" },
      send,
      h("button", { type: "button", onclick: () => picker.click() }, "Attach file"),
      picker,
      h("div", { class: "grow" }, mode),
    ),
    h("p", { class: "small muted" }, "Files are deleted after 3 days. The AI can read text files and PDFs."),
  );
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });

  const content = h("div", { class: "chat" },
    h("h1", { class: "sr-only" }, "Private chat"),
    list,
    h("div", { class: "conversation" },
      h("div", { class: "notice info small" }, "Private and encrypted with your own key. "
        + "Only explicit or illegal content is ever reported, to HR and the admin."),
      messages,
      composer,
    ),
  );
  await open(current);
  return content;
}

// ---- requests ----
function requestsView() {
  const error = errorBox();
  const results = h("div");
  const paint = () => {
    results.replaceChildren(table(
      ["Request", "Department", "Result", "Reply"],
      state.requests.map((r) => [
        h("div", { class: "pre" }, r.message), r.department || "-", outcomePill(r.outcome),
        h("div", { class: "pre" }, r.reply || r.reason || ""),
      ]),
      "Requests you send in this session appear here.",
    ));
  };
  paint();
  const message = h("textarea", { required: true, rows: 3 });
  const department = h("select", null,
    h("option", { value: "" }, "Choose automatically"),
    (has("admin") ? ["account", "hr", "logistics", "researcher", "superuser", "admin"] : state.roles)
      .map((d) => h("option", { value: d }, d)),
  );
  const mode = h("select", null,
    h("option", { value: "single" }, "Quick answer"),
    h("option", { value: "team" }, "Plan, research, write"),
  );
  const submit = h("button", { class: "primary", type: "submit" }, "Send request");

  return h("div", null,
    heading("Requests", "Department work: tools, data, quotas and approvals apply. Requests are recorded in the audit log."),
    h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        error.clear();
        submit.disabled = true;
        const text = message.value;
        try {
          const result = await api("/v1/request", {
            method: "POST",
            // Each request gets its own conversation, so one waiting on approval blocks nothing.
            body: { message: text, thread_id: newThreadId(), mode: mode.value, department: department.value || null },
          });
          state.requests.unshift({ message: text, ...result });
          message.value = "";
          paint();
        } catch (err) {
          error.fail(err);
        }
        submit.disabled = false;
      },
    },
      error,
      h("label", null, "What do you need?", message),
      h("div", { class: "row" },
        h("label", { class: "grow" }, "Department", department),
        h("label", { class: "grow" }, "How the AI works", mode),
      ),
      submit,
    ),
    h("div", { class: "card" }, h("h2", null, "This session"), results),
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
  const feedback = h("div");
  const error = errorBox();
  const decide = (id, verb) => async () => {
    error.clear();
    try {
      const result = await api(`/v1/approvals/${id}/${verb}`, { method: "POST" });
      const content = await approvalsView(refresh);
      content.prepend(resultNotice(result));
      document.querySelector(".main").replaceChildren(content);
      refreshCounts();
    } catch (err) {
      error.fail(err);
    }
  };
  return h("div", null,
    heading("Approvals", "These actions are paused until you decide. Accounts is notified of each decision on a finance action."),
    feedback,
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
      document.querySelector(".main").replaceChildren(content);
      refreshCounts();
    } catch (err) {
      error.fail(err);
      event.target.disabled = false;
    }
  };
  return h("div", null,
    heading("Accounts inbox", "Orders, bookings and bills forwarded to accounts."),
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
async function usersView(refresh) {
  const rows = await api("/v1/auth/users");
  const error = errorBox();
  const username = h("input", { required: true, autocomplete: "off" });
  const password = h("input", { type: "password", required: true, minLength: 12, autocomplete: "new-password" });
  const boxes = ["account", "hr", "logistics", "researcher", "superuser"].map((role) => {
    const box = h("input", { type: "checkbox", value: role });
    return [box, h("label", { class: "check" }, box, h("span", null, role))];
  });
  return h("div", null,
    heading("Users", "You choose a first password. Each user replaces it to turn on private chat, and you cannot read their chats."),
    h("div", { class: "card" }, table(
      ["Username", "Departments", "Private chat"],
      rows.map((u) => [
        u.username,
        u.roles.join(", ") || "None",
        h("span", { class: `pill ${u.chat_enabled ? "good" : "warn"}` }, u.chat_enabled ? "On" : "Waiting for own password"),
      ]),
      "No users yet.",
    )),
    h("form", {
      class: "card",
      onsubmit: async (event) => {
        event.preventDefault();
        error.clear();
        try {
          await api("/v1/auth/users", {
            method: "POST",
            body: {
              username: username.value,
              password: password.value,
              roles: boxes.filter(([box]) => box.checked).map(([box]) => box.value),
            },
          });
          refresh();
        } catch (err) {
          error.fail(err);
        }
      },
    },
      h("h2", null, "Add a user"),
      error,
      h("label", null, "Username", username),
      h("label", null, "First password (at least 12 characters)", password),
      h("div", { class: "row" }, boxes.map(([, label]) => label)),
      h("button", { class: "primary", type: "submit" }, "Add user"),
    ),
  );
}

// ---- my account ----
function accountView() {
  const error = errorBox();
  const note = h("div");
  const oldPassword = h("input", { type: "password", required: true, autocomplete: "current-password" });
  const next = h("input", { type: "password", required: true, minLength: 12, autocomplete: "new-password" });
  const codePassword = h("input", { type: "password", required: true, autocomplete: "current-password" });
  const codeError = errorBox();

  return h("div", null,
    heading("My account", `${state.username} · ${state.roles.join(", ") || "no department"}`),
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
