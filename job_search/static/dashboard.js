"use strict";

const list = document.querySelector("#job-list");
const detail = document.querySelector("#job-detail");
const dashboard = document.querySelector("[data-dashboard]");
const token = document.querySelector('meta[name="csrf-token"]')?.content;
const storage = {
  get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* Browser storage can be disabled. */ } },
  remove(key) { try { sessionStorage.removeItem(key); } catch { /* Nonessential feedback. */ } },
};

function wireScroll() {
  const key = `job-list:${list.dataset.scrollKey}`;
  const navigation = performance.getEntriesByType("navigation")[0]?.type;
  const returning = navigation === "back_forward" || navigation === "reload" || storage.get("return-to-list") === key;
  const saved = (detail || returning) && storage.get(key);
  storage.remove("return-to-list");
  document.querySelector(".back-link")?.addEventListener("click", () => {
    storage.set("return-to-list", `job-list:${list.dataset.scrollKey}`);
  });
  if (saved) {
    try {
      const position = JSON.parse(saved);
      list.scrollTop = position.list || 0;
      if (!detail && matchMedia("(max-width: 640px)").matches) window.scrollTo(0, position.page || 0);
    } catch { storage.remove(key); }
  }
  // The return link's fragment can arrive before the shell has any job rows.
  if (!detail && dashboard?.dataset.view === "recommended" && /^#job--?\d+$/.test(location.hash) &&
      (document.activeElement === document.body || document.activeElement === list)) {
    (document.getElementById(location.hash.slice(1)) || list).focus();
  }
  window.addEventListener("pagehide", () => {
    if (getComputedStyle(list).display !== "none") {
      storage.set(`job-list:${list.dataset.scrollKey}`, JSON.stringify({ list: list.scrollTop, page: window.scrollY }));
    }
  });
}

async function post(url, data, requestId) {
  const response = await fetch(url, {
    method: "POST", credentials: "same-origin", keepalive: Boolean(requestId),
    ...(requestId ? {signal: refreshSignal()} : {}),
    headers: { "Content-Type": "application/json", "X-CSRF-Token": token,
               ...(requestId ? {"X-Refresh-Request": requestId} : {}) },
    body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.message || "저장하지 못했어요. 다시 시도해 주세요.");
    error.status = response.status;
    error.result = result;
    throw error;
  }
  return result;
}

function deadlinePreview(input, automatic, now = Date.now()) {
  if (input.kind === "unknown") return automatic.closed;
  if (input.kind === "rolling") return false;
  if (!input.date || (input.kind === "datetime" && !input.time)) return null;
  if (input.kind === "date") {
    const koreaDate = new Date(now + 9 * 60 * 60 * 1000).toISOString().slice(0, 10);
    return koreaDate > input.date;
  }
  return now >= Date.parse(`${input.date}T${input.time}:00+09:00`) + 60000;
}

function deadlineWarning(expired, next) {
  if (next === true && !expired) return "저장하면 이 공고가 다시 만료로 표시됩니다.";
  if (next === false && expired) return "저장하면 이 공고의 만료 표시가 해제됩니다.";
  return "";
}

