/* Venner PWA frontend. Vanilla JS, no dependencies. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  token: localStorage.getItem("venner_token") || "",
  user: null,
  friends: [],
  categories: [],
  candidates: [],
  notifications: [],
  shownPushes: new Set(JSON.parse(localStorage.getItem("venner_pushes") || "[]")),
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, { method = "GET", body = null } = {}) {
  const res = await fetch(path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(state.token ? { Authorization: "Bearer " + state.token } : {}),
    },
    body: body ? JSON.stringify(body) : null,
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && state.token) {
    logout();
    throw new Error("session expired - please sign in again");
  }
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
}

function initials(first, last) {
  const a = (first || "").trim().charAt(0), b = (last || "").trim().charAt(0);
  return ((a + b) || "?").toUpperCase();
}

function friendName(f) {
  const full = `${f.first_name || ""} ${f.last_name || ""}`.trim();
  return full || f.phone || f.email || "Unnamed";
}

function catById(id) {
  return state.categories.find((c) => c.id === id) || null;
}

function avatarTone(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 997;
  return "tone-" + (h % 3);
}

/* ---------------- auth & boot ---------------- */
function showView(name) {
  $("#view-login").hidden = name !== "login";
  $("#view-change").hidden = name !== "change";
  $("#app").hidden = name !== "app";
}

function logout() {
  state.token = "";
  state.user = null;
  localStorage.removeItem("venner_token");
  showView("login");
}

async function boot() {
  $("#form-login").addEventListener("submit", onLogin);
  $("#form-change").addEventListener("submit", onChangePassword);
  $("#btn-logout").addEventListener("click", logout);
  $$(".nav-item").forEach((btn) =>
    btn.addEventListener("click", () => showTab(btn.dataset.tab)));
  if (!state.token) return showView("login");
  try {
    const { user } = await api("/api/me");
    state.user = user;
    if (user.must_change_password) return showView("change");
    await enterApp();
  } catch {
    showView("login");
  }
}

async function onLogin(ev) {
  ev.preventDefault();
  $("#login-error").textContent = "";
  try {
    const { token, user } = await api("/api/login", {
      method: "POST",
      body: { username: $("#login-user").value.trim(), password: $("#login-pass").value },
    });
    state.token = token;
    state.user = user;
    localStorage.setItem("venner_token", token);
    $("#login-pass").value = "";
    if (user.must_change_password) {
      showView("change");
      toast("Temporary password accepted - pick your own now.");
    } else {
      await enterApp();
    }
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
}

async function onChangePassword(ev) {
  ev.preventDefault();
  const err = $("#change-error");
  err.textContent = "";
  const a = $("#change-new").value, b = $("#change-repeat").value;
  if (a !== b) { err.textContent = "Passwords do not match."; return; }
  if (a.length < 8) { err.textContent = "Use at least 8 characters."; return; }
  try {
    const { user } = await api("/api/change-password", {
      method: "POST", body: { newPassword: a },
    });
    state.user = user;
    $("#change-new").value = "";
    $("#change-repeat").value = "";
    toast("Password saved. Welcome to Venner!");
    await enterApp();
  } catch (e) { err.textContent = e.message; }
}

async function enterApp() {
  showView("app");
  const adminTag = state.user.is_admin ? '<span class="admin-tag">admin</span>' : "";
  $("#user-chip").innerHTML = `${esc(state.user.username)}${adminTag}`;
  $("#nav-admin").hidden = !state.user.is_admin;
  try {
    await refreshAll();
  } catch {
    logout();
    return;
  }
  showTab("reminders");
  startReminderScheduler();
}

async function refreshAll() {
  const [friends, cats, merge, notifs] = await Promise.all([
    api("/api/friends"), api("/api/categories"),
    api("/api/merge-candidates"), api("/api/notifications"),
  ]);
  state.friends = friends.friends;
  state.categories = cats.categories;
  state.candidates = merge.candidates;
  state.notifications = notifs.notifications;
  updateBadges();
}

function updateBadges() {
  const unread = state.notifications.filter((n) => !n.is_read).length;
  const bR = $("#badge-reminders");
  bR.hidden = unread === 0;
  bR.textContent = unread;
  const bM = $("#badge-merge");
  bM.hidden = state.candidates.length === 0;
  bM.textContent = state.candidates.length;
}

const TAB_LOADERS = {};
function showTab(name) {
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => { t.hidden = t.id !== "tab-" + name; });
  if (TAB_LOADERS[name]) TAB_LOADERS[name]();
}

