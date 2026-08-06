"use strict";

const REVIEW_STATUSES = ["pending", "approved", "rejected"];

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
};

const refreshButton = document.querySelector("#refresh-button");
const lastUpdated = document.querySelector("#last-updated");
const filterTabs = document.querySelectorAll(".filter-tab");
const reviewList = document.querySelector("#review-list");
const detailPanel = document.querySelector("#detail-panel");
const systemStatus = document.querySelector("#system-status");
const systemStatusLabel = document.querySelector("#system-status-label");
const notification = document.querySelector("#notification");

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

function buildIssueUrl(repo, issueNumber) {
  const parts = String(repo || "").split("/");
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    return null;
  }

  const owner = encodeURIComponent(parts[0]);
  const repository = encodeURIComponent(parts[1]);
  const number = encodeURIComponent(String(issueNumber));

  return `https://github.com/${owner}/${repository}/issues/${number}`;
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

async function requestReviewTasks(status) {
  const response = await fetch(
    `/review-tasks?status=${encodeURIComponent(status)}`,
    {
      method: "GET",
      headers: {
        Accept: "application/json",
      },
      cache: "no-store",
    },
  );

  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      detail = body.detail ? `：${body.detail}` : "";
    } catch {
      detail = "";
    }

    throw new Error(
      `读取${statusLabel(status)}任务失败（HTTP ${response.status}）${detail}`,
    );
  }

  const data = await response.json();

  if (!data || !Array.isArray(data.items)) {
    throw new Error(`读取${statusLabel(status)}任务失败：响应格式不正确`);
  }

  return data.items;
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
    );

    const title = createElement("h3", {
      text: review.issue_title || "无标题 Issue",
    });

    const summary = createElement("p", {
      className: "review-summary",
      text: truncateText(result.summary || review.issue_body),
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
      issueSection,
      summarySection,
      missingSection,
      duplicateSection,
      similarSection,
      replySection,
      commandsSection,
    );
  } else {
    content.append(
      header,
      facts,
      issueSection,
      summarySection,
      missingSection,
      duplicateSection,
      similarSection,
      replySection,
      commandsSection,
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
  if (state.loading) {
    return;
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
  } catch (error) {
    const message =
      error instanceof Error ? error.message : "读取审核任务时发生未知错误";

    renderListError(message);
    renderDetailEmpty(
      "审核数据暂时不可用",
      "请检查后端、PostgreSQL 和接口状态，然后重新刷新。",
    );
    showNotification(message);
    setSystemState("连接失败", "error");
  } finally {
    state.loading = false;
    refreshButton.disabled = false;
    refreshButton.textContent = "刷新状态";
  }
}

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
loadReviews();
