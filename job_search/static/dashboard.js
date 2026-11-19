"use strict";

const list = document.querySelector("#job-list");
const detail = document.querySelector("#job-detail");
const token = document.querySelector('meta[name="csrf-token"]')?.content;
const storage = {
  get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* Browser storage can be disabled. */ } },
  remove(key) { try { sessionStorage.removeItem(key); } catch { /* Nonessential feedback. */ } },
};

if (list) {
  const key = `job-list:${list.dataset.scrollKey}`;
  const saved = storage.get(key);
  if (saved) {
    try {
      const position = JSON.parse(saved);
      list.scrollTop = position.list || 0;
      if (!detail && matchMedia("(max-width: 640px)").matches) window.scrollTo(0, position.page || 0);
    } catch { storage.remove(key); }
  }
  window.addEventListener("pagehide", () => {
    if (getComputedStyle(list).display !== "none") {
      storage.set(`job-list:${list.dataset.scrollKey}`, JSON.stringify({ list: list.scrollTop, page: window.scrollY }));
    }
  });
}

async function post(url, data) {
  const response = await fetch(url, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
    body: JSON.stringify(data),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.message || "저장하지 못했어요. 다시 시도해 주세요.");
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
  const refresh = document.querySelector("#deadline-refresh");
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

  function applyScreen(html) {
    const updated = new DOMParser().parseFromString(html, "text/html");
    const freshDialog = updated.querySelector("#deadline-dialog");
    snapshot = JSON.parse(freshDialog.dataset.deadline);
    for (const selector of ["#deadline-auto-value", "#deadline-manual-value"]) {
      dialog.querySelector(selector).replaceChildren(...freshDialog.querySelector(selector).childNodes);
    }
    const freshBadge = updated.querySelector("#deadline-edit");
    edit.className = freshBadge.className;
    edit.setAttribute("aria-label", freshBadge.getAttribute("aria-label"));
    edit.replaceChildren(...freshBadge.childNodes);
    for (const selector of [".detail-meta", ".detail-actions", ".list-title > span", ".summary-strip"]) {
      document.querySelector(selector).replaceWith(updated.querySelector(selector));
    }
    const counts = updated.querySelectorAll(".nav-count");
    document.querySelectorAll(".nav-count").forEach((count, i) => { count.textContent = counts[i].textContent; });
    const freshList = updated.querySelector("#job-list");
    const scrollTop = list.scrollTop;
    list.replaceChildren(...freshList.childNodes);
    list.dataset.scrollKey = freshList.dataset.scrollKey;
    list.scrollTop = scrollTop;
    detail.querySelector(".back-link").href = updated.querySelector(".back-link").href;
    drawFields();
  }

  function showStatus(message, failed = false) {
    status.textContent = message;
    status.classList.toggle("error", failed);
  }

  edit.addEventListener("click", () => {
    if (pending || dialog.open) return;
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
      applyScreen(result.html);
      showStatus(result.message);
      dialog.close();
    } catch (failure) {
      if (failure.result?.error === 409) applyScreen(failure.result.html);
      error.textContent = failure instanceof TypeError ? "연결을 확인한 뒤 다시 시도해 주세요." : failure.message;
    } finally {
      pending = save.disabled = cancel.disabled = false;
    }
  });
  refresh?.addEventListener("click", async () => {
    if (pending) return;
    pending = true;
    refresh.setAttribute("aria-disabled", "true");
    showStatus("출처 확인 중…");
    try {
      const result = await post(`/jobs/${id}/deadline/refresh${location.search}`, {});
      applyScreen(result.html);
      showStatus(result.message + (snapshot.manual ? " 직접 입력값을 유지했어요." : ""));
    } catch (failure) {
      showStatus(failure instanceof TypeError ? "연결을 확인한 뒤 다시 시도해 주세요." : failure.message, true);
    } finally {
      pending = false;
      refresh.removeAttribute("aria-disabled");
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