/* ---------------- modal ---------------- */
function openModal(html) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-back"><div class="modal" role="dialog">${html}</div></div>`;
  root.querySelector(".modal-back").addEventListener("click", (ev) => {
    if (ev.target.classList.contains("modal-back")) closeModal();
  });
}
function closeModal() { $("#modal-root").innerHTML = ""; }
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeModal(); });

/* ---------------- reminders ---------------- */
TAB_LOADERS.reminders = async function renderReminders() {
  const el = $("#tab-reminders");
  try {
    const { notifications } = await api("/api/notifications");
    state.notifications = notifications;
    updateBadges();
  } catch (e) { el.innerHTML = `<div class="card">Could not load reminders: ${esc(e.message)}</div>`; return; }
  const unread = state.notifications.filter((n) => !n.is_read);
  const pushState = "Notification" in window ? Notification.permission : "unsupported";
  el.innerHTML = `
    <div class="tab-head">
      <div><h2>Reminders</h2><p>${unread.length} unread. Policies on your lists decide when these appear.</p></div>
      <div class="btn-row">
        <button class="btn ghost small" id="btn-push">${pushState === "granted" ? "Browser alerts on" : "Enable browser alerts"}</button>
        <button class="btn sun small" id="btn-refresh-rem">Refresh</button>
      </div>
    </div>
    <div class="card">
      ${state.notifications.length === 0 ? emptyState("No reminders yet. Import friends, put them on a list, and Venner will nudge you.") : ""}
      ${state.notifications.map((n) => `
        <div class="row-item reminder ${n.is_read ? "read" : ""}">
          <span class="dot ${esc(n.type)}"></span>
          <div class="row-main">
            <div>${esc(n.message)}</div>
            <div class="sub"><span class="when">${esc(n.due_at || "")}</span> &middot; ${esc(n.type)}</div>
          </div>
          <div class="row-side">${n.is_read ? "" : `<button class="btn small" data-read="${n.id}">Done</button>`}</div>
        </div>`).join("")}
    </div>`;
  $("#btn-refresh-rem").addEventListener("click", async () => {
    await api("/api/notifications/generate", { method: "POST" });
    renderReminders();
  });
  $("#btn-push").addEventListener("click", enablePush);
  $$("[data-read]", el).forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/notifications/${b.dataset.read}/read`, { method: "POST" });
    renderReminders();
  }));
};

function emptyState(text) {
  return `<div class="empty">
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="10" cy="10" r="7.5"/><path d="M10 6v4l2.5 2.5"/></svg>
    <div>${esc(text)}</div></div>`;
}

async function enablePush() {
  if (!("Notification" in window)) return toast("This browser has no notification support.", "error");
  const perm = await Notification.requestPermission();
  if (perm === "granted") {
    toast("Browser alerts enabled.");
    showDuePushes(true);
  } else {
    toast("Browser alerts blocked in this browser.", "error");
  }
  TAB_LOADERS.reminders();
}

let schedulerStarted = false;
function startReminderScheduler() {
  if (schedulerStarted) return;
  schedulerStarted = true;
  setTimeout(() => showDuePushes(false), 4000);
  setInterval(() => showDuePushes(false), 60000);
}

async function showDuePushes(force) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    const { notifications } = await api("/api/notifications");
    state.notifications = notifications;
    updateBadges();
  } catch { return; }
  const today = new Date().toISOString().slice(0, 10);
  const due = state.notifications.filter(
    (n) => !n.is_read && (n.due_at || "9999") <= today && !state.shownPushes.has(n.id));
  const reg = await navigator.serviceWorker?.getRegistration().catch(() => null);
  for (const n of due.slice(0, 5)) {
    const title = n.type === "birthday" ? "Birthday" : n.type === "avoid" ? "Stay away" : "Reach out";
    if (reg?.showNotification) {
      reg.showNotification(`Venner: ${title}`, { body: n.message, tag: "venner-" + n.id });
    } else {
      new Notification(`Venner: ${title}`, { body: n.message, tag: "venner-" + n.id });
    }
    state.shownPushes.add(n.id);
  }
  localStorage.setItem("venner_pushes", JSON.stringify([...state.shownPushes].slice(-200)));
  if (force && due.length === 0) toast("No due reminders right now.");
}

/* ---------------- friends ---------------- */
TAB_LOADERS.friends = function renderFriends() {
  const el = $("#tab-friends");
  const catOpts = state.categories.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  el.innerHTML = `
    <div class="tab-head">
      <div><h2>Friends</h2><p>${state.friends.length} people. Put each on a list to set their reminders.</p></div>
      <button class="btn sun small" id="btn-add-friend">Add friend</button>
    </div>
    <div class="toolbar">
      <input type="search" id="q" placeholder="Search name, phone, email...">
      <select id="f-cat"><option value="">All lists</option>${catOpts}<option value="none">No list</option></select>
    </div>
    <div class="card" id="friend-list"></div>`;
  const paint = () => {
    const q = ($("#q").value || "").toLowerCase();
    const fc = $("#f-cat").value;
    const rows = state.friends.filter((f) => {
      const hay = `${f.first_name} ${f.last_name} ${f.phone} ${f.email}`.toLowerCase();
      if (q && !hay.includes(q)) return false;
      if (fc === "none") return !f.category_id;
      if (fc) return String(f.category_id) === fc;
      return true;
    });
    $("#friend-list").innerHTML = rows.length === 0 ? emptyState("Nobody matches. Add a friend or try an import.") : rows.map((f) => {
      const cat = catById(f.category_id);
      const name = friendName(f);
      const bits = [f.birthday ? "born " + esc(f.birthday) : "", f.phone ? esc(f.phone) : "", f.email ? esc(f.email) : ""].filter(Boolean).join(" &middot; ");
      return `<div class="row-item">
        <div class="avatar ${avatarTone(name)}">${esc(initials(f.first_name, f.last_name))}</div>
        <div class="row-main">
          <div class="name">${esc(name)}</div>
          <div class="sub">${bits || "no details yet"} &middot; via ${esc(f.source || "manual")}</div>
          <div style="margin-top:6px; display:flex; gap:6px; flex-wrap:wrap;">
            ${cat ? `<span class="chip ${cat.policy_type === "avoid" ? "avoid" : ""}">${esc(cat.name)}</span>` : `<span class="chip quiet">no list</span>`}
            ${f.last_contact ? `<span class="chip quiet">last contact ${esc(f.last_contact.slice(0, 10))}</span>` : ""}
          </div>
        </div>
        <div class="row-side">
          <button class="btn small" data-contact="${f.id}" title="Mark as contacted today">Contacted</button>
          <button class="btn ghost small" data-edit="${f.id}">Edit</button>
        </div>
      </div>`;
    }).join("");
    $$("[data-edit]", el).forEach((b) => b.addEventListener("click", () => friendModal(Number(b.dataset.edit))));
    $$("[data-contact]", el).forEach((b) => b.addEventListener("click", async () => {
      await api(`/api/friends/${b.dataset.contact}/contact`, { method: "POST" });
      const { friends } = await api("/api/friends");
      state.friends = friends;
      paint();
      toast("Marked as contacted.");
    }));
  };
  $("#q").addEventListener("input", paint);
  $("#f-cat").addEventListener("change", paint);
  $("#btn-add-friend").addEventListener("click", () => friendModal(null));
  paint();
};

function friendModal(fid) {
  const f = fid ? state.friends.find((x) => x.id === fid) : null;
  const catOpts = `<option value="">No list</option>` + state.categories.map((c) =>
    `<option value="${c.id}" ${f && f.category_id === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  openModal(`
    <h2>${f ? "Edit friend" : "Add friend"}</h2>
    <form id="form-friend">
      <div class="form-row">
        <label>First name<input name="first_name" value="${esc(f?.first_name || "")}"></label>
        <label>Last name<input name="last_name" value="${esc(f?.last_name || "")}"></label>
      </div>
      <div class="form-row">
        <label>Birthday (YYYY-MM-DD)<input name="birthday" placeholder="1990-05-14" value="${esc(f?.birthday || "")}"></label>
        <label>Phone<input name="phone" value="${esc(f?.phone || "")}"></label>
      </div>
      <label>Email<input name="email" value="${esc(f?.email || "")}"></label>
      <label>List<select name="category_id">${catOpts}</select></label>
      <label>Notes<textarea name="notes" rows="2">${esc(f?.notes || "")}</textarea></label>
      <div class="btn-row" style="margin-top:14px;">
        <button class="btn primary" type="submit">Save</button>
        ${f ? `<button class="btn danger" type="button" id="btn-del-friend">Remove</button>` : ""}
        <button class="btn ghost" type="button" id="btn-cancel">Cancel</button>
      </div>
    </form>`);
  $("#btn-cancel").addEventListener("click", closeModal);
  $("#form-friend").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const body = Object.fromEntries(fd.entries());
    body.category_id = body.category_id ? Number(body.category_id) : null;
    try {
      if (f) await api(`/api/friends/${f.id}`, { method: "PUT", body });
      else await api("/api/friends", { method: "POST", body });
      closeModal();
      const { friends } = await api("/api/friends");
      state.friends = friends;
      TAB_LOADERS.friends();
      toast("Saved.");
    } catch (e) { toast(e.message, "error"); }
  });
  const del = $("#btn-del-friend");
  if (del) del.addEventListener("click", async () => {
    if (!confirm(`Remove ${friendName(f)}?`)) return;
    await api(`/api/friends/${f.id}`, { method: "DELETE" });
    closeModal();
    const { friends } = await api("/api/friends");
    state.friends = friends;
    const { candidates } = await api("/api/merge-candidates");
    state.candidates = candidates;
    updateBadges();
    TAB_LOADERS.friends();
    toast("Removed.");
  });
}