function wireDeadline(dialog, id) {
  const edit = document.querySelector("#deadline-edit");
  const status = document.querySelector("#deadline-status");
  const form = dialog.querySelector("form");
  const fields = form.querySelector(".deadline-fields");
  const warning = form.querySelector("#deadline-warning");
  const error = form.querySelector("#deadline-error");
  const save = form.querySelector('button[type="submit"]');
  const cancel = form.querySelector("[data-deadline-cancel]");
  const controls = form.elements;
  let snapshot = JSON.parse(dialog.dataset.deadline);
  let pending = false;

  function drawFields() {
    const manual = controls.mode.value === "manual";
    fields.hidden = !manual;
    for (const label of fields.querySelectorAll("[data-deadline-kinds]")) {
      label.hidden = !label.dataset.deadlineKinds.split(" ").includes(controls.kind.value);
      label.querySelector("input, select").disabled = !manual || label.hidden;
    }
    controls.kind.disabled = !manual;
    const input = {kind: controls.kind.value, date: controls.date.value, time: controls.time.value};
    const next = manual ? deadlinePreview(input, snapshot.auto) : snapshot.auto.is_expired;
    warning.textContent = deadlineWarning(snapshot.expired, next);
  }

  function showStatus(message, failed = false) {
    status.textContent = message;
    status.classList.toggle("error", failed);
  }

  edit.addEventListener("click", () => {
    if (pending || dialog.open) return;
    snapshot = JSON.parse(dialog.dataset.deadline);
    const initial = snapshot.manual || snapshot.auto;
    controls.mode.value = snapshot.manual ? "manual" : "auto";
    for (const key of ["kind", "date", "time", "word"]) controls[key].value = initial[key];
    if (!controls.word.value) controls.word.value = "상시 채용";
    error.textContent = "";
    drawFields();
    dialog.showModal();
    edit.setAttribute("aria-expanded", "true");
    form.querySelector('input[name="mode"]:checked').focus();
  });
  form.addEventListener("input", drawFields);
  cancel.addEventListener("click", () => { if (!pending) dialog.close(); });
  dialog.addEventListener("cancel", event => { if (pending) event.preventDefault(); });
  dialog.addEventListener("close", () => {
    edit.setAttribute("aria-expanded", "false");
    edit.focus({preventScroll: true});
  });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (pending) return;
    const payload = Object.fromEntries(new FormData(form));
    if (payload.mode === "auto") payload.version = snapshot.auto.version;
    pending = save.disabled = cancel.disabled = true;
    error.textContent = "";
    try {
      const result = await post(`/jobs/${id}/deadline${location.search}`, payload);
      showStatus(result.message);
      dialog.close();
      await refreshScreen();
    } catch (failure) {
      if (failure.result?.error === 409) {
        await refreshScreen();
        snapshot = JSON.parse(dialog.dataset.deadline);
        drawFields();
      }
      error.textContent = failure instanceof TypeError ? "연결을 확인한 뒤 다시 시도해 주세요." : failure.message;
    } finally {
      pending = save.disabled = cancel.disabled = false;
    }
  });

}

if (detail) {
  const id = detail.dataset.jobId;
  const deadlineDialog = document.querySelector("#deadline-dialog");
  if (deadlineDialog) wireDeadline(deadlineDialog, id);
  const message = document.querySelector("#save-status");
  const buttons = [...detail.querySelectorAll("[data-status]")];
  const forms = [...detail.querySelectorAll(".progress-form")];
  const controls = [...detail.querySelectorAll("[data-status], .progress-form button, .progress-form select, .progress-form input, .progress-form textarea")];
  const noticeKey = `job-saved:${id}`;
  if (storage.get(noticeKey)) {
    message.textContent = storage.get(noticeKey) === "1" ? "지원 상태를 저장했어요." : storage.get(noticeKey);
    storage.remove(noticeKey);
  }
  if (detail.dataset.reviewedAt) {
    post(`/jobs/${id}/seen`, { reviewed_at: detail.dataset.reviewedAt }).catch(() => {
      message.textContent = "읽음 표시를 저장하지 못했어요. 새로고침하면 다시 시도해요.";
    });
  }
  let saving = false;
  async function save(path, payload, target, notice) {
      if (saving) return;
      saving = true;
      controls.forEach(element => { element.disabled = true; });
      target.classList.remove("error");
      target.textContent = "저장 중…";
      try {
        const result = await post(`/jobs/${id}/${path}`, payload);
        if (document.querySelector("#job-detail")?.dataset.jobId === id) {
          if (path === "progress") {
            const form = forms.find(element => element.dataset.stage === result.stage);
            const badge = form.querySelector(".stage-result");
            badge.textContent = form.querySelector("select").selectedOptions[0].textContent;
            badge.className = `stage-result ${result.result}`;
            form.querySelector(".stage-footer > span").textContent = "방금 저장";
            const preview = document.querySelector(".job-row.selected .progress-preview");
            if (preview) preview.textContent = result.progress_label;
            target.textContent = notice;
            if (dashboard) await refreshScreen();
          } else {
            storage.set(noticeKey, notice);
            location.reload();
          }
        }
      } catch (error) {
        target.textContent = error instanceof TypeError ? "연결을 확인한 뒤 다시 시도해 주세요. 저장 여부는 새로고침 후 확인할 수 있어요." : error.message;
        target.classList.add("error");
      } finally {
        saving = false;
        controls.forEach(element => { element.disabled = false; });
      }
  }
  for (const button of buttons) {
    button.addEventListener("click", () => save("state", { status: button.dataset.status }, message, "지원 상태를 저장했어요."));
  }
  for (const form of forms) {
    form.addEventListener("submit", event => {
      event.preventDefault();
      const payload = { ...Object.fromEntries(new FormData(form)), stage: form.dataset.stage };
      save("progress", payload, form.querySelector(".stage-save-status"), "전형 기록을 저장했어요.");
    });
  }
}

