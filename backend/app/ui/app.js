"use strict";

const refreshButton = document.querySelector("#refresh-button");
const lastUpdated = document.querySelector("#last-updated");
const filterTabs = document.querySelectorAll(".filter-tab");

function formatCurrentTime() {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date());
}

function updateRefreshTime() {
  lastUpdated.textContent = `UI 状态刷新于 ${formatCurrentTime()}`;
}

refreshButton.addEventListener("click", updateRefreshTime);

filterTabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    filterTabs.forEach((item) => item.classList.remove("is-active"));
    tab.classList.add("is-active");
  });
});

updateRefreshTime();
