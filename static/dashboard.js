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
      storage.set(key, JSON.stringify({ list: list.scrollTop, page: window.scrollY }));
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
  if (!response.ok) throw new Error(result.message || "저장하지 못했어요. 다시 시도해 주세요.");
  return result;
}

if (detail) {
  const id = detail.dataset.jobId;
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