let screenVersion = 0;
let pageActive = true;
let scrollWired = false;
const runs = new Map();
let activity = null;
let needsSnapshot = false;
let pollTimer;
let pollDelay = 1500;
const lastProgress = new Map();

function refreshSignal() {
  return globalThis.AbortSignal?.timeout?.(15000);
}

async function readCurrentScreen(accept) {
  const version = ++screenVersion;
  try {
    const response = await fetch(location.href, {headers: {"X-Dashboard-Snapshot": "1"}, signal: refreshSignal()});
    if (!response.ok) throw new Error("화면을 불러오지 못했어요.");
    const html = await response.text();
    if (pageActive && version === screenVersion) accept(html);
  } catch (error) {
    if (pageActive && version === screenVersion) throw error;
  }
}

// Reconcile only server-owned markup. Editable fields and their event handlers stay outside this tree.
function patchReadOnly(current, fresh) {
  if (current.isEqualNode(fresh)) return;
  if (current.nodeType !== Node.ELEMENT_NODE) {
    current.nodeValue = fresh.nodeValue;
    return;
  }
  for (const name of current.getAttributeNames()) {
    if (!fresh.hasAttribute(name)) current.removeAttribute(name);
  }
  for (const {name, value} of fresh.attributes) {
    if (current.getAttribute(name) !== value) current.setAttribute(name, value);
  }
  const children = [...fresh.childNodes];
  children.forEach((child, i) => {
    const old = current.childNodes[i];
    if (!old) current.append(child);
    else if (old.nodeName !== child.nodeName) old.replaceWith(child);
    else patchReadOnly(old, child);
  });
  while (current.childNodes.length > children.length) current.lastChild.remove();
}

function syncList(next) {
  const rows = [...list.querySelectorAll(".job-row")];
  const key = node => node.dataset.jobId || (node.tagName === "H2" ? node.textContent : node.className);
  const existing = new Map([...list.children].map(node => [key(node), node]));
  const nextIds = new Set([...next.querySelectorAll(".job-row")].map(row => row.dataset.jobId));
  const mobile = matchMedia("(max-width: 640px)").matches;
  const top = Math.max(0, list.getBoundingClientRect().top);
  const anchor = rows.find(row => nextIds.has(row.dataset.jobId) && row.getBoundingClientRect().bottom > top);
  const offset = anchor?.getBoundingClientRect().top;
  const focused = document.activeElement;
  const focusedPage = list.contains(focused) && focused.getAttribute("aria-label");
  let position = list.firstElementChild;
  for (const incoming of [...next.children]) {
    const old = existing.get(key(incoming));
    const node = old || incoming;
    if (old) patchReadOnly(old, incoming);
    if (node !== position) list.insertBefore(node, position);
    position = node.nextElementSibling;
  }
  while (position) {
    const obsolete = position;
    position = position.nextElementSibling;
    obsolete.remove();
  }
  list.dataset.scrollKey = next.dataset.scrollKey;
  list.dataset.page = next.dataset.page;
  delete list.dataset.loadFailed;
  list.setAttribute("aria-busy", "false");
  if (list.contains(focused) && document.activeElement !== focused) focused.focus({preventScroll: true});
  if (!focused.isConnected && (focusedPage || rows.includes(focused) || focused.hasAttribute("data-list-retry"))) {
    const link = [...list.querySelectorAll(".pagination a")].find(node => node.getAttribute("aria-label") === focusedPage);
    (link || anchor || list).focus({preventScroll: true});
  }
  if (anchor && list.offsetHeight) {
    const shift = anchor.getBoundingClientRect().top - offset;
    if (mobile) window.scrollBy(0, shift);
    else list.scrollTop += shift;
  }
}