/* ---------------- categories & policies ---------------- */
TAB_LOADERS.categories = function renderCategories() {
  const el = $("#tab-categories");
  const counts = {};
  state.friends.forEach((f) => { counts[f.category_id] = (counts[f.category_id] || 0) + 1; });
  const policyLabel = (c) =>
    c.policy_type === "contact" ? `Remind me to contact them <strong>${esc(c.frequency)}</strong>` :
    c.policy_type === "avoid" ? `Remind me to stay away <strong>${esc(c.frequency)}</strong>` :
    "No contact reminders";
  el.innerHTML = `
    <div class="tab-head">
      <div><h2>Lists &amp; policies</h2><p>Each list has one policy. Friends on it inherit it.</p></div>
      <button class="btn sun small" id="btn-add-cat">New list</button>
    </div>
    <div class="grid-2">
      ${state.categories.map((c) => `
        <div class="card cat-card">
          <h3>${esc(c.name)}
            <span class="chip ${c.policy_type === "avoid" ? "avoid" : ""}">${esc(c.policy_type)}</span>
            <span class="chip quiet">${counts[c.id] || 0} people</span>
          </h3>
          <p class="policy-line">${policyLabel(c)}${c.birthday_alert ? " &middot; birthday alerts on" : ""}.</p>
          ${c.message ? `<p class="muted" style="font-size:0.9rem;">&ldquo;${esc(c.message)}&rdquo;</p>` : ""}
          <div class="btn-row">
            <button class="btn small" data-edit-cat="${c.id}">Edit policy</button>
            <button class="btn ghost small" data-del-cat="${c.id}">Delete</button>
          </div>
        </div>`).join("")}
    </div>
    ${state.categories.length === 0 ? `<div class="card">${emptyState("No lists yet. Create one, like Close friends.")}</div>` : ""}`;
  $("#btn-add-cat").addEventListener("click", () => categoryModal(null));
  $$("[data-edit-cat]", el).forEach((b) => b.addEventListener("click", () => categoryModal(Number(b.dataset.editCat))));
  $$("[data-del-cat]", el).forEach((b) => b.addEventListener("click", async () => {
    const c = state.categories.find((x) => x.id === Number(b.dataset.delCat));
    if (!confirm(`Delete list "${c.name}"? Its friends keep no list.`)) return;
    await api(`/api/categories/${c.id}`, { method: "DELETE" });
    const [cats, friends] = await Promise.all([api("/api/categories"), api("/api/friends")]);
    state.categories = cats.categories;
    state.friends = friends.friends;
    TAB_LOADERS.categories();
    toast("List deleted.");
  }));
};

