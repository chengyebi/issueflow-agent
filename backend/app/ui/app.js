"use strict";

const REVIEW_ADMIN_HEADER_NAME = "X-Review-Admin-Token";
const REVIEW_ADMIN_TOKEN_STORAGE_KEY = "issueflow.reviewAdminToken";
const REVIEW_STATUSES = ["pending", "approved", "rejected"];


class ReviewAuthenticationError extends Error {}


class ReviewAuthenticationConfigurationError extends Error {}

const STATUS_LABELS = {
  pending: "待审核",
  approved: "已批准",
  rejected: "已拒绝",
  proposed: "待批准",
  executing: "执行中",
  executed: "已执行",
  failed: "执行失败",
};

const CATEGORY_LABELS = {
  bug: "Bug",
  feature: "功能建议",
  question: "问题咨询",
  documentation: "文档",
  other: "其他",
};

const PRIORITY_LABELS = {
  low: "低",
  medium: "中",
  high: "高",
  critical: "紧急",
};

const RISK_LABELS = {
  low: "低风险",
  medium: "中风险",
  high: "高风险",
};

const state = {
  activeStatus: "pending",
  reviewsByStatus: {
    pending: [],
    approved: [],
    rejected: [],
  },
  selectedReviewTaskId: null,
  loading: false,
  deciding: false,
  authenticating: false,
  reviewAdminToken: "",
};

const refreshButton = document.querySelector("#refresh-button");
const lastUpdated = document.querySelector("#last-updated");
const filterTabs = document.querySelectorAll(".filter-tab");
const reviewList = document.querySelector("#review-list");
const detailPanel = document.querySelector("#detail-panel");
const systemStatus = document.querySelector("#system-status");
const systemStatusLabel = document.querySelector("#system-status-label");
const notification = document.querySelector("#notification");
const lockButton = document.querySelector("#lock-button");
const unlockDialog = document.querySelector("#unlock-dialog");
const unlockForm = document.querySelector("#unlock-form");
const unlockTokenInput = document.querySelector("#review-admin-token");
const unlockButton = document.querySelector("#unlock-button");
const unlockError = document.querySelector("#unlock-error");

const metricElements = {
  pending: document.querySelector("#pending-count"),
  approved: document.querySelector("#approved-count"),
  rejected: document.querySelector("#rejected-count"),
  failed: document.querySelector("#failed-count"),
};

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value
    : {};
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function createElement(tagName, options = {}) {
  const element = document.createElement(tagName);

  if (options.className) {
    element.className = options.className;
  }

  if (options.text !== undefined && options.text !== null) {
    element.textContent = String(options.text);
  }

  return element;
}

function formatCurrentTime() {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date());
}