function applySnapshot(html) {
  const next = new DOMParser().parseFromString(html, "text/html");
  syncList(next.querySelector("#job-list"));
  const selectors = [".list-title > span", ".collection-time", ".nav-count", ".summary-strip strong", "[data-refresh-panels]"];
  if (detail) {
    selectors.push(".detail-company", "#job-detail > h2", ".detail-meta", ".detail-actions", "#detail-fit",
                   "#detail-evidence", "#deadline-auto-value", "#deadline-manual-value");
    next.querySelector(".jd-section").open = document.querySelector(".jd-section").open;
    document.querySelector(".back-link").href = next.querySelector(".back-link").href;
    const edit = document.querySelector("#deadline-edit"), badge = next.querySelector("#deadline-edit");
    badge.setAttribute("aria-expanded", edit.getAttribute("aria-expanded"));
    patchReadOnly(edit, badge);
    document.querySelector("#deadline-dialog").dataset.deadline = next.querySelector("#deadline-dialog").dataset.deadline;
  }
  for (const selector of selectors) {
    const fresh = next.querySelectorAll(selector);
    document.querySelectorAll(selector).forEach((current, i) => patchReadOnly(current, fresh[i]));
  }
  const address = new URL(location.href);
  if (list.dataset.page !== (address.searchParams.get("page") || "1")) {
    address.searchParams.set("page", list.dataset.page);
    history.replaceState(history.state, "", address);
  }
  if (!scrollWired) {
    wireScroll();
    scrollWired = true;
  }
  const baseline = next.querySelector("[data-dashboard]").dataset;
  runs.clear();
  for (const run of JSON.parse(baseline.runs)) runs.set(run.id, run);
  activity = JSON.parse(baseline.activity);
  needsSnapshot = false;
  pollDelay = 1500;
  refreshMessage("");
  showProgress();
}

function refreshMessage(message) {
  for (const status of document.querySelectorAll("[data-refresh-status]")) {
    if (status.textContent !== message) status.textContent = message;
  }
}

async function refreshScreen() {
  needsSnapshot = true;
  const retry = list?.querySelector("[data-list-retry]");
  if (retry) {
    list.setAttribute("aria-busy", "true");
    retry.setAttribute("aria-disabled", "true");
    if (!retry.hidden) list.querySelector("[data-list-load-status]").textContent = "추천 공고를 다시 불러오는 중…";
  }
  try {
    await readCurrentScreen(applySnapshot);
  } catch {
    if (pageActive) {
      const inlineError = retry?.isConnected;
      if (inlineError) {
        list.dataset.loadFailed = "";
        list.setAttribute("aria-busy", "false");
        list.querySelector("[data-list-load-status]").textContent = "추천 공고를 불러오지 못했어요. 연결을 확인해 주세요.";
        retry.hidden = false;
        retry.removeAttribute("aria-disabled");
      }
      retryRefresh(inlineError && !detail ? "" : "화면을 불러오지 못했어요. 연결을 다시 확인할게요.");
      schedulePoll(pollDelay);
    }
  }
}

function retryRefresh(message) {
  pollDelay = Math.min(30000, pollDelay * 2);
  refreshMessage(message);
}

