(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const state = { articles: [], view: "map" };
  const labels = { high: "高", medium: "中", low: "低", fast: "快速變動", evergreen: "長期適用", unknown: "未知" };

  function apiPath(value) {
    const url = new URL(value, location.origin);
    const native = url.protocol === "http:" && url.hostname === "127.0.0.1" && Boolean(url.port);
    if ((url.origin !== location.origin && !native) || !url.pathname.startsWith("/api/")) throw new Error("API 分頁位址不安全");
    return url.pathname + url.search;
  }
  async function pages(path) {
    let next = path;
    const seen = new Set();
    const rows = [];
    while (next) {
      const safe = apiPath(next);
      if (seen.has(safe) || seen.size >= 1000) throw new Error("API 分頁異常");
      seen.add(safe);
      const response = await fetch(safe, { credentials: "same-origin", headers: { Accept: "application/json; version=10" } });
      if (response.status === 401 || response.status === 403 || response.redirected && response.url.includes("/accounts/login")) {
        const error = new Error("請先登入 Paperless，並確認有權限查看文件。"); error.login = true; throw error;
      }
      if (!response.ok || !(response.headers.get("Content-Type") || "").includes("json")) throw new Error(`Paperless API 回應異常（${response.status}）`);
      const data = await response.json();
      if (Array.isArray(data)) { rows.push(...data); next = null; }
      else if (data && Array.isArray(data.results)) { rows.push(...data.results); next = data.next; }
      else throw new Error("Paperless API 資料格式不符");
    }
    return rows;
  }
  function fieldMap(doc) {
    const result = new Map();
    for (const entry of doc.custom_fields || []) if (entry && typeof entry.field === "number") result.set(entry.field, entry.value);
    return result;
  }
  function monthAge(dateString) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(dateString || "")) return null;
    const date = new Date(`${dateString}T00:00:00`);
    if (Number.isNaN(date.getTime()) || date > new Date()) return null;
    const now = new Date();
    let months = (now.getFullYear() - date.getFullYear()) * 12 + now.getMonth() - date.getMonth();
    if (now.getDate() < date.getDate()) months--;
    return months;
  }
  function freshness(article) {
    const shelf = String(article.shelf || "").toLowerCase();
    if (shelf === "evergreen") return { key: "evergreen", text: "長期適用 · 不標過期" };
    const age = monthAge(article.publishDate);
    if (age === null) return { key: "unknown", text: "日期不足" };
    const limit = shelf === "fast" ? 3 : shelf === "medium" ? 9 : null;
    if (limit === null) return { key: "unknown", text: "時效類型未知" };
    return age >= limit ? { key: "stale", text: `已過 ${limit} 個月提醒門檻` } : { key: "fresh", text: "未達提醒門檻" };
  }
  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }
  function safeSource(url) {
    try { const parsed = new URL(url); return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null; }
    catch { return null; }
  }
  function articleCard(article) {
    const button = node("button", "article", "");
    button.type = "button";
    const meta = node("div", "meta");
    meta.append(node("span", "category-name", article.category || "未分類"), node("span", "", article.publishDate || "日期未知"));
    button.append(meta, node("strong", "article-title", article.title || "未命名文章"),
      node("p", "summary", article.summary || "無摘要"));
    const foot = node("div", "foot");
    foot.append(node("span", "", article.account || "來源未知"), node("span", freshness(article).key, freshness(article).text));
    button.append(foot);
    button.addEventListener("click", () => showDetail(article));
    return button;
  }
  function visibleArticles() {
    const search = $("search").value.trim().toLocaleLowerCase();
    return state.articles.filter(article => {
      if ($( "category").value && article.category !== $("category").value) return false;
      if ($("freshness").value && freshness(article).key !== $("freshness").value) return false;
      if ($("actionable").checked && String(article.actionable).toLowerCase() !== "high") return false;
      return !search || [article.title, article.summary, article.account, article.category, article.source].join(" ").toLocaleLowerCase().includes(search);
    });
  }
  function render() {
    const articles = visibleArticles();
    $("count").textContent = `${articles.length} / ${state.articles.length} 篇`;
    const container = $("results"); container.replaceChildren();
    $("status").textContent = articles.length ? "" : state.articles.length ? "沒有符合篩選的文章。" : "尚無已匯入的文章。";
    if (state.view === "list") {
      container.className = "list";
      for (const article of articles) container.append(articleCard(article));
    } else {
      container.className = "map";
      const groups = new Map();
      for (const article of articles) {
        const category = article.category || "未分類";
        if (!groups.has(category)) groups.set(category, []);
        groups.get(category).push(article);
      }
      for (const [category, group] of [...groups].sort((a, b) => a[0].localeCompare(b[0], "zh-Hant"))) {
        const section = node("section", "topic");
        const heading = node("h2", "topic-title", category);
        heading.append(node("span", "topic-count", group.length));
        section.append(heading);
        for (const article of group) section.append(articleCard(article));
        container.append(section);
      }
    }
  }
  function detailRow(parent, label, value) {
    if (!value) return;
    const row = node("div", "detail-row");
    row.append(node("dt", "", label), node("dd", "", value));
    parent.append(row);
  }
  function showDetail(article) {
    $("detail-title").textContent = article.title || "未命名文章";
    const body = $("detail-body"); body.replaceChildren();
    const dl = node("dl", "details");
    detailRow(dl, "來源", article.account || "來源未知");
    detailRow(dl, "類別", article.category || "未分類");
    detailRow(dl, "發布日期", article.publishDate || "未知");
    detailRow(dl, "時效提示", freshness(article).text);
    detailRow(dl, "摘要", article.summary);
    try {
      const points = JSON.parse(article.keyPoints || "[]");
      if (Array.isArray(points)) detailRow(dl, "重點", points.map(p => typeof p === "string" ? p : JSON.stringify(p)).join("\n"));
    } catch { detailRow(dl, "重點", article.keyPoints); }
    detailRow(dl, "行動價值", `${labels[article.actionable] || article.actionable || "未知"}${article.actionableNote ? " · " + article.actionableNote : ""}`);
    detailRow(dl, "炒作程度", `${labels[article.hype] || article.hype || "未知"}${article.hypeNote ? " · " + article.hypeNote : ""}`);
    body.append(dl);
    const links = node("div", "detail-links");
    const native = node("a", "primary-link", "在 Paperless 開啟文件"); native.href = `/documents/${encodeURIComponent(article.id)}/details`; links.append(native);
    const source = safeSource(article.source);
    if (source) { const link = node("a", "", "開啟原文"); link.href = source; link.target = "_blank"; link.rel = "noopener noreferrer"; links.append(link); }
    body.append(links, node("p", "caveat", "時效依發布日期與保存期限推算，並非事實查核；請核對原文。"));
    $("detail").showModal();
  }
  async function load() {
    $("status").textContent = "正在讀取文件…";
    try {
      const [fields, correspondents, documents] = await Promise.all([
        pages("/api/custom_fields/?page_size=100"), pages("/api/correspondents/?page_size=100"),
        pages("/api/documents/?page_size=100&fields=id,title,correspondent,custom_fields")
      ]);
      const ids = Object.fromEntries(fields.filter(f => typeof f.name === "string").map(f => [f.name, f.id]));
      if (!ids.wx_source_id || !ids.wx_source_url) throw new Error("尚未建立 Article Radar 自訂欄位。請先執行匯入。 ");
      const names = new Map(correspondents.map(c => [c.id, c.name]));
      state.articles = documents.map(doc => {
        const values = fieldMap(doc);
        const get = key => values.get(ids[key]) || "";
        if (!get("wx_source_id") || !get("wx_source_url")) return null;
        return { id: doc.id, title: doc.title, account: names.get(doc.correspondent),
          source: get("wx_source_url"), category: get("wx_category"), summary: get("wx_summary"),
          keyPoints: get("wx_key_points"), actionable: get("wx_actionable"), actionableNote: get("wx_actionable_note"),
          hype: get("wx_hype"), hypeNote: get("wx_hype_note"), shelf: get("wx_shelf_life"),
          publishDate: get("wx_publish_date") };
      }).filter(Boolean);
      const categories = [...new Set(state.articles.map(a => a.category || "未分類"))].sort((a, b) => a.localeCompare(b, "zh-Hant"));
      const allOption = node("option", "", "所有類別");
      allOption.value = "";
      $("category").replaceChildren(allOption);
      for (const name of categories) { const option = node("option", "", name); option.value = name; $("category").append(option); }
      render();
    } catch (error) {
      $("status").replaceChildren(node("p", "", error.message));
      if (error.login) { const link = node("a", "", "前往 Paperless 登入"); link.href = "/accounts/login/"; $("status").append(link); }
      else { const retry = node("button", "retry", "重試"); retry.addEventListener("click", load); $("status").append(retry); }
    }
  }
  for (const id of ["search", "category", "freshness", "actionable"]) $(id).addEventListener(id === "search" ? "input" : "change", render);
  for (const view of ["map", "list"]) $(view + "-view").addEventListener("click", () => {
    state.view = view;
    $("map-view").setAttribute("aria-pressed", String(view === "map"));
    $("list-view").setAttribute("aria-pressed", String(view === "list"));
    render();
  });
  $("close-detail").addEventListener("click", () => $("detail").close());
  load();
})();