function categoryModal(cid) {
  const c = cid ? state.categories.find((x) => x.id === cid) : null;
  openModal(`
    <h2>${c ? "Edit policy" : "New list"}</h2>
    <form id="form-cat">
      <label>List name<input name="name" required value="${esc(c?.name || "")}" placeholder="Close friends"></label>
      <div class="form-row">
        <label>Policy
          <select name="policy_type">
            <option value="contact" ${c?.policy_type === "contact" ? "selected" : ""}>Remind me to contact them</option>
            <option value="avoid" ${c?.policy_type === "avoid" ? "selected" : ""}>Remind me to stay away</option>
            <option value="none" ${!c || c.policy_type === "none" ? "selected" : ""}>No reminders</option>
          </select>
        </label>
        <label>How often
          <select name="frequency">
            ${["daily", "weekly", "biweekly", "monthly"].map((f) =>
              `<option ${c?.frequency === f ? "selected" : ""}>${f}</option>`).join("")}
          </select>
        </label>
      </div>
      <label>Reminder text (use {name}, {birthday}, {phone})
        <input name="message" value="${esc(c?.message || "")}" placeholder="Call {name} this week"></label>
      <label style="display:flex; gap:8px; align-items:center;">
        <input type="checkbox" name="birthday_alert" style="width:auto;" ${!c || c.birthday_alert ? "checked" : ""}>
        Birthday alerts for this list
      </label>
      <div class="btn-row" style="margin-top:14px;">
        <button class="btn primary" type="submit">Save</button>
        <button class="btn ghost" type="button" id="btn-cancel">Cancel</button>
      </div>
    </form>`);
  $("#btn-cancel").addEventListener("click", closeModal);
  $("#form-cat").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const body = {
      name: fd.get("name"), policy_type: fd.get("policy_type"),
      frequency: fd.get("frequency"), message: fd.get("message"),
      birthday_alert: fd.get("birthday_alert") === "on",
    };
    try {
      if (c) await api(`/api/categories/${c.id}`, { method: "PUT", body });
      else await api("/api/categories", { method: "POST", body });
      closeModal();
      const { categories } = await api("/api/categories");
      state.categories = categories;
      TAB_LOADERS.categories();
      toast("List saved.");
    } catch (e) { toast(e.message, "error"); }
  });
}