function progressSummary(counts) {
  const finished = counts.done + counts.failed + counts.skipped;
  const total = finished + counts.running + counts.queued;
  let state = "완료";
  if (!total) state = "대상 없음";
  else if (counts.running) state = "처리 중";
  else if (counts.queued) state = "대기 중";
  else if (counts.failed) state = counts.failed === total ? "전체 실패" : "일부 실패";

  const parts = [];
  for (const [key, label] of [["running","처리 중"], ["queued","대기"], ["done","성공"], ["skipped","해당 없음"], ["failed","실패"]]) {
    if (counts[key]) parts.push(`${label} ${counts[key]}`);
  }
  return {state, total, finished, details: parts.join(" · ") || state};
}

function setText(element, value) {
  if (element.textContent !== value) element.textContent = value;
}

function setProgress(bar, summary) {
  bar.max = summary.total || 1;
  bar.value = summary.finished;
}

const taskStates = {queued: "대기 중", running: "처리 중", done: "성공", failed: "실패", skipped: "건너뜀"};

function showPostTasks(panel, tasks) {
  const counts = {queued: 0, running: 0, done: 0, failed: 0, skipped: 0};
  for (const row of panel.querySelectorAll("[data-task]")) {
    const task = tasks[row.dataset.task];
    if (!task) continue;
    counts[task.status]++;
    row.dataset.state = task.status;
    setText(row.querySelector("[data-task-status]"), taskStates[task.status]);
    setText(row.querySelector("[data-task-message]"), task.message ||
      (task.status === "failed" ? "세부 실패 사유가 기록되지 않았어요." : ""));
  }
  return counts;
}

function showProgress() {
  const now = Date.now();
  for (const panel of document.querySelectorAll("[data-refresh-panels] [data-run-id]")) {
    const run = runs.get(Number(panel.dataset.runId));
    if (!run) continue;
    const individual = panel.dataset.postResults !== undefined;
    if (individual && !run.post_tasks) continue;
    const counts = individual ? showPostTasks(panel, run.post_tasks) : run.counts;
    const summary = progressSummary(counts);
    const complete = summary.finished === summary.total;
    const progressKey = `${run.id}/${individual ? "post" : "list"}`;
    const signature = individual ? JSON.stringify(run.post_tasks) : [run.cursor, counts.running, counts.queued].join("/");
    if (lastProgress.get(progressKey)?.signature !== signature) lastProgress.set(progressKey, {signature, at: now});
    const minutes = Math.floor((now - lastProgress.get(progressKey).at) / 60000);
    const elapsed = !complete && minutes ? ` · ${minutes}분간 변동 없음` : "";
    setText(panel.querySelector("[data-run-state]"), summary.state);
    const pending = complete ? "" : ` · 실행 중 ${counts.running} · 대기 ${counts.queued}`;
    setText(panel.querySelector("[data-run-total]"), `작업 ${summary.finished}/${summary.total}개 처리${pending}${elapsed}`);
    setProgress(panel.querySelector("[data-run-bar]"), summary);
    panel.dataset.outcome = complete && counts.failed ? "failed" : complete ? "complete" : "active";
    for (const part of panel.querySelectorAll("[data-kind]")) {
      const counts = run.by_kind[part.dataset.kind];
      const value = progressSummary(counts);
      part.dataset.failed = String(counts.failed > 0);
      setText(part.querySelector("[data-kind-count]"), `${value.finished}/${value.total}`);
      setText(part.querySelector("[data-kind-status]"), value.details);
      setProgress(part.querySelector("progress"), value);
    }
  }
}

function submitRefresh(scope, postId, requestId) {
  const query = new URLSearchParams({view: scope.view, q: scope.query});
  return post(`/refreshes?${query}`, postId === undefined ? {} : {post_id: Number(postId)}, requestId);
}

function pendingSubmissions() {
  try { return JSON.parse(storage.get("refresh-submitting") || "[]"); } catch { return []; }
}

function rememberSubmission(request) {
  storage.set("refresh-submitting", JSON.stringify([...pendingSubmissions(), request]));
}