function formatDateTime(value) {
  if (!value) {
    return "—";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }

  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatScore(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "—";
}

function formatConfidence(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${Math.round(number * 100)}%` : "—";
}

function truncateText(value, maxLength = 120) {
  const text = String(value || "").trim();
  if (!text) {
    return "暂无内容";
  }

  return text.length > maxLength
    ? `${text.slice(0, maxLength).trim()}…`
    : text;
}

function setSystemState(label, variant = "ready") {
  systemStatusLabel.textContent = label;
  systemStatus.classList.toggle("is-error", variant === "error");
  systemStatus.classList.toggle("is-loading", variant === "loading");
}

function showNotification(message, variant = "error") {
  notification.textContent = message;
  notification.className = `notification is-${variant}`;
  notification.hidden = false;
}

function hideNotification() {
  notification.hidden = true;
  notification.textContent = "";
  notification.className = "notification";
}

function readStoredReviewAdminToken() {
  try {
    return window.sessionStorage.getItem(REVIEW_ADMIN_TOKEN_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}


function storeReviewAdminToken(token) {
  state.reviewAdminToken = token;

  try {
    window.sessionStorage.setItem(REVIEW_ADMIN_TOKEN_STORAGE_KEY, token);
    return true;
  } catch {
    return false;
  }
}


function clearReviewAdminToken() {
  state.reviewAdminToken = "";

  try {
    window.sessionStorage.removeItem(REVIEW_ADMIN_TOKEN_STORAGE_KEY);
  } catch {
    // 当前页面的内存 Token 已经清除；存储不可用时不扩大错误。
  }
}


function getReviewAdminToken() {
  if (state.reviewAdminToken) {
    return state.reviewAdminToken;
  }

  const storedToken = readStoredReviewAdminToken();

  if (storedToken) {
    state.reviewAdminToken = storedToken;
  }

  return storedToken;
}


function resetReviewState() {
  REVIEW_STATUSES.forEach((status) => {
    state.reviewsByStatus[status] = [];
  });

  state.activeStatus = "pending";
  state.selectedReviewTaskId = null;

  metricElements.pending.textContent = "—";
  metricElements.approved.textContent = "—";
  metricElements.rejected.textContent = "—";
  metricElements.failed.textContent = "—";

  updateActiveTab();
  renderDetailEmpty(
    "审核台已锁定",
    "输入管理员 Token 后才能查看 Issue、Agent 判断和待执行命令。",
  );

  reviewList.setAttribute("aria-busy", "false");
  reviewList.replaceChildren();

  const locked = createElement("div", {
    className: "empty-state compact locked-state",
  });
  locked.append(
    createElement("div", {
      className: "empty-icon",
      text: "◇",
    }),
    createElement("h3", {
      text: "审核数据已锁定",
    }),
    createElement("p", {
      text: "请先完成管理员身份验证。",
    }),
  );

  reviewList.append(locked);
}


function setUnlockError(message = "") {
  unlockError.textContent = message;
  unlockError.hidden = !message;
}


function showUnlockDialog(message = "") {
  setUnlockError(message);
  lockButton.hidden = true;
  setSystemState(
    message ? "需要重新解锁" : "审核台已锁定",
    message ? "error" : "ready",
  );

  if (!unlockDialog.open) {
    unlockDialog.showModal();
  }

  window.setTimeout(() => {
    unlockTokenInput.focus();
  }, 0);
}


function closeUnlockDialog() {
  setUnlockError("");
  unlockTokenInput.value = "";

  if (unlockDialog.open) {
    unlockDialog.close();
  }

  lockButton.hidden = false;
}


function lockReviewConsole(message = "") {
  clearReviewAdminToken();
  resetReviewState();
  hideNotification();
  showUnlockDialog(message);
}


function reviewRequestHeaders(extraHeaders = {}) {
  const token = getReviewAdminToken();

  return {
    Accept: "application/json",
    ...(token ? { [REVIEW_ADMIN_HEADER_NAME]: token } : {}),
    ...extraHeaders,
  };
}

function extractApiErrorDetail(body) {
  const detail = body?.detail;

  if (typeof detail === "string") {
    return detail;
  }

  if (!Array.isArray(detail)) {
    return "";
  }

  return detail
    .map((item) => {
      if (!item || typeof item.msg !== "string") {
        return null;
      }

      const location = Array.isArray(item.loc)
        ? item.loc.slice(1).join(".")
        : "";

      return location ? `${location}：${item.msg}` : item.msg;
    })
    .filter(Boolean)
    .join("；");
}

function buildIssueUrl(repo, issueNumber) {
  const repositoryName = String(repo || "").trim();
  const parts = repositoryName.split("/");
  const number = Number(issueNumber);

  if (
    parts.length !== 2 ||
    !parts[0] ||
    !parts[1] ||
    parts[0].toLowerCase() === "local" ||
    !Number.isInteger(number) ||
    number <= 0
  ) {
    return null;
  }

  const owner = encodeURIComponent(parts[0]);
  const repository = encodeURIComponent(parts[1]);
  const encodedNumber = encodeURIComponent(String(number));

  return `https://github.com/${owner}/${repository}/issues/${encodedNumber}`;
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status || "未知";
}

function getActiveReviews() {
  return state.reviewsByStatus[state.activeStatus] || [];
}

function findSelectedReview() {
  return getActiveReviews().find(
    (review) => review.review_task_id === state.selectedReviewTaskId,
  );
}

function createBadge(text, variant = "neutral") {
  return createElement("span", {
    className: `badge badge-${variant}`,
    text,
  });
}

function createDefinitionItem(label, value) {
  const item = createElement("div", { className: "definition-item" });
  item.append(
    createElement("dt", { text: label }),
    createElement("dd", { text: value ?? "—" }),
  );
  return item;
}

function createSection(title, content, className = "") {
  const section = createElement("section", {
    className: `detail-section ${className}`.trim(),
  });
  section.append(createElement("h3", { text: title }), content);
  return section;
}

function createTextBlock(value, fallback = "暂无内容") {
  return createElement("p", {
    className: "content-block",
    text: String(value || "").trim() || fallback,
  });
}

function createEmptyBlock(message) {
  return createElement("p", {
    className: "inline-empty",
    text: message,
  });
}

async function requestReviewTasks(status, tokenOverride = null) {
  const headers =
    tokenOverride === null
      ? reviewRequestHeaders()
      : {
          Accept: "application/json",
          [REVIEW_ADMIN_HEADER_NAME]: tokenOverride,
        };

  const response = await fetch(
    `/review-tasks?status=${encodeURIComponent(status)}`,
    {
      method: "GET",
      headers,
      cache: "no-store",
    },
  );

  let data = null;

  try {
    data = await response.json();
  } catch {
    data = null;
  }

  if (!response.ok) {
    const detail = extractApiErrorDetail(data);
    const suffix = detail ? `：${detail}` : "";

    if (response.status === 401) {
      throw new ReviewAuthenticationError(
        `管理员 Token 无效或已失效${suffix}`,
      );
    }

    if (response.status === 503) {
      throw new ReviewAuthenticationConfigurationError(
        `服务端尚未配置审核管理员 Token${suffix}`,
      );
    }

    throw new Error(
      `读取${statusLabel(status)}任务失败（HTTP ${response.status}）${suffix}`,
    );
  }

  if (!data || !Array.isArray(data.items)) {
    throw new Error(`读取${statusLabel(status)}任务失败：响应格式不正确`);
  }

  return data.items;
}

async function requestReviewDecision(
  reviewTaskId,
  decision,
  reviewer,
  reviewNote,
) {
  let endpoint;

  if (decision === "approved") {
    endpoint = `/review-tasks/${encodeURIComponent(
      String(reviewTaskId),
    )}/approve`;
  } else if (decision === "rejected") {
    endpoint = `/review-tasks/${encodeURIComponent(
      String(reviewTaskId),
    )}/reject`;
  } else {
    throw new Error("不支持的审核决策");
  }

  const response = await fetch(endpoint, {
    method: "POST",
    headers: reviewRequestHeaders({
      "Content-Type": "application/json",
    }),
    body: JSON.stringify({
      reviewer,
      review_note: reviewNote || null,
    }),
  });

  let data = null;

  try {
    data = await response.json();
  } catch {
    data = null;
  }

  if (!response.ok) {
    const detail = extractApiErrorDetail(data);
    const suffix = detail ? `：${detail}` : "";

    if (response.status === 401) {
      throw new ReviewAuthenticationError(
        `管理员 Token 无效或已失效${suffix}`,
      );
    }

    if (response.status === 503) {
      throw new ReviewAuthenticationConfigurationError(
        `服务端尚未配置审核管理员 Token${suffix}`,
      );
    }

    throw new Error(
      `提交${statusLabel(decision)}决策失败（HTTP ${response.status}）${suffix}`,
    );
  }

  if (
    !data ||
    !data.review_task ||
    data.review_task.status !== decision
  ) {
    throw new Error(`提交${statusLabel(decision)}决策失败：响应格式不正确`);
  }

  return data;
}

function decisionSuccessMessage(decision, result) {
  if (decision === "rejected") {
    return "审核任务已拒绝，仍处于待批准状态的命令已取消。";
  }

  const updatedCommandIds = asArray(result.updated_command_ids);

  if (result.rq_job_id) {
    return `审核任务已批准，命令任务已入队（RQ Job：${result.rq_job_id}）。`;
  }

  if (result.recovery_pending) {
    return "审核任务已批准，但即时派发未完成，系统已保留恢复记录。";
  }

  if (updatedCommandIds.length === 0) {
    return "审核任务已批准；该任务没有需要派发的 GitHub 命令。";
  }

  return "审核任务已批准，命令状态已经更新。";
}

function renderMetrics() {
  metricElements.pending.textContent = String(
    state.reviewsByStatus.pending.length,
  );
  metricElements.approved.textContent = String(
    state.reviewsByStatus.approved.length,
  );
  metricElements.rejected.textContent = String(
    state.reviewsByStatus.rejected.length,
  );

  const allReviews = REVIEW_STATUSES.flatMap(
    (status) => state.reviewsByStatus[status],
  );
  const failedCommands = allReviews.reduce((count, review) => {
    return (
      count +
      asArray(review.commands).filter((command) => command.status === "failed")
        .length
    );
  }, 0);

  metricElements.failed.textContent = String(failedCommands);
}

function renderLoadingList() {
  reviewList.setAttribute("aria-busy", "true");
  reviewList.replaceChildren();

  const loading = createElement("div", { className: "loading-state" });
  loading.append(
    createElement("span", { className: "loading-spinner" }),
    createElement("p", { text: "正在读取审核任务……" }),
  );
  reviewList.append(loading);
}

function renderListError(message) {
  reviewList.setAttribute("aria-busy", "false");
  reviewList.replaceChildren();

  const error = createElement("div", {
    className: "empty-state compact error-state",
  });
  error.append(
    createElement("div", {
      className: "empty-icon",
      text: "!",
    }),
    createElement("h3", { text: "无法读取审核任务" }),
    createElement("p", { text: message }),
  );

  reviewList.append(error);
}

function renderReviewList() {
  reviewList.setAttribute("aria-busy", "false");
  reviewList.replaceChildren();

  const reviews = getActiveReviews();

  if (reviews.length === 0) {
    const empty = createElement("div", {
      className: "empty-state compact",
    });
    empty.append(
      createElement("div", {
        className: "empty-icon",
        text: "◎",
      }),
      createElement("h3", {
        text: `暂无${statusLabel(state.activeStatus)}任务`,
      }),
      createElement("p", {
        text: "切换其他状态或稍后刷新。",
      }),
    );
    reviewList.append(empty);
    return;
  }

  const fragment = document.createDocumentFragment();

  reviews.forEach((review) => {
    const result = asObject(review.result_json);
    const commands = asArray(review.commands);
    const button = createElement("button", {
      className: "review-item",
    });

    button.type = "button";
    button.dataset.reviewTaskId = String(review.review_task_id);
    button.classList.toggle(
      "is-selected",
      review.review_task_id === state.selectedReviewTaskId,
    );

    const top = createElement("div", { className: "review-item-top" });
    top.append(
      createElement("span", {
        className: "review-reference",
        text: `${review.repo || "未知仓库"} #${review.issue_number ?? "—"}`,
      }),
      createBadge(
        statusLabel(review.review_status),
        review.review_status || "neutral",
      ),
      renderReasonCodeBadge(review.reason_code),
    );

    const title = createElement("h3", {
      text: review.issue_title || "无标题 Issue",
    });

    const summaryText =
      review.human_task ||
      review.reason ||
      result.summary ||
      review.issue_body;
    const summary = createElement("p", {
      className: "review-summary",
      text: truncateText(summaryText),
    });

    const meta = createElement("div", { className: "review-item-meta" });
    meta.append(
      createElement("span", {
        text: `${commands.length} 条命令`,
      }),
      createElement("span", {
        text: formatDateTime(review.created_at),
      }),
    );

    button.append(top, title, summary, meta);

    button.addEventListener("click", () => {
      state.selectedReviewTaskId = review.review_task_id;
      renderReviewList();
      renderSelectedReview();
    });

    fragment.append(button);
  });

  reviewList.append(fragment);
}

function renderDetailEmpty(title, message) {
  detailPanel.classList.remove("has-content");
  detailPanel.replaceChildren();

  const empty = createElement("div", { className: "empty-state" });
  empty.append(
    createElement("div", {
      className: "empty-icon large",
      text: "◇",
    }),
    createElement("p", {
      className: "panel-kicker",
      text: "Review details",
    }),
    createElement("h2", { text: title }),
    createElement("p", { text: message }),
  );

  detailPanel.append(empty);
}

function renderReasonCodeBadge(reasonCode) {
  if (!reasonCode) {
    return createBadge("无原因码", "neutral");
  }
  const labels = {
    security_risk: "安全风险",
    retrieval_degraded: "检索降级",
    duplicate_uncertain: "疑似重复",
    conflicting_evidence: "证据冲突",
    insufficient_evidence: "证据不足",
    low_calibrated_confidence: "置信度不足",
    unsupported_action: "不支持的动作",
    policy_blocked: "策略拦截",
    out_of_distribution: "分布外",
    model_failure: "模型失败",
  };
  return createBadge(labels[reasonCode] || reasonCode, "warning");
}

function renderHandoffBlock(review) {
  const reason = review.reason || "";
  const humanTask = review.human_task || "";
  const reasonCode = review.reason_code || "";
  const evidence = asArray(review.evidence);
  const alreadyChecked = asArray(review.already_checked);

  if (!reason && !humanTask) {
    return createSection(
      "需要人工的原因",
      createTextBlock("这条任务在人工审核列表中，但没有额外的接管说明。", "无"),
    );
  }

  const container = createElement("div", {
    className: "handoff-block",
  });

  const reasonItem = createElement("div", {
    className: "handoff-item",
  });
  reasonItem.append(
    createElement("strong", { text: "需要人工的原因" }),
    createTextBlock(reason, "无"),
  );

  const taskItem = createElement("div", {
    className: "handoff-item",
  });
  taskItem.append(
    createElement("strong", { text: "你只需要判断" }),
    createTextBlock(humanTask, "无"),
  );

  container.append(reasonItem, taskItem);

  if (alreadyChecked.length > 0) {
    const checkedList = createElement("ul", { className: "check-list" });
    alreadyChecked.forEach((item) => {
      const li = createElement("li", { text: `✔ ${item}` });
      checkedList.append(li);
    });
    container.append(
      createElement("strong", { text: "Agent 已完成" }),
      checkedList,
    );
  }

  if (evidence.length > 0) {
    const evidenceList = createElement("ul", { className: "evidence-list" });
    evidence.forEach((item) => {
      const li = createElement("li", { text: item });
      evidenceList.append(li);
    });
    container.append(
      createElement("strong", { text: "证据" }),
      evidenceList,
    );
  }

  return createSection("为什么需要我", container);
}

function renderMissingFields(fields) {
  if (fields.length === 0) {
    return createEmptyBlock("Agent 未发现缺失的复现信息。");
  }

  const list = createElement("ul", { className: "tag-list" });
  fields.forEach((field) => {
    const item = createElement("li", {
      className: "tag-item",
      text: field,
    });
    list.append(item);
  });
  return list;
}

function renderDuplicateAssessment(assessmentValue) {
  const assessment = asObject(assessmentValue);

  if (Object.keys(assessment).length === 0) {
    return createEmptyBlock("没有重复判断数据。");
  }

  const container = createElement("div", {
    className: "duplicate-assessment",
  });

  const headline = createElement("div", {
    className: "duplicate-headline",
  });
  headline.append(
    createBadge(
      assessment.is_duplicate ? "可能重复" : "未判定重复",
      assessment.is_duplicate ? "warning" : "success",
    ),
    createElement("strong", {
      text: `置信度 ${formatConfidence(assessment.confidence)}`,
    }),
  );

  container.append(
    headline,
    createTextBlock(assessment.rationale, "没有提供判断理由。"),
  );

  if (assessment.candidate_issue_number) {
    container.append(
      createElement("p", {
        className: "candidate-reference",
        text: `候选 Issue：#${assessment.candidate_issue_number}`,
      }),
    );
  }

  const evidence = asArray(assessment.evidence);
  if (evidence.length > 0) {
    const list = createElement("ul", { className: "evidence-list" });
    evidence.forEach((item) => {
      list.append(createElement("li", { text: item }));
    });
    container.append(list);
  }

  return container;
}

function renderSimilarIssues(issues, fallbackRepo) {
  if (issues.length === 0) {
    return createEmptyBlock("本次没有返回相似 Issue。");
  }

  const list = createElement("div", { className: "similar-list" });

  issues.forEach((issue) => {
    const card = createElement("article", {
      className: "similar-card",
    });
    const heading = createElement("div", {
      className: "similar-card-heading",
    });

    const url = buildIssueUrl(
      issue.repo || fallbackRepo,
      issue.issue_number,
    );
    const link = createElement("a", {
      text: `#${issue.issue_number ?? "—"} ${issue.title || "无标题"}`,
    });

    if (url) {
      link.href = url;
      link.target = "_blank";
      link.rel = "noreferrer";
    }

    heading.append(
      link,
      createBadge(issue.state || "unknown"),
    );

    const scores = createElement("div", {
      className: "similar-scores",
    });
    scores.append(
      createElement("span", {
        text: `RRF ${formatScore(issue.rrf_score, 4)}`,
      }),
      createElement("span", {
        text: `Lexical ${formatScore(issue.lexical_score, 4)}`,
      }),
      createElement("span", {
        text: `Vector ${formatScore(issue.vector_score, 4)}`,
      }),
    );

    card.append(
      heading,
      createTextBlock(issue.evidence, "没有检索证据。"),
      scores,
    );
    list.append(card);
  });

  return list;
}

function commandTitle(commandType) {
  if (commandType === "add_label") {
    return "添加标签";
  }
  if (commandType === "post_comment") {
    return "发布评论";
  }
  return commandType || "未知命令";
}

function renderCommands(commands) {
  if (commands.length === 0) {
    return createEmptyBlock("该审核任务没有可执行命令。");
  }

  const list = createElement("div", {
    className: "command-list",
  });

  commands.forEach((command) => {
    const payload = asObject(command.payload);
    const card = createElement("article", {
      className: "command-card",
    });
    const header = createElement("div", {
      className: "command-card-header",
    });

    header.append(
      createElement("strong", {
        text: commandTitle(command.command_type),
      }),
      createBadge(
        statusLabel(command.status),
        command.status || "neutral",
      ),
    );

    card.append(
      header,
      createTextBlock(payload.value, "命令内容为空。"),
    );

    if (command.error_message) {
      card.append(
        createElement("p", {
          className: "command-error",
          text: `${command.error_type || "Error"}：${command.error_message}`,
        }),
      );
    }

    list.append(card);
  });

  return list;
}

function setDecisionFormBusy(form, busy, decision = null) {
  form.querySelectorAll("input, textarea, button").forEach((control) => {
    control.disabled = busy;
  });

  const approveButton = form.querySelector('[data-decision="approved"]');
  const rejectButton = form.querySelector('[data-decision="rejected"]');

  if (approveButton) {
    approveButton.textContent =
      busy && decision === "approved"
        ? "正在批准…"
        : approveButton.dataset.defaultLabel;
  }

  if (rejectButton) {
    rejectButton.textContent =
      busy && decision === "rejected"
        ? "正在拒绝…"
        : rejectButton.dataset.defaultLabel;
  }
}

async function handleReviewDecision(
  review,
  decision,
  form,
  reviewerInput,
  noteInput,
) {
  if (state.deciding) {
    return;
  }

  const reviewer = reviewerInput.value.trim();

  if (!reviewer) {
    reviewerInput.setCustomValidity("请输入审核人");
    reviewerInput.reportValidity();
    reviewerInput.focus();
    return;
  }

  reviewerInput.setCustomValidity("");

  const reviewNote = noteInput.value.trim();
  const commandCount = asArray(review.commands).filter(
    (command) => command.status === "proposed",
  ).length;

  const confirmation =
    decision === "approved"
      ? commandCount > 0
        ? `确认批准审核任务 #${review.review_task_id}？这会批准 ${commandCount} 条命令并尝试派发到任务队列。`
        : `确认批准审核任务 #${review.review_task_id}？该任务没有待派发命令。`
      : `确认拒绝审核任务 #${review.review_task_id}？这会取消所有仍处于待批准状态的命令。`;

  if (!window.confirm(confirmation)) {
    return;
  }

  state.deciding = true;
  setDecisionFormBusy(form, true, decision);
  refreshButton.disabled = true;
  filterTabs.forEach((tab) => {
    tab.disabled = true;
  });

  setSystemState(
    decision === "approved" ? "正在批准" : "正在拒绝",
    "loading",
  );
  hideNotification();

  try {
    const result = await requestReviewDecision(
      review.review_task_id,
      decision,
      reviewer,
      reviewNote,
    );

    state.activeStatus = decision;
    state.selectedReviewTaskId = review.review_task_id;
    updateActiveTab();

    const refreshed = await loadReviews();
    const message = decisionSuccessMessage(decision, result);

    showNotification(
      refreshed
        ? message
        : `${message} 但列表刷新失败，请手动刷新。`,
      refreshed ? "success" : "warning",
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "提交审核决策时发生未知错误";

    if (
      error instanceof ReviewAuthenticationError ||
      error instanceof ReviewAuthenticationConfigurationError
    ) {
      lockReviewConsole(message);
      return;
    }

    showNotification(message, "error");
    setSystemState("操作失败", "error");
  } finally {
    state.deciding = false;

    filterTabs.forEach((tab) => {
      tab.disabled = false;
    });

    if (!state.loading) {
      refreshButton.disabled = false;
    }

    if (form.isConnected) {
      setDecisionFormBusy(form, false);
    }
  }
}

function renderDecisionForm(review) {
  const form = createElement("form", {
    className: "review-decision-form",
  });
  form.noValidate = true;

  const introduction = createElement("p", {
    className: "decision-introduction",
    text: "请确认模型建议和待执行命令。审核人必须填写，审核备注可以为空。",
  });

  const safetyNotice = createElement("p", {
    className: "decision-safety",
    text: "批准后，命令只会被交给后台任务队列；真正的 GitHub 写回仍由独立 Command Worker 执行。",
  });

  const reviewerId = `reviewer-${review.review_task_id}`;
  const noteId = `review-note-${review.review_task_id}`;

  const reviewerField = createElement("div", {
    className: "decision-field",
  });
  const reviewerLabel = createElement("label", {
    text: "审核人",
  });
  reviewerLabel.htmlFor = reviewerId;

  const reviewerInput = createElement("input", {
    className: "decision-input",
  });
  reviewerInput.id = reviewerId;
  reviewerInput.name = "reviewer";
  reviewerInput.type = "text";
  reviewerInput.required = true;
  reviewerInput.maxLength = 200;
  reviewerInput.autocomplete = "name";
  reviewerInput.placeholder = "例如：chengyebi";

  const reviewerHint = createElement("small", {
    className: "decision-hint",
    text: "必填，最多 200 个字符。",
  });

  reviewerField.append(reviewerLabel, reviewerInput, reviewerHint);

  const noteField = createElement("div", {
    className: "decision-field",
  });
  const noteLabel = createElement("label", {
    text: "审核备注",
  });
  noteLabel.htmlFor = noteId;

  const noteInput = createElement("textarea", {
    className: "decision-textarea",
  });
  noteInput.id = noteId;
  noteInput.name = "review_note";
  noteInput.maxLength = 4000;
  noteInput.rows = 4;
  noteInput.placeholder = "记录批准或拒绝的原因，选填。";

  const noteHint = createElement("small", {
    className: "decision-hint",
    text: "选填，最多 4000 个字符。",
  });

  noteField.append(noteLabel, noteInput, noteHint);

  const actions = createElement("div", {
    className: "decision-actions",
  });

  const rejectButton = createElement("button", {
    className: "decision-button decision-button-reject",
    text: "拒绝",
  });
  rejectButton.type = "button";
  rejectButton.dataset.decision = "rejected";
  rejectButton.dataset.defaultLabel = "拒绝";

  const proposedCommandCount = asArray(review.commands).filter(
    (command) => command.status === "proposed",
  ).length;
  const approveLabel =
    proposedCommandCount > 0 ? "批准并派发" : "批准审核";

  const approveButton = createElement("button", {
    className: "decision-button decision-button-approve",
    text: approveLabel,
  });
  approveButton.type = "button";
  approveButton.dataset.decision = "approved";
  approveButton.dataset.defaultLabel = approveLabel;

  rejectButton.addEventListener("click", () => {
    handleReviewDecision(
      review,
      "rejected",
      form,
      reviewerInput,
      noteInput,
    );
  });

  approveButton.addEventListener("click", () => {
    handleReviewDecision(
      review,
      "approved",
      form,
      reviewerInput,
      noteInput,
    );
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
  });

  actions.append(rejectButton, approveButton);
  form.append(
    introduction,
    safetyNotice,
    reviewerField,
    noteField,
    actions,
  );

  return form;
}

function renderSelectedReview() {
  const review = findSelectedReview();

  if (!review) {
    renderDetailEmpty(
      "选择一条任务查看 Agent 提议",
      "当前状态下没有已选择的审核任务。",
    );
    return;
  }

  const result = asObject(review.result_json);
  const duplicateAssessment = asObject(result.duplicate_assessment);
  const similarIssues = asArray(result.similar_issues);
  const commands = asArray(review.commands);

  detailPanel.classList.add("has-content");
  detailPanel.replaceChildren();

  const content = createElement("div", {
    className: "detail-content",
  });

  const header = createElement("header", {
    className: "detail-header",
  });
  const headingGroup = createElement("div");

  const reference = createElement("p", {
    className: "detail-reference",
    text: `${review.repo || "未知仓库"} #${review.issue_number ?? "—"}`,
  });

  const issueUrl = buildIssueUrl(review.repo, review.issue_number);
  const title = createElement("a", {
    className: "detail-title",
    text: review.issue_title || "无标题 Issue",
  });

  if (issueUrl) {
    title.href = issueUrl;
    title.target = "_blank";
    title.rel = "noreferrer";
  }

  headingGroup.append(reference, title);

  const badges = createElement("div", {
    className: "detail-badges",
  });
  badges.append(
    createBadge(
      statusLabel(review.review_status),
      review.review_status || "neutral",
    ),
    createBadge(
      RISK_LABELS[result.risk_level] || result.risk_level || "风险未知",
      result.risk_level === "high" ? "danger" : result.risk_level || "neutral",
    ),
  );

  header.append(headingGroup, badges);

  const facts = createElement("dl", {
    className: "definition-grid",
  });
  facts.append(
    createDefinitionItem(
      "分类",
      CATEGORY_LABELS[result.category] || result.category || "—",
    ),
    createDefinitionItem(
      "优先级",
      PRIORITY_LABELS[result.priority] || result.priority || "—",
    ),
    createDefinitionItem("Agent 置信度", formatConfidence(result.confidence)),
    createDefinitionItem("检索模式", result.retrieval_mode || "—"),
    createDefinitionItem(
      "检索降级",
      result.retrieval_degraded ? "是" : "否",
    ),
    createDefinitionItem("创建时间", formatDateTime(review.created_at)),
  );

  const handoffSection = renderHandoffBlock(review);

  const issueSection = createSection(
    "原始 Issue",
    createTextBlock(review.issue_body, "Issue 正文为空。"),
  );

  const summarySection = createSection(
    "Agent 摘要",
    createTextBlock(result.summary, "Agent 未生成摘要。"),
  );

  const missingSection = createSection(
    "缺失的复现信息",
    renderMissingFields(asArray(result.missing_repro_fields)),
  );

  const duplicateSection = createSection(
    "重复判断",
    renderDuplicateAssessment(duplicateAssessment),
  );

  const similarSection = createSection(
    "相似 Issue",
    renderSimilarIssues(similarIssues, review.repo),
  );

  const replySection = createSection(
    "建议回复",
    createTextBlock(result.suggested_reply, "Agent 未生成建议回复。"),
  );

  const commandsSection = createSection(
    "GitHub 命令草案",
    renderCommands(commands),
  );

  if (review.review_status !== "pending") {
    const reviewFacts = createElement("dl", {
      className: "definition-grid compact-grid",
    });
    reviewFacts.append(
      createDefinitionItem("审核人", review.reviewer || "—"),
      createDefinitionItem("审核时间", formatDateTime(review.reviewed_at)),
      createDefinitionItem("审核备注", review.review_note || "—"),
    );
    content.append(header, facts);
    content.append(
      createSection("审核结果", reviewFacts),
      handoffSection,
      issueSection,
      summarySection,
      missingSection,
      duplicateSection,
      similarSection,
      replySection,
      commandsSection,
    );
  } else {
    const decisionSection = createSection(
      "人工审核",
      renderDecisionForm(review),
      "decision-section",
    );

    content.append(
      header,
      facts,
      handoffSection,
      issueSection,
      summarySection,
      missingSection,
      duplicateSection,
      similarSection,
      replySection,
      commandsSection,
      decisionSection,
    );
  }

  detailPanel.append(content);
}

function updateActiveTab() {
  filterTabs.forEach((tab) => {
    const isActive = tab.dataset.status === state.activeStatus;
    tab.classList.toggle("is-active", isActive);
    tab.setAttribute("aria-pressed", String(isActive));
  });
}

function selectFirstActiveReview() {
  const reviews = getActiveReviews();
  const selectedStillExists = reviews.some(
    (review) => review.review_task_id === state.selectedReviewTaskId,
  );

  if (!selectedStillExists) {
    state.selectedReviewTaskId =
      reviews.length > 0 ? reviews[0].review_task_id : null;
  }
}

async function loadReviews() {
  if (!getReviewAdminToken()) {
    resetReviewState();
    showUnlockDialog();
    return false;
  }

  if (state.loading) {
    return false;
  }

  state.loading = true;
  refreshButton.disabled = true;
  refreshButton.textContent = "刷新中…";
  setSystemState("正在同步", "loading");
  hideNotification();
  renderLoadingList();

  try {
    const results = await Promise.all(
      REVIEW_STATUSES.map(async (status) => {
        const items = await requestReviewTasks(status);
        return [status, items];
      }),
    );

    results.forEach(([status, items]) => {
      state.reviewsByStatus[status] = items;
    });

    selectFirstActiveReview();
    renderMetrics();
    renderReviewList();
    renderSelectedReview();

    lastUpdated.textContent = `数据更新于 ${formatCurrentTime()}`;
    setSystemState("数据已连接", "ready");

    return true;
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "读取审核任务时发生未知错误";

    if (
      error instanceof ReviewAuthenticationError ||
      error instanceof ReviewAuthenticationConfigurationError
    ) {
      lockReviewConsole(message);
      return false;
    }

    renderListError(message);
    renderDetailEmpty(
      "审核数据暂时不可用",
      "请检查后端、PostgreSQL 和接口状态，然后重新刷新。",
    );
    showNotification(message, "error");
    setSystemState("连接失败", "error");
    return false;
  } finally {
    state.loading = false;
    refreshButton.disabled = false;
    refreshButton.textContent = "刷新状态";
  }
}

function setUnlockFormBusy(busy) {
  state.authenticating = busy;
  unlockTokenInput.disabled = busy;
  unlockButton.disabled = busy;
  unlockButton.textContent = busy ? "正在验证…" : "解锁审核台";
}


async function unlockReviewConsole(token) {
  if (state.authenticating) {
    return;
  }

  const candidateToken = token.trim();

  if (!candidateToken) {
    unlockTokenInput.setCustomValidity("请输入管理员 Token");
    unlockTokenInput.reportValidity();
    unlockTokenInput.focus();
    return;
  }

  unlockTokenInput.setCustomValidity("");
  setUnlockError("");
  setUnlockFormBusy(true);
  setSystemState("正在验证", "loading");

  try {
    await requestReviewTasks("pending", candidateToken);

    const persisted = storeReviewAdminToken(candidateToken);
    closeUnlockDialog();

    const loaded = await loadReviews();

    if (!loaded) {
      return;
    }

    showNotification(
      persisted
        ? "审核台已解锁。"
        : "审核台已解锁，但浏览器会话存储不可用；刷新页面后需要重新输入 Token。",
      persisted ? "success" : "warning",
    );
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "验证管理员 Token 时发生未知错误";

    clearReviewAdminToken();
    resetReviewState();
    setUnlockError(message);
    setSystemState(
      error instanceof ReviewAuthenticationConfigurationError
        ? "服务未配置"
        : "解锁失败",
      "error",
    );

    if (!unlockDialog.open) {
      unlockDialog.showModal();
    }

    unlockTokenInput.select();
  } finally {
    setUnlockFormBusy(false);
  }
}


unlockDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
});


unlockForm.addEventListener("submit", (event) => {
  event.preventDefault();
  unlockReviewConsole(unlockTokenInput.value);
});


lockButton.addEventListener("click", () => {
  if (!window.confirm("确认锁定审核台并清除当前页面会话中的管理员 Token？")) {
    return;
  }

  lockReviewConsole();
});


refreshButton.addEventListener("click", loadReviews);

filterTabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    const status = tab.dataset.status;

    if (!REVIEW_STATUSES.includes(status)) {
      return;
    }

    state.activeStatus = status;
    state.selectedReviewTaskId = null;
    updateActiveTab();
    selectFirstActiveReview();
    renderReviewList();
    renderSelectedReview();
  });
});

updateActiveTab();
resetReviewState();

const initialReviewAdminToken = readStoredReviewAdminToken();

if (initialReviewAdminToken) {
  state.reviewAdminToken = initialReviewAdminToken;
  lockButton.hidden = false;
  loadReviews();
} else {
  showUnlockDialog();
}