/* ---------------- imports ---------------- */
TAB_LOADERS.imports = function renderImports() {
  const el = $("#tab-imports");
  const card = (kind, title, desc, accept, sample) => `
    <div class="card">
      <h3>${title}</h3>
      <p class="muted" style="font-size:0.9rem;">${desc}</p>
      <input type="file" id="file-${kind}" accept="${accept}">
      <div class="btn-row" style="margin-top:10px;">
        <button class="btn small" data-import="${kind}">Import</button>
        <a class="btn ghost small" style="text-decoration:none;" href="/samples/${sample}" download>Sample file</a>
      </div>
      <div class="sub" id="result-${kind}" style="margin-top:8px; font-size:0.9rem;"></div>
    </div>`;
  el.innerHTML = `
    <div class="tab-head">
      <div><h2>Imports</h2><p>Birthdays from a calendar, people from contacts, numbers from a list. Name matches land in Merge review.</p></div>
    </div>
    <div class="grid-2">
      ${card("birthdays", "Birthday calendar", "An .ics file with birthday events. Names become friends with birthdays.", ".ics", "birthdays.ics")}
      ${card("contacts", "Contacts", "A .vcf/vCard export or a .csv with name, phone, email, birthday columns.", ".vcf,.vcard,.csv", "contacts.vcf")}
      ${card("phones", "Phone list", "A .csv or plain-text list: one <i>name, phone</i> pair per line.", ".csv,.txt", "phones.csv")}
    </div>`;
  $$("[data-import]", el).forEach((b) => b.addEventListener("click", async () => {
    const kind = b.dataset.import;
    const input = $(`#file-${kind}`);
    const out = $(`#result-${kind}`);
    if (!input.files.length) { out.textContent = "Pick a file first."; return; }
    const file = input.files[0];
    const content = await file.text();
    out.textContent = "Importing...";
    try {
      const res = await api(`/api/import/${kind}`, {
        method: "POST", body: { content, filename: file.name },
      });
      out.textContent = `Imported ${res.imported}. ${res.candidates.length} possible duplicate(s) sent to Merge review.`;
      const [friends, merge] = await Promise.all([api("/api/friends"), api("/api/merge-candidates")]);
      state.friends = friends.friends;
      state.candidates = merge.candidates;
      updateBadges();
      toast(`Imported ${res.imported} from ${file.name}.`);
      input.value = "";
    } catch (e) { out.textContent = e.message; }
  }));
};

