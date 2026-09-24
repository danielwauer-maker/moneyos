document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-category-search]").forEach((input) => {
    const select = document.getElementById(input.dataset.categorySearch);
    if (!select) return;
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
});
