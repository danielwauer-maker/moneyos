function setupCategorySearch(root = document) {
  root.querySelectorAll("[data-category-search]").forEach((input) => {
    if (input.dataset.categorySearchReady === "true") return;
    const select = document.getElementById(input.dataset.categorySearch);
    if (!select) return;
    input.dataset.categorySearchReady = "true";
    input.addEventListener("input", () => {
      const needle = input.value.trim().toLocaleLowerCase("de");
      Array.from(select.options).forEach((option) => {
        if (!option.value) return;
        const searchable = option.dataset.categoryText || option.textContent.toLocaleLowerCase("de");
        option.hidden = Boolean(needle) && !searchable.includes(needle);
      });
      Array.from(select.querySelectorAll("optgroup")).forEach((group) => {
        group.hidden = Array.from(group.querySelectorAll("option")).every((option) => option.hidden);
      });
    });
  });
}

function sortReviewRows(body) {
  const descending = body.dataset.sort !== "oldest";
  const rows = Array.from(body.querySelectorAll("[data-review-row]"));
  rows.sort((left, right) => {
    const compared = left.dataset.sortKey.localeCompare(right.dataset.sortKey);
    return descending ? -compared : compared;
  });
  rows.forEach((row) => body.appendChild(row));
}

function updateReviewCounts(progress) {
  Object.entries(progress).forEach(([key, value]) => {
    const element = document.querySelector(`[data-review-progress="${key}"]`);
    if (element) element.textContent = value;
  });
  const openBody = document.getElementById("open-review-body");
  const reviewedBody = document.getElementById("reviewed-review-body");
  const openCount = document.querySelector("[data-open-row-count]");
  const reviewedCount = document.querySelector("[data-reviewed-row-count]");
  if (openCount && openBody) openCount.textContent = openBody.querySelectorAll("[data-review-row]").length;
  if (reviewedCount && reviewedBody) reviewedCount.textContent = reviewedBody.querySelectorAll("[data-review-row]").length;
}

function parseReviewRow(html) {
  const body = document.createElement("tbody");
  body.innerHTML = html.trim();
  return body.querySelector("[data-review-row]");
}

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-review-row-form]");
  if (!form) return;
  event.preventDefault();
  const currentRow = form.closest("[data-review-row]");
  const status = currentRow.querySelector(".row-save-status");
  const scrollPosition = window.scrollY;
  if (status) status.textContent = "Speichert …";
  try {
    const response = await fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-MoneyOS-Row-Update": "1", Accept: "application/json" },
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Speichern fehlgeschlagen");
    const targetBody = document.getElementById(
      result.fully_reviewed ? "reviewed-review-body" : "open-review-body",
    );
    currentRow.remove();
    targetBody.querySelector("[data-empty-row]")?.remove();
    if (result.html) {
      const replacement = parseReviewRow(result.html);
      targetBody.appendChild(replacement);
      sortReviewRows(targetBody);
      setupCategorySearch(replacement);
    }
    updateReviewCounts(result.progress);
    if (result.fully_reviewed) {
      const nextOpen = document.querySelector("#open-review-body [data-review-row]");
      if (nextOpen) {
        nextOpen.scrollIntoView({ block: "nearest", behavior: "smooth" });
        nextOpen.querySelector("select, button")?.focus({ preventScroll: true });
      } else {
        window.scrollTo({ top: scrollPosition });
      }
    } else {
      window.scrollTo({ top: scrollPosition });
    }
  } catch (error) {
    if (status) status.textContent = error.message;
  }
});

document.addEventListener("DOMContentLoaded", () => {
  setupCategorySearch();
});