/* ---------------- merge review ---------------- */
TAB_LOADERS.merge = async function renderMerge() {
  const el = $("#tab-merge");
  try {
    const { candidates } = await api("/api/merge-candidates");
    state.candidates = candidates;
    updateBadges();
  } catch (e) { el.innerHTML = `<div class="card">Could not load: ${esc(e.message)}</div>`; return; }
  el.innerHTML = `
    <div class="tab-head">
      <div><h2>Merge review</h2><p>${state.candidates.length} suggestion(s). Confirm only when both entries are the same person — birthday and phone join into one.</p></div>
    </div>
    ${state.candidates.length === 0 ? `<div class="card">${emptyState("No duplicates waiting. Import a calendar and contacts with matching names to see suggestions here.")}</div>` : ""}
    ${state.candidates.map((c) => `
      <div class="card">
        <p style="margin-top:0;"><strong>Same person?</strong> <span class="muted">${esc(c.reason)}</span></p>
        <div class="merge-cols">
          ${mergeCol("Entry A", c.friend_a)}${mergeCol("Entry B", c.friend_b)}
        </div>
        <div class="btn-row">
          <button class="btn sun small" data-merge="${c.id}">Yes, merge into one</button>
          <button class="btn ghost small" data-keep="${c.id}">No, keep separate</button>
        </div>
      </div>`).join("")}`;
  $$("[data-merge]", el).forEach((b) => b.addEventListener("click", () => resolveMerge(Number(b.dataset.merge), true)));
  $$("[data-keep]", el).forEach((b) => b.addEventListener("click", () => resolveMerge(Number(b.dataset.keep), false)));
};

function mergeCol(title, f) {
  if (!f) return `<div class="merge-col"><h4>${title}</h4><p class="muted">Entry was removed.</p></div>`;
  const row = (k, v) => `<dt>${k}</dt><dd>${esc(v || "-")}</dd>`;
  const cat = f.category_id ? catById(f.category_id) : null;
  return `<div class="merge-col"><h4>${title} &middot; via ${esc(f.source || "manual")}</h4>
    <dl>${row("Name", friendName(f))}${row("Birthday", f.birthday)}${row("Phone", f.phone)}
    ${row("Email", f.email)}${row("List", cat ? cat.name : "-")}${row("Notes", f.notes)}</dl></div>`;
}

async function resolveMerge(cid, confirm) {
  const res = await api(`/api/merge-candidates/${cid}/resolve`, { method: "POST", body: { confirm } });
  const [friends, merge] = await Promise.all([api("/api/friends"), api("/api/merge-candidates")]);
  state.friends = friends.friends;
  state.candidates = merge.candidates;
  updateBadges();
  TAB_LOADERS.merge();
  toast(res.status === "merged" ? "Merged into one friend." : "Kept as separate friends.");
}