function forgetSubmission(id) {
  const remaining = pendingSubmissions().filter(item => item.id !== id);
  if (remaining.length) storage.set("refresh-submitting", JSON.stringify(remaining));
  else storage.remove("refresh-submitting");
}

async function recoverRefresh(request) {
  const response = await fetch(`/refreshes?request=${encodeURIComponent(request.id)}`, {signal: refreshSignal()});
  if (!response.ok) throw new Error();
  const found = await response.json();
  return found.runs[0] || submitRefresh(request.scope, request.postId, request.id);
}

const submitting = new Set();

function schedulePoll(delay) {
  clearTimeout(pollTimer);
  if (pageActive) pollTimer = setTimeout(poll, delay);
}

async function poll() {
  const version = screenVersion;
  try {
    if (needsSnapshot) {
      await refreshScreen();
    } else {
      const active = [...runs.values()].filter(run => !run.complete);
      if (activity?.active || active.length) {
        const read = async url => {
          const response = await fetch(url, {signal: refreshSignal()});
          if (!response.ok) throw new Error();
          return response.json();
        };
        const [latest, ...updates] = await Promise.all([
          activity?.active ? read("/refreshes/activity") : null,
          ...active.map(run => read(`/refreshes/${run.id}?after=${run.cursor}&view=progress` +
            (detail ? `&post_id=${encodeURIComponent(detail.dataset.jobId)}` : ""))),
        ]);
        if (!pageActive || version !== screenVersion) return;
        const changed = (latest && latest.cursor !== activity.cursor) || updates.some(run => run.changes.length > 0);
        if (latest) activity = latest;
        for (const run of updates) runs.set(run.id, run);
        pollDelay = 1500;
        refreshMessage("");
        showProgress();
        if (changed) await refreshScreen();
      }
    }
    for (const request of pendingSubmissions()) {
      if (submitting.has(request.id)) continue;
      try {
        await recoverRefresh(request);
      } catch (error) {
        if (!pageActive || ![400, 403, 404].includes(error.status)) throw error;
      }
      if (!pageActive) return;
      forgetSubmission(request.id);
      await refreshScreen();
    }
  } catch {
    if (pageActive) retryRefresh("진행 상태를 받아오지 못했어요. 마지막으로 확인한 결과를 표시하고 있어요.");
  } finally {
    if (needsSnapshot || activity?.active || [...runs.values()].some(run => !run.complete) || storage.get("refresh-submitting")) {
      schedulePoll(pollDelay);
    }
  }
}

if (dashboard) {
  document.querySelector("[data-list-retry]")?.addEventListener("click", () => {
    if (list.getAttribute("aria-busy") === "false") schedulePoll(0);
  });
  for (const button of document.querySelectorAll("[data-refresh]")) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      const request = {
        id: crypto.randomUUID(),
        scope: {view: dashboard.dataset.view, query: dashboard.dataset.query},
        postId: button.dataset.refresh === "detail" ? Number(detail.dataset.jobId) : undefined,
      };
      rememberSubmission(request);
      submitting.add(request.id);
      refreshMessage("새로고침을 요청하고 있어요.");
      try {
        const run = await submitRefresh(request.scope, request.postId, request.id);
        if (!pageActive) return;
        forgetSubmission(request.id);
        runs.set(run.id, run);
        await refreshScreen();
      } catch (error) {
        if (!pageActive) return;
        if ([400, 403, 404].includes(error.status)) {
          forgetSubmission(request.id);
          refreshMessage(error.message);
        } else {
          retryRefresh("서버 응답이 없어 요청 접수 여부를 다시 확인하고 있어요.");
        }
      } finally {
        submitting.delete(request.id);
        button.disabled = false;
        if (pageActive) schedulePoll(pollDelay);
      }
    });
  }
  window.addEventListener("pagehide", () => {
    pageActive = false;
    screenVersion++;
    clearTimeout(pollTimer);
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted) {
      pageActive = true;
      needsSnapshot = true;
      schedulePoll(0);
    }
  });
  needsSnapshot = true;
  schedulePoll(0);
} else if (list) {
  wireScroll();
}
