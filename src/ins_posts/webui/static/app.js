(() => {
  "use strict";

  const PAGE_SIZE = 50;
  const TERMINAL = new Set(["succeeded", "partial", "failed"]);
  const TOKEN_KEY = "ins-posts-ui-token";
  const state = {
    token: "",
    jobId: null,
    logSequence: 0,
    logs: [],
    page: 0,
    pollTimer: null,
    busy: false,
    jobMode: null,
  };

  const elements = {
    form: document.querySelector("#job-form"),
    fatal: document.querySelector("#fatal-message"),
    formError: document.querySelector("#form-error"),
    submit: document.querySelector("#submit-job"),
    submitLabel: document.querySelector("#submit-label"),
    modeRadios: [...document.querySelectorAll('input[name="mode"]')],
    randomPanel: document.querySelector("#random-panel"),
    targetPanel: document.querySelector("#target-panel"),
    target: document.querySelector("#target"),
    maxResults: document.querySelector("#max-results"),
    maxResultsField: document.querySelector("#target-max-results-field"),
    filterFinalGrid: document.querySelector("#filter-final-grid"),
    filterHelp: document.querySelector("#filter-help"),
    authentication: document.querySelector("#authentication"),
    statusTitle: document.querySelector("#status-title"),
    statusBadge: document.querySelector("#job-status"),
    phase: document.querySelector("#phase-text"),
    progress: document.querySelector("#progress-track"),
    jobMessage: document.querySelector("#job-message"),
    openOutput: document.querySelector("#open-output"),
    logBox: document.querySelector(".log-box"),
    log: document.querySelector("#job-log"),
    logCount: document.querySelector("#log-count"),
    scanned: document.querySelector("#metric-scanned"),
    matched: document.querySelector("#metric-matched"),
    added: document.querySelector("#metric-new"),
    stored: document.querySelector("#metric-stored"),
    resultsSection: document.querySelector("#results-section"),
    resultsTitle: document.querySelector("#results-title"),
    resultsCount: document.querySelector("#results-count"),
    resultsEmpty: document.querySelector("#results-empty"),
    resultsEmptyCopy: document.querySelector("#results-empty-copy"),
    resultsList: document.querySelector("#results-list"),
    previousPage: document.querySelector("#previous-page"),
    nextPage: document.querySelector("#next-page"),
    pageLabel: document.querySelector("#page-label"),
    navItems: [...document.querySelectorAll(".nav-item")],
  };

  function setText(element, value) {
    element.textContent = value == null ? "" : String(value);
  }

  function showMessage(element, message, error = false) {
    setText(element, message);
    element.classList.toggle("notice-error", error);
    element.hidden = !message;
  }

  function readStartupToken() {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const fragmentToken = params.get("token");
    if (fragmentToken) {
      window.sessionStorage.setItem(TOKEN_KEY, fragmentToken);
      window.history.replaceState(null, "", window.location.pathname);
      return fragmentToken;
    }
    return window.sessionStorage.getItem(TOKEN_KEY) || "";
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-UI-Token", state.token);
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    const response = await window.fetch(path, {
      ...options,
      headers,
      cache: "no-store",
      credentials: "omit",
    });
    const type = response.headers.get("Content-Type") || "";
    const payload = type.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      throw new Error(payload && payload.error ? payload.error : `请求失败（${response.status}）`);
    }
    return payload;
  }

  function browserLabel(name) {
    const labels = {
      brave: "Brave",
      chrome: "Google Chrome",
      chromium: "Chromium",
      edge: "Microsoft Edge",
      firefox: "Firefox（推荐）",
      floorp: "Floorp",
      librewolf: "LibreWolf",
      opera: "Opera",
      orion: "Orion",
      safari: "Safari",
      thorium: "Thorium",
      vivaldi: "Vivaldi",
      zen: "Zen Browser",
    };
    return labels[name] || name;
  }

  function populateBrowsers(browsers) {
    const current = elements.authentication.value;
    const anonymous = elements.authentication.firstElementChild;
    elements.authentication.replaceChildren(anonymous);
    for (const browser of browsers) {
      const option = document.createElement("option");
      option.value = browser;
      setText(option, browserLabel(browser));
      elements.authentication.append(option);
    }
    if ([...elements.authentication.options].some((option) => option.value === current)) {
      elements.authentication.value = current;
    } else if (browsers.includes("firefox")) {
      elements.authentication.value = "firefox";
    }
  }

  function commaValues(id) {
    const raw = document.querySelector(id).value;
    const result = [];
    const seen = new Set();
    for (const part of raw.replace(/\n/g, ",").split(",")) {
      const value = part.trim();
      const key = value.toLocaleLowerCase();
      if (value && !seen.has(key)) {
        seen.add(key);
        result.push(value);
      }
    }
    return result;
  }

  function optionalNumber(id) {
    const value = document.querySelector(id).value.trim();
    return value === "" ? null : Number(value);
  }

  function selectedMode() {
    const selected = elements.modeRadios.find((input) => input.checked);
    return selected ? selected.value : "random";
  }

  function setPanelDisabled(panel, disabled) {
    for (const control of panel.querySelectorAll("input, select, button")) {
      control.disabled = disabled;
    }
  }

  function setResultCopy(mode) {
    const random = mode === "random";
    setText(elements.resultsTitle, random ? "本次随机结果" : "命中的帖子");
    setText(
      elements.resultsEmptyCopy,
      random
        ? "可以选择更广的探索范围，或放宽日期、点赞、关键词等筛选条件。"
        : "可以扩大扫描上限，或放宽日期、点赞、关键词等筛选条件。",
    );
  }

  function applyMode() {
    const mode = selectedMode();
    const random = mode === "random";
    elements.randomPanel.hidden = !random;
    elements.targetPanel.hidden = random;
    setPanelDisabled(elements.randomPanel, !random);
    setPanelDisabled(elements.targetPanel, random);
    elements.target.required = !random;
    elements.maxResultsField.hidden = random;
    elements.maxResults.disabled = random;
    elements.filterFinalGrid.classList.toggle("single-column", random);
    setText(elements.submitLabel, random ? "随机采一批" : "开始采集");
    setText(
      elements.filterHelp,
      random
        ? "先从随机来源收集候选帖子，再按条件保留并打散结果。不同类别的条件之间按“并且”处理。"
        : "先扫描上方指定范围，再按条件保留结果。不同类别的条件之间按“并且”处理。",
    );
    if (!state.jobId) {
      setResultCopy(mode);
    }
  }

  function buildPayload() {
    const mode = selectedMode();
    const since = document.querySelector("#since").value;
    const until = document.querySelector("#until").value;
    if (since && until && since > until) {
      throw new Error("开始日期不能晚于结束日期。");
    }
    const minLikes = optionalNumber("#min-likes");
    const maxLikes = optionalNumber("#max-likes");
    if (minLikes !== null && maxLikes !== null && minLikes > maxLikes) {
      throw new Error("最低点赞数不能大于最高点赞数。");
    }
    const filters = {
      since: since || null,
      until: until || null,
      min_likes: minLikes,
      max_likes: maxLikes,
      keywords: commaValues("#keywords"),
      keyword_mode: document.querySelector("#keyword-mode").value,
      hashtags: commaValues("#hashtags"),
      hashtag_mode: document.querySelector("#hashtag-mode").value,
      media_type: document.querySelector("#media-type").value,
    };
    const payload = {
      mode,
      authentication: elements.authentication.value,
      download_media: document.querySelector("#download-media").checked,
      request_delay: document.querySelector("#request-delay").value.trim(),
      filters,
    };
    if (mode === "random") {
      payload.discovery = {
        breadth: document.querySelector("#discovery-breadth").value,
        result_count: Number(document.querySelector("#discovery-result-count").value),
      };
      return payload;
    }

    const includes = [...document.querySelectorAll('input[name="include"]:checked')]
      .map((input) => input.value);
    if (includes.length === 0) {
      throw new Error("至少选择帖子或 Reels 中的一种。");
    }
    payload.target = elements.target.value.trim();
    payload.include = includes;
    payload.max_posts = Number(document.querySelector("#max-posts").value);
    filters.max_results = optionalNumber("#max-results");
    return payload;
  }

  const statusLabels = {
    queued: "排队中",
    running: "运行中",
    succeeded: "已完成",
    partial: "部分完成",
    failed: "失败",
  };

  const phaseLabels = {
    waiting: "任务正在等待后台执行。",
    collecting: "正在访问 Instagram 并整理帖子元数据。",
    media: "元数据已保存，正在下载命中的媒体文件。",
    finished: "后台任务已经结束。",
  };

  function updateStatus(job) {
    const status = job.status || "queued";
    setText(elements.statusTitle, status === "running" ? "正在采集" : statusLabels[status] || "任务状态");
    setText(elements.statusBadge, statusLabels[status] || status);
    elements.statusBadge.className = `job-badge ${status}`;
    const phaseText = job.phase === "collecting" && state.jobMode === "random"
      ? "正在从随机公共来源发现并整理帖子。"
      : phaseLabels[job.phase] || "正在更新任务状态。";
    setText(elements.phase, phaseText);
    elements.progress.classList.toggle("active", status === "queued" || status === "running");
    elements.progress.classList.toggle("complete", status === "succeeded" || status === "partial");
    if (status === "failed") {
      elements.logBox.open = true;
    }

    const summary = job.summary || {};
    setText(elements.scanned, summary.scanned_this_run ?? summary.fetched_this_run ?? "—");
    setText(elements.matched, summary.matched_this_run ?? job.results.raw_total ?? "—");
    setText(elements.added, summary.new_posts ?? "—");
    setText(elements.stored, summary.stored_posts ?? "—");

    elements.openOutput.disabled = !state.jobId;
    elements.submit.disabled = !TERMINAL.has(status);
    state.busy = !TERMINAL.has(status);

    let message = "";
    let isError = false;
    if (job.error) {
      message = job.error;
      isError = true;
    } else if (summary.errors && summary.errors.length) {
      message = summary.errors.map((item) => item.message || item.error).join("；");
      isError = status === "failed";
    } else if (summary.media_download_skipped_reason) {
      message = `媒体下载已跳过：${summary.media_download_skipped_reason}`;
    } else if (status === "succeeded") {
      message = "采集完成，结果已经保存到本机。";
    } else if (status === "partial") {
      message = "元数据已保留，但任务存在部分错误，请查看日志。";
    }
    showMessage(elements.jobMessage, message, isError);
  }

  function updateLogs(job) {
    if (job.logs_truncated) {
      state.logs = [];
    }
    for (const entry of job.logs || []) {
      state.logSequence = Math.max(state.logSequence, Number(entry.sequence) || 0);
      const time = entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString() : "";
      state.logs.push(`[${time}] ${entry.message}`);
    }
    if (state.logs.length > 500) {
      state.logs = state.logs.slice(-500);
    }
    setText(elements.logCount, state.logs.length);
    setText(elements.log, state.logs.length ? state.logs.join("\n") : "尚无日志。");
    elements.log.scrollTop = elements.log.scrollHeight;
  }

  function safeInstagramUrl(value) {
    if (typeof value !== "string" || !value) {
      return null;
    }
    try {
      const parsed = new URL(value);
      const host = parsed.hostname.toLocaleLowerCase().replace(/\.$/, "");
      if (parsed.protocol !== "https:") {
        return null;
      }
      if (host !== "instagram.com" && !host.endsWith(".instagram.com")) {
        return null;
      }
      return parsed.href;
    } catch (_error) {
      return null;
    }
  }

  function formatDate(value) {
    if (!value) {
      return "日期未知";
    }
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {
      return String(value);
    }
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "short",
      day: "numeric",
    }).format(parsed);
  }

  function formatNumber(value) {
    if (value === null || value === undefined) {
      return "点赞未知";
    }
    return `${new Intl.NumberFormat("zh-CN").format(value)} 赞`;
  }

  function appendTextElement(parent, tag, className, text) {
    const element = document.createElement(tag);
    if (className) {
      element.className = className;
    }
    setText(element, text);
    parent.append(element);
    return element;
  }

  function resultCard(post) {
    const card = document.createElement("article");
    card.className = "result-card";

    const media = Array.isArray(post.media) ? post.media : [];
    const hasVideo = media.some((item) => item && item.media_type === "video");
    const mediaBlock = document.createElement("div");
    mediaBlock.className = hasVideo ? "media-block video" : "media-block";
    appendTextElement(mediaBlock, "strong", "", hasVideo ? "VIDEO" : "IMAGE");
    appendTextElement(mediaBlock, "span", "", String(post.media_count ?? media.length ?? 0));
    card.append(mediaBlock);

    const content = document.createElement("div");
    content.className = "result-content";
    const meta = document.createElement("div");
    meta.className = "result-meta";
    appendTextElement(meta, "span", "", formatDate(post.published_at));
    appendTextElement(meta, "span", "", formatNumber(post.like_count));
    if (post.location && typeof post.location === "object") {
      const location = post.location.slug || post.location.id;
      if (location) {
        appendTextElement(meta, "span", "", String(location));
      }
    }
    content.append(meta);

    const titleText = post.username ? `@${post.username}` : post.shortcode || "Instagram 帖子";
    const postUrl = safeInstagramUrl(post.post_url);
    if (postUrl) {
      const title = appendTextElement(content, "a", "result-title", titleText);
      title.href = postUrl;
      title.target = "_blank";
      title.rel = "noopener noreferrer";
    } else {
      appendTextElement(content, "span", "result-title", titleText);
    }
    appendTextElement(content, "p", "caption", post.caption || "无文案");

    const tags = document.createElement("div");
    tags.className = "tag-list";
    const values = Array.isArray(post.hashtags) ? post.hashtags.slice(0, 5) : [];
    for (const value of values) {
      appendTextElement(tags, "span", "tag", value);
    }
    content.append(tags);
    card.append(content);
    return card;
  }

  function renderResults(results, terminal) {
    const total = Number(results.total) || 0;
    const rawTotal = Number(results.raw_total) || 0;
    const offset = Number(results.offset) || 0;
    elements.resultsSection.hidden = !terminal && rawTotal === 0;
    setText(elements.resultsCount, `${total} 条命中结果`);
    elements.resultsList.replaceChildren();
    for (const post of results.items || []) {
      elements.resultsList.append(resultCard(post));
    }
    elements.resultsEmpty.hidden = total !== 0 || !terminal;
    const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
    const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
    setText(elements.pageLabel, `第 ${currentPage} / ${pageCount} 页`);
    elements.previousPage.disabled = state.page === 0;
    elements.nextPage.disabled = offset + PAGE_SIZE >= total;
  }

  async function pollJob() {
    if (!state.jobId) {
      return;
    }
    window.clearTimeout(state.pollTimer);
    try {
      const offset = state.page * PAGE_SIZE;
      const job = await api(
        `/api/jobs/${encodeURIComponent(state.jobId)}?after=${state.logSequence}&offset=${offset}&limit=${PAGE_SIZE}`,
      );
      updateStatus(job);
      updateLogs(job);
      renderResults(job.results, TERMINAL.has(job.status));
      if (!TERMINAL.has(job.status)) {
        state.pollTimer = window.setTimeout(pollJob, 1000);
      }
    } catch (error) {
      showMessage(elements.jobMessage, error.message, true);
      if (state.busy) {
        state.pollTimer = window.setTimeout(pollJob, 2500);
      }
    }
  }

  async function submitJob(event) {
    event.preventDefault();
    showMessage(elements.formError, "");
    if (!elements.form.reportValidity()) {
      return;
    }
    let payload;
    try {
      payload = buildPayload();
    } catch (error) {
      showMessage(elements.formError, error.message, true);
      return;
    }
    elements.submit.disabled = true;
    state.busy = true;
    state.jobMode = payload.mode;
    setResultCopy(state.jobMode);
    state.page = 0;
    state.logSequence = 0;
    state.logs = [];
    setText(elements.log, "正在创建任务……");
    try {
      const created = await api("/api/jobs", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.jobId = created.id;
      elements.openOutput.disabled = false;
      await pollJob();
    } catch (error) {
      state.busy = false;
      elements.submit.disabled = false;
      showMessage(elements.formError, error.message, true);
    }
  }

  async function openOutput() {
    if (!state.jobId) {
      return;
    }
    elements.openOutput.disabled = true;
    try {
      await api(`/api/jobs/${encodeURIComponent(state.jobId)}/open-output`, {
        method: "POST",
      });
    } catch (error) {
      showMessage(elements.jobMessage, error.message, true);
    } finally {
      elements.openOutput.disabled = false;
    }
  }

  function changePage(delta) {
    state.page = Math.max(0, state.page + delta);
    pollJob();
    elements.resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function activateNavigation(selected) {
    for (const item of elements.navItems) {
      const active = item === selected;
      item.classList.toggle("active", active);
      if (active) {
        item.setAttribute("aria-current", "page");
      } else {
        item.removeAttribute("aria-current");
      }
    }
  }

  async function initialize() {
    applyMode();
    state.token = readStartupToken();
    if (!state.token) {
      showMessage(elements.fatal, "缺少本次启动令牌。请关闭此标签页，并从启动程序重新打开界面。", true);
      elements.submit.disabled = true;
      return;
    }
    try {
      const health = await api("/api/health");
      populateBrowsers(Array.isArray(health.browsers) ? health.browsers : []);
    } catch (error) {
      showMessage(elements.fatal, `无法连接本地采集服务：${error.message}`, true);
      elements.submit.disabled = true;
    }
  }

  elements.form.addEventListener("submit", submitJob);
  for (const radio of elements.modeRadios) {
    radio.addEventListener("change", applyMode);
  }
  elements.openOutput.addEventListener("click", openOutput);
  elements.previousPage.addEventListener("click", () => changePage(-1));
  elements.nextPage.addEventListener("click", () => changePage(1));
  for (const item of elements.navItems) {
    item.addEventListener("click", () => activateNavigation(item));
  }
  window.addEventListener("beforeunload", () => window.clearTimeout(state.pollTimer));
  initialize();
})();