/* ---------------- admin ---------------- */
TAB_LOADERS.admin = async function renderAdmin() {
  const el = $("#tab-admin");
  if (!state.user.is_admin) { el.innerHTML = ""; return; }
  let users = [];
  try {
    ({ users } = await api("/api/admin/users"));
  } catch (e) { el.innerHTML = `<div class="card">Could not load users: ${esc(e.message)}</div>`; return; }
  el.innerHTML = `
    <div class="tab-head">
      <div><h2>Admin</h2><p>Registration is closed — only you can add people. Hand each newcomer their temporary password.</p></div>
    </div>
    <div class="card">
      <h3>People (${users.length})</h3>
      <table class="users">
        <tr><th>Username</th><th>Role</th><th>Password</th><th>Joined</th><th></th></tr>
        ${users.map((u) => `
          <tr>
            <td><strong>${esc(u.username)}</strong></td>
            <td>${u.is_admin ? "admin" : "user"}</td>
            <td>${u.must_change_password ? "temporary - must change" : "set by user"}</td>
            <td>${esc((u.created_at || "").slice(0, 10))}</td>
            <td style="white-space:nowrap;">
              <button class="btn small" data-reset="${u.id}">New temp password</button>
              <button class="btn ghost small" data-del-user="${u.id}">Remove</button>
            </td>
          </tr>`).join("")}
      </table>
      <h3 style="margin-top:22px;">Add someone</h3>
      <form id="form-add-user" class="inline-form">
        <label>Username<input name="username" required minlength="2" placeholder="mia"></label>
        <label>Temporary password<input name="temp" required minlength="4" placeholder="sunny-4821"></label>
        <label style="display:flex; gap:6px; align-items:center;"><input type="checkbox" name="admin" style="width:auto;"> admin</label>
        <button class="btn primary" type="submit">Add user</button>
      </form>
    </div>`;
  $("#form-add-user").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    try {
      await api("/api/admin/users", {
        method: "POST",
        body: { username: fd.get("username"), tempPassword: fd.get("temp"), isAdmin: fd.get("admin") === "on" },
      });
      toast("User added. Share the temporary password.");
      TAB_LOADERS.admin();
    } catch (e) { toast(e.message, "error"); }
  });
  $$("[data-reset]", el).forEach((b) => b.addEventListener("click", () => resetModal(Number(b.dataset.reset), users)));
  $$("[data-del-user]", el).forEach((b) => b.addEventListener("click", async () => {
    const u = users.find((x) => x.id === Number(b.dataset.delUser));
    if (!confirm(`Remove ${u.username}? Their friends, lists and reminders go too.`)) return;
    try {
      await api(`/api/admin/users/${u.id}`, { method: "DELETE" });
      toast("User removed.");
      TAB_LOADERS.admin();
    } catch (e) { toast(e.message, "error"); }
  }));
};

function resetModal(uid, users) {
  const u = users.find((x) => x.id === uid);
  openModal(`
    <h2>New temporary password</h2>
    <p class="muted">${esc(u.username)} will pick their own password at the next sign-in.</p>
    <form id="form-reset">
      <label>Temporary password<input name="temp" required minlength="4" placeholder="sunny-9042"></label>
      <div class="btn-row" style="margin-top:14px;">
        <button class="btn primary" type="submit">Assign</button>
        <button class="btn ghost" type="button" id="btn-cancel">Cancel</button>
      </div>
    </form>`);
  $("#btn-cancel").addEventListener("click", closeModal);
  $("#form-reset").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const temp = new FormData(ev.target).get("temp");
    try {
      await api(`/api/admin/users/${uid}/reset-password`, { method: "POST", body: { tempPassword: temp } });
      closeModal();
      toast("Temporary password assigned.");
      TAB_LOADERS.admin();
    } catch (e) { toast(e.message, "error"); }
  });
}

/* ---------------- init ---------------- */
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  });
}
boot();


