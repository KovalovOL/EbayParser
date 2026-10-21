"use strict";
const $ = (selector) => document.querySelector(selector);
const state = { token: null, refreshPromise: null, refreshTimer: null, user: null, watches: [],
  selected: null, onlyNew: false, offset: 0, limit: 30, items: [], total: 0, editing: null,
  challenge: null, loading: false, generation: 0 };
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const date = (value) => value ? new Date(value * 1000).toLocaleString("uk-UA", {day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}) : "Ще не перевіряли";
const money = (item) => item.price_text || (item.price != null ? `${item.price} ${item.currency}` : "Ціну не вказано");
const imageURL = (value) => { try {const u = new URL(value); return u.protocol === "https:" && (u.hostname === "i.ebayimg.com" || u.hostname.endsWith(".ebayimg.com")) ? u.href : "";} catch {return "";} };
const itemURL = (value) => {try {const u = new URL(value); return u.protocol === "https:" && u.hostname === "www.ebay.com" && /^\/itm\/\d+$/.test(u.pathname) ? u.href : "#";} catch {return "#";} };

function errorText(data) {
  if (Array.isArray(data.detail)) return data.detail.map(x => x.msg.replace(/^Value error, /, "")).join(". ");
  return data.detail || "Не вдалося виконати запит. Спробуйте ще раз.";
}
async function raw(path, options = {}) {
  let response;
  try { response = await fetch(path, {credentials:"same-origin", ...options}); }
  catch {throw new Error("Немає з’єднання із сервером. Перевірте мережу.");}
  const data = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) {const error = new Error(errorText(data)); error.status = response.status; throw error;}
  return data;
}
function acceptTokens(data) {
  state.token = data.access_token;
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(() => refreshToken().catch(() => showLogin()), Math.max(5, data.expires_in - 60) * 1000);
}
async function refreshToken() {
  if (!state.refreshPromise) {
    const refresh = async () => acceptTokens(await raw("/api/auth/refresh", {method:"POST"}));
    // Refresh cookies are shared by tabs; serialize rotation in browsers supporting Web Locks.
    state.refreshPromise = (navigator.locks ? navigator.locks.request("signal-refresh", refresh) : refresh())
      .finally(() => {state.refreshPromise = null;});
  }
  return state.refreshPromise;
}
async function api(path, options = {}, retry = true) {
  if (!state.token) await refreshToken();
  try {return await raw(path, {...options, headers:{...(options.body ? {"Content-Type":"application/json"} : {}), ...options.headers, Authorization:`Bearer ${state.token}`}});}
  catch (error) {
    if (error.status === 401 && retry) {
      try {await refreshToken();} catch {showLogin(); throw new Error("Сесія закінчилася. Увійдіть ще раз.");}
      return api(path, options, false);
    }
    throw error;
  }
}
function toast(message) {
  $("#toast").textContent = message; $("#toast").hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => $("#toast").hidden = true, 5000);
}
function showError(id, message) {$(id).textContent = message; $(id).hidden = !message;}
function showLogin() {
  state.token = null; state.user = null; clearTimeout(state.refreshTimer);
  if (!$("#login-dialog").open) $("#login-dialog").showModal();
}
function empty(title, description, add = false) {
  return `<div class="empty-state"><div class="empty-icon">◎</div><h3>${escape(title)}</h3><p>${escape(description)}</p>${add ? '<button class="button primary" data-new-watch>＋ Створити перший пошук</button><div class="hint">Наприклад: Sony Walkman, Fujifilm X100, LEGO</div>' : '<div class="hint">Знахідки з’являться тут автоматично</div>'}</div>`;
}
function renderWatches() {
  $("#watch-count").textContent = state.watches.length;
  $("#stat-active").textContent = state.watches.filter(w => w.active).length;
  $("#stat-total").textContent = state.watches.reduce((sum, w) => sum + (w.counts.total || 0), 0);
  $("#stat-new").textContent = state.watches.reduce((sum, w) => sum + (w.counts.new || 0), 0);
  $("#mobile-watch-select").innerHTML = '<option value="">Усі мої пошуки</option>' + state.watches.map(w => `<option value="${w.id}">${escape(w.name)}</option>`).join("");
  $("#mobile-watch-select").value = state.selected ?? "";
  $("#all-watches").classList.toggle("selected", state.selected === null);
  $("#watch-list").innerHTML = state.watches.length ? state.watches.map(w => `<button class="watch-link ${w.id === state.selected ? "active" : ""}" data-select-watch="${w.id}"><span class="dot ${w.error ? "error" : w.active ? "online" : ""}"></span><span class="watch-title">${escape(w.name)}</span><span class="watch-counter">${w.counts.total || 0}</span></button>`).join("") : '<p class="sidebar-empty">Ваші пошуки з’являться тут</p>';
  const w = state.watches.find(w => w.id === state.selected);
  $("#page-title").textContent = w ? w.name : "Мій радар.";
  $("#page-description").textContent = w ? `Стежимо за «${w.query}» на eBay. Ваші критерії — наш орієнтир.` : "Ви обираєте, що шукати. Ми тримаємо руку на пульсі eBay.";
  $("#selected-watch").hidden = !w;
  if (w) {
    const f = w.filters;
    const tags = [f.min_price != null || f.max_price != null ? `${f.min_price ?? 0} — ${f.max_price ?? "∞"} ${f.currency}` : f.currency,
      f.location, f.seller && `Продавець: ${f.seller}`, f.condition !== "any" && ({new:"Новий",used:"Вживаний",refurbished:"Відновлений",parts:"На запчастини"}[f.condition]),
      f.buying_format !== "any" && (f.buying_format === "auction" ? "Аукціон" : "Купити зараз"),
      f.free_shipping && "Безкоштовна доставка", f.include_shipping && "Ціна з доставкою",
      ...(f.exclude_keywords || []).map(v => `Без: ${v}`), ...Object.entries(f.attributes || {}).map(([k,v]) => `${k}: ${v}`)].filter(Boolean);
    $("#selected-watch").innerHTML = `<div class="selected-watch-top"><div><h3>${escape(w.query)}</h3><div class="watch-meta">${w.active ? (w.initialized ? "Моніторинг активний" : "Готуємо початковий знімок") : "Пошук на паузі"} · кожні ${Math.round(w.interval_seconds/60)} хв<br>Остання перевірка: ${date(w.last_checked_at)}${w.active ? ` · Наступна: ${date(w.next_check_at)}` : ""}</div></div><div class="watch-actions"><button class="button subtle" data-action="check" ${w.active ? "" : "disabled"}>↻ Перевірити</button><button class="button subtle" data-action="toggle">${w.active ? "Ⅱ Пауза" : "▷ Відновити"}</button><button class="button subtle" data-action="edit">Редагувати</button><button class="icon-button danger" data-action="delete" aria-label="Видалити пошук" title="Видалити пошук">×</button></div></div><div class="filter-tags">${tags.map(t => `<span class="tag">${escape(t)}</span>`).join("")}</div>${w.counts.pending ? `<div class="watch-meta">Ще перевіряємо характеристики ${w.counts.pending} товарів.</div>` : ""}${w.error ? `<div class="watch-error">${escape(w.error)}</div>` : ""}${w.truncated ? '<div class="watch-error">Видача перевищує ліміт сторінок. Звузьте пошук для повнішого охоплення.</div>' : ""}`;
  }
}
function renderItems() {
  $("#items-count").textContent = state.total;
  $("#pagination").hidden = state.total <= state.limit;
  $("#prev-page").disabled = state.offset === 0;
  $("#next-page").disabled = state.offset + state.limit >= state.total;
  $("#page-info").textContent = `${state.offset + 1}–${Math.min(state.offset + state.limit,state.total)} з ${state.total}`;
  if (!state.items.length) {
    const w = state.watches.find(w => w.id === state.selected);
    $("#results").innerHTML = !state.watches.length ? empty("Додайте перший пошук", "Розкажіть, яку річ шукаєте, задайте ціну та фільтри. Далі радар працюватиме сам.", true)
      : state.onlyNew ? empty("Чекаємо на новий сигнал", "Тут будуть товари з підтвердженою датою публікації після початку вашого моніторингу.")
      : empty(w?.error ? "Перевірка потребує повторної спроби" : "Радар налаштовано", w?.error ? "Деталі помилки в налаштуваннях пошуку вище. Збережені результати залишаються доступними." : "Моніторинг покаже наявні оголошення після першої успішної перевірки. Про нові повідомимо в Telegram.");
    return;
  }
  $("#results").innerHTML = `<div class="item-list">${state.items.map(item => {
    const image = imageURL(item.image_url);
    const status = item.baseline ? "ПОЧАТКОВИЙ ЗНІМОК" : item.is_new ? "НОВЕ ОГОЛОШЕННЯ" : item.published_at ? "НАЯВНИЙ ТОВАР" : "ДАТУ НЕ ПІДТВЕРДЖЕНО";
    return `<article class="item-row">${image ? `<img class="item-image" src="${escape(image)}" alt="${escape(item.title)}" loading="lazy" referrerpolicy="no-referrer">` : '<div class="item-image image-placeholder">▤</div>'}<div><div class="item-labels"><span class="status-tag ${item.is_new ? "fresh" : ""}">${status}</span><button class="text-button item-watch" data-select-watch="${item.watch_id}">${escape(item.watch_name)}</button></div><button class="item-title" data-item="${item.id}">${escape(item.title)}</button><div class="item-meta"><span>${escape(item.condition || "Стан не вказано")}</span><span>⌖ ${escape(item.location || "Локація не вказана")}</span></div></div><div class="item-right"><div class="item-price">${escape(money(item))}</div><div class="item-shipping">${escape(item.shipping_text || "Доставка не вказана")}</div><a class="item-link" href="${escape(itemURL(item.url))}" target="_blank" rel="noopener noreferrer">На eBay ↗</a></div></article>`;
  }).join("")}</div>`;
}
async function load(quiet = false) {
  if (!state.user || state.loading) return;
  state.loading = true;
  const generation = state.generation;
  try {
    const params = new URLSearchParams({only_new:state.onlyNew,offset:state.offset,limit:state.limit});
    if (state.selected !== null) params.set("watch_id",state.selected);
    const [watches, page, status] = await Promise.all([api("/api/watches"), api(`/api/items?${params}`), api("/api/status")]);
    if (generation !== state.generation) return;
    state.watches = watches; state.items = page.items; state.total = page.total;
    renderWatches(); renderItems();
    const services = status.services;
    const healthy = services.worker?.online && !services.worker.error && services.bot?.online && !services.bot.error;
    $("#connection-dot").className = `dot ${healthy ? "online" : "error"}`;
    $("#connection-text").textContent = healthy ? "Усі системи на зв’язку" : "Перевірте стан сервісів";
    const warnings = [];
    for (const [key, label] of [["worker","Моніторинг"],["bot","Telegram-бот"]]) {
      const s = services[key];
      if (!s?.online) warnings.push(`${label}: процес не запущено або немає зв’язку.`);
      else if (s.error) warnings.push(`${label}: ${s.error}`);
    }
    const failed = watches.filter(w => w.error);
    if (failed.length && state.selected === null) warnings.push(`Пошуків із помилкою: ${failed.length}. Виберіть пошук для деталей.`);
    showError("#service-warning", warnings.join(" "));
    $("#sync-text").textContent = `Оновлено ${new Date().toLocaleTimeString("uk-UA",{hour:"2-digit",minute:"2-digit"})} · 15 с`;
  } catch (error) {if (!quiet) toast(error.message);}
  finally {
    state.loading = false;
    if (generation !== state.generation && state.user) load();
  }
}
function selectWatch(id) {state.selected = id; state.offset = 0; state.generation++; load();}
function addAttribute(key = "", value = "") {
  const row = document.createElement("div"); row.className = "attribute-row";
  row.innerHTML = `<input aria-label="Назва характеристики" placeholder="Brand" maxlength="100" required value="${escape(key)}"><input aria-label="Значення характеристики" placeholder="Sony" maxlength="300" required value="${escape(value)}"><button type="button" class="icon-button remove-attribute" aria-label="Прибрати характеристику">×</button>`;
  $("#attribute-rows").append(row);
}
function openWatch(watch = null) {
  if (!state.user) return showLogin();
  state.editing = watch?.id || null;
  const form = $("#watch-form"); form.reset(); $("#attribute-rows").replaceChildren();
  showError("#watch-error", ""); $("#watch-dialog-title").textContent = watch ? "Редагувати пошук" : "Новий пошук";
  if (watch) {
    for (const [key,value] of Object.entries({name:watch.name,query:watch.query,interval_seconds:watch.interval_seconds,...watch.filters})) {
      const input = form.elements.namedItem(key);
      if (!input || key === "attributes") continue;
      if (input.type === "checkbox") input.checked = value;
      else input.value = Array.isArray(value) ? value.join(", ") : value ?? "";
    }
    Object.entries(watch.filters.attributes || {}).forEach(([k,v]) => addAttribute(k,v));
    $(".advanced").open = Object.keys(watch.filters.attributes || {}).length > 0 || !!watch.filters.category_id;
  } else $(".advanced").open = false;
  $("#watch-dialog").showModal();
}
function showItem(id) {
  const item = state.items.find(x => x.id === id); if (!item) return;
  const image = imageURL(item.image_url);
  const attrs = {"Стан":item.condition,"Локація":item.location,"Продавець":item.seller,
    "Доставка":item.shipping_text,"ID eBay":item.ebay_id,"Пошук":item.watch_name,
    "Уперше побачили":date(item.first_seen_at),"Востаннє бачили":date(item.last_seen_at),
    "Опубліковано на eBay":item.published_at ? date(item.published_at) : "Дату не підтверджено — сповіщення не надсилаємо",
    "Telegram":item.notification_sent ? "Сповіщення надіслано" : item.notification_error || (item.is_new ? "У черзі відправлення" : "Без сповіщення"), ...item.attributes};
  $("#item-detail").innerHTML = `<div class="dialog-heading"><div class="eyebrow">ВАША ЗНАХІДКА НА EBAY</div><button class="icon-button" data-close="item-dialog" aria-label="Закрити">×</button></div><div class="item-detail-top">${image ? `<img class="detail-image" src="${escape(image)}" alt="${escape(item.title)}" referrerpolicy="no-referrer">` : ""}<div><h2 class="detail-title">${escape(item.title)}</h2><div class="detail-price">${escape(money(item))}</div><a class="button primary" href="${escape(itemURL(item.url))}" target="_blank" rel="noopener noreferrer">Відкрити на eBay ↗</a></div></div><table class="detail-table"><tbody>${Object.entries(attrs).filter(([,v]) => v !== null && v !== "").map(([k,v]) => `<tr><td>${escape(k)}</td><td>${escape(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`).join("")}</tbody></table>${item.detail_error ? `<div class="alert warning">${escape(item.detail_error)}</div>` : ""}<button class="text-button" data-details="${item.id}">↻ Завантажити / оновити характеристики</button><p class="muted">Збережені дані можуть відрізнятися від поточного оголошення. Ціну та доступність перевіряйте на eBay.</p><details class="advanced"><summary>Усі отримані дані eBay</summary><pre class="raw-data">${escape(JSON.stringify(item.raw || {}, null, 2))}</pre></details>`;
  $("#item-dialog").showModal();
}
async function busy(form, action, errorId) {
  const button = form.querySelector('[type="submit"]'); button.disabled = true;
  showError(errorId, "");
  try {await action();} catch(error) {showError(errorId, error.message);} finally {button.disabled = false;}
}
$("#login-dialog").addEventListener("cancel", e => e.preventDefault());
$("#login-start-form").addEventListener("submit", e => {e.preventDefault(); busy(e.currentTarget, async () => {
  const result = await raw("/api/auth/start", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:$("#username").value})});
  state.challenge = result.challenge_id; $("#bot-link").href = result.bot_url;
  $("#login-start-form").hidden = true; $("#login-code-step").hidden = false;
}, "#login-error");});
$("#login-verify-form").addEventListener("submit", e => {e.preventDefault(); busy(e.currentTarget, async () => {
  acceptTokens(await raw("/api/auth/verify", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({challenge_id:state.challenge,code:$("#login-code").value.trim()})}));
  await startSession();
}, "#login-error");});
$("#login-back").addEventListener("click", () => {$("#login-start-form").hidden = false; $("#login-code-step").hidden = true; $("#login-code").value = ""; showError("#login-error", "");});
$("#logout").addEventListener("click", async () => {
  try {await raw("/api/auth/logout",{method:"POST"}); state.watches = []; state.items = []; state.total = 0; state.selected = null; state.generation++; renderWatches(); renderItems(); $("#logout").hidden = true; $("#user-name").textContent = "Ваш простір"; $("#avatar").textContent = "↗"; showLogin();} catch(error) {toast(error.message);}
});
$("#watch-form").addEventListener("submit", e => {e.preventDefault(); busy(e.currentTarget, async () => {
  const form = new FormData($("#watch-form")); const attributes = {};
  for (const row of $("#attribute-rows").children) {const [k,v] = row.querySelectorAll("input"); if (k.value.trim() && v.value.trim()) attributes[k.value.trim()] = v.value.trim();}
  const body = {name:form.get("name"),query:form.get("query"),interval_seconds:Number(form.get("interval_seconds")),filters:{
    min_price:form.get("min_price") === "" ? null : Number(form.get("min_price")),max_price:form.get("max_price") === "" ? null : Number(form.get("max_price")),currency:form.get("currency"),condition:form.get("condition"),buying_format:form.get("buying_format"),location:form.get("location"),seller:form.get("seller"),free_shipping:form.has("free_shipping"),include_shipping:form.has("include_shipping"),exclude_keywords:form.get("exclude_keywords").split(",").map(s => s.trim()).filter(Boolean),category_id:form.get("category_id"),attributes}};
  const watch = await api(state.editing ? `/api/watches/${state.editing}` : "/api/watches", {method:state.editing ? "PUT" : "POST",body:JSON.stringify(body)});
  $("#watch-dialog").close(); toast(state.editing ? "Пошук оновлено" : "Радар налаштовано. Готуємо початковий знімок."); selectWatch(watch.id);
}, "#watch-error");});
$("#add-attribute").addEventListener("click", () => addAttribute());
$("#new-watch").addEventListener("click", () => openWatch());
$("#sidebar-add").addEventListener("click", () => openWatch());
$("#all-watches").addEventListener("click", () => selectWatch(null));
$("#mobile-watch-select").addEventListener("change", e => selectWatch(e.target.value ? Number(e.target.value) : null));
$("#tab-all").addEventListener("click", () => setTab(false));
$("#tab-new").addEventListener("click", () => setTab(true));
function setTab(onlyNew) {state.onlyNew = onlyNew; state.offset = 0; state.generation++; for(const [id,active] of [["#tab-all",!onlyNew],["#tab-new",onlyNew]]) {$(id).classList.toggle("active",active); $(id).setAttribute("aria-selected",String(active));} load();}
$("#prev-page").addEventListener("click", () => {state.offset = Math.max(0,state.offset-state.limit); state.generation++; load();});
$("#next-page").addEventListener("click", () => {state.offset += state.limit; state.generation++; load();});
document.addEventListener("click", async e => {
  const close = e.target.closest("[data-close]"); if (close) $("#"+close.dataset.close).close();
  const select = e.target.closest("[data-select-watch]"); if (select) selectWatch(Number(select.dataset.selectWatch));
  if (e.target.closest("[data-new-watch]")) openWatch();
  if (e.target.closest(".remove-attribute")) e.target.closest(".attribute-row").remove();
  const item = e.target.closest("[data-item]"); if (item) showItem(Number(item.dataset.item));
  const detail = e.target.closest("[data-details]");
  if (detail) {detail.disabled = true; try {const r = await api(`/api/items/${detail.dataset.details}/details`,{method:"POST"});toast(r.message);} catch(error) {toast(error.message);} finally {detail.disabled = false;}}
  const action = e.target.closest("[data-action]"); if (!action) return;
  const watch = state.watches.find(w => w.id === state.selected); if (!watch) return;
  action.disabled = true;
  try {
    if (action.dataset.action === "edit") return openWatch(watch);
    if (action.dataset.action === "delete") {if (!confirm(`Видалити пошук «${watch.name}» і збережені для нього результати?`)) return; await api(`/api/watches/${watch.id}`,{method:"DELETE"}); selectWatch(null); toast("Пошук видалено");}
    if (action.dataset.action === "toggle") {await api(`/api/watches/${watch.id}`,{method:"PATCH",body:JSON.stringify({active:!watch.active})});toast(watch.active ? "Пошук призупинено" : "Пошук відновлено. Формуємо новий знімок."); await load();}
    if (action.dataset.action === "check") {await api(`/api/watches/${watch.id}/check`,{method:"POST"});toast("Перевірку додано в чергу"); await load();}
  } catch(error) {toast(error.message);} finally {action.disabled = false;}
});
async function startSession() {
  state.user = await api("/api/me"); $("#user-name").textContent = "@" + state.user.username;
  $("#avatar").textContent = state.user.username.slice(0,2).toUpperCase(); $("#logout").hidden = false;
  if ($("#login-dialog").open) $("#login-dialog").close(); await load();
}
async function init() {
  renderItems();
  try {await refreshToken(); await startSession();}
  catch {showLogin(); raw("/api/config").then(config => {if(config.error) showError("#login-error",config.error);}).catch(error => showError("#login-error",error.message));}
  setInterval(() => {if(state.user && !document.hidden) load(true);},15000);
}
init();
