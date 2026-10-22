"use strict";
const $ = (selector) => document.querySelector(selector);
const state = {token:null, refreshPromise:null, refreshTimer:null, user:null, watches:[], selected:null,
  onlyNew:false, offset:0, limit:30, items:[], total:0, editing:null, deleting:null, challenge:null,
  loginDeadline:0, loginStatus:"waiting", challengeTimer:null, loginPoll:null, pollingChallenge:null,
  verifying:false, loginAuthenticated:false, openItem:null, detailVersion:null,
  loading:false, generation:0, monitoring:null};
const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const date = value => value ? new Date(value * 1000).toLocaleString("en-GB", {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"}) : "Not checked yet";
const money = item => item.price_text || (item.price != null ? `${item.price} ${item.currency || ""}` : "Price not specified");
const imageURL = value => {try {const u = new URL(value); return u.protocol === "https:" && (u.hostname === "i.ebayimg.com" || u.hostname.endsWith(".ebayimg.com")) ? u.href : "";} catch {return "";}};
const itemURL = value => {try {const u = new URL(value); return u.protocol === "https:" && u.hostname === "www.ebay.com" && /^\/itm\/\d+$/.test(u.pathname) ? u.href : "#";} catch {return "#";}};
const conditionLabels = {new:"New",used:"Used",refurbished:"Refurbished",parts:"For parts"};
function errorText(data) {
  if (Array.isArray(data.detail)) return data.detail.map(x => x.msg.replace(/^Value error, /, "")).join(". ");
  return typeof data.detail === "string" ? data.detail : "The request failed. Please try again.";
}
async function raw(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(path, {credentials:"same-origin",signal:controller.signal,...options});
    const data = response.status === 204 ? null : await response.json().catch(() => ({}));
    if (!response.ok) {const error = new Error(errorText(data)); error.status = response.status; throw error;}
    return data;
  } catch(error) {
    if (error.status) throw error;
    throw new Error(error.name === "AbortError" ? "The server took too long to respond. Try again." : "Cannot reach the server. Check your connection and try again.");
  } finally {clearTimeout(timeout);}
}
function scheduleRefresh(seconds) {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(() => refreshToken().catch(error => {
    if (error.status === 401) showLogin();
    else {connectionError(error.message); scheduleRefresh(30);}
  }), seconds * 1000);
}
function acceptTokens(data) {state.token = data.access_token; scheduleRefresh(Math.max(5, data.expires_in - 60));}
async function refreshToken() {
  if (!state.refreshPromise) {
    const refresh = async () => acceptTokens(await raw("/api/auth/refresh", {method:"POST"}));
    state.refreshPromise = (navigator.locks ? navigator.locks.request("signal-refresh", refresh) : refresh())
      .finally(() => {state.refreshPromise = null;});
  }
  return state.refreshPromise;
}
async function api(path, options = {}, retry = true) {
  if (!state.token) await refreshToken();
  try {return await raw(path, {...options,headers:{...(options.body ? {"Content-Type":"application/json"} : {}),...options.headers,Authorization:`Bearer ${state.token}`}});}
  catch(error) {
    if (error.status === 401 && retry) {
      try {await refreshToken();} catch(refreshError) {
        if (refreshError.status === 401) {showLogin(); throw new Error("Your session has expired. Sign in again.");}
        throw refreshError;
      }
      return api(path,options,false);
    }
    if (error.status === 401) showLogin();
    throw error;
  }
}
function toast(message) {
  $("#toast").textContent = message; $("#toast").hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => $("#toast").hidden = true, 6000);
}
function setText(element,text) {if (element.textContent !== text) element.textContent = text;}
function showError(id,message) {setText($(id),message); $(id).hidden = !message;}
function setHTML(element,html) {
  if (element.innerHTML === html) return;
  // Polling must not discard keyboard focus on search controls or listing links.
  const focused = element.contains(document.activeElement) ? document.activeElement : null;
  const key = focused?.dataset.focus;
  element.innerHTML = html;
  if (key) element.querySelector(`[data-focus="${CSS.escape(key)}"]`)?.focus({preventScroll:true});
}
function resetLogin() {
  state.challenge = null; state.loginStatus = "waiting"; state.loginAuthenticated = false;
  clearInterval(state.challengeTimer); clearTimeout(state.loginPoll);
  $("#login-start-form").hidden = false; $("#login-code-step").hidden = true;
  $("#login-code").value = ""; $("#login-code").disabled = false;
  $("#login-submit").disabled = false; $("#login-submit").textContent = "Verify & sign in";
  showError("#login-error",""); loginGuide(1);
}
function showLogin() {
  if ($("#login-dialog").open) return;
  clearTimeout(state.refreshTimer);
  state.token = null; state.user = null; state.watches = []; state.items = []; state.total = 0;
  state.selected = null; state.offset = 0; state.onlyNew = false; state.generation++;
  document.querySelectorAll("dialog[open]").forEach(dialog => dialog.close());
  $("#logout").hidden = true; $("#user-name").textContent = "Not signed in"; $("#avatar").textContent = "M";
  $("#connection-text").textContent = "Sign in required"; $("#connection-dot").className = "dot";
  $("#connection-detail").textContent = ""; state.monitoring = null;
  showError("#service-warning",""); $("#load-error").hidden = true;
  resetLogin(); renderWatches(); updateTabs(); renderItems(); $("#login-dialog").showModal();
}
function empty(title,description,action = "") {
  return `<div class="empty-state"><h3>${escape(title)}</h3><p>${escape(description)}</p>${action}</div>`;
}
function watchStatus(w) {return !w.active ? "Paused" : state.monitoring?.state === "restricted" ? "eBay paused" : w.error ? "Retry scheduled" : !w.initialized ? "Creating initial snapshot" : "Monitoring";}
function renderWatches() {
  $("#watch-count").textContent = state.watches.length;
  $("#stat-active").textContent = state.watches.filter(w => w.active).length;
  $("#stat-total").textContent = state.watches.reduce((sum,w) => sum + (w.counts.total || 0),0);
  $("#stat-new").textContent = state.watches.reduce((sum,w) => sum + (w.counts.new || 0),0);
  const options = '<option value="">All searches</option>' + state.watches.map(w => `<option value="${w.id}">${escape(w.name)}${!w.active ? " (paused)" : w.error ? " (retry scheduled)" : ""}</option>`).join("");
  setHTML($("#mobile-watch-select"),options); $("#mobile-watch-select").value = state.selected ?? "";
  $("#all-watches").classList.toggle("selected",state.selected === null);
  $("#all-watches").setAttribute("aria-pressed",String(state.selected === null));
  setHTML($("#watch-list"),state.watches.length ? state.watches.map(w => `<button class="watch-link ${w.id === state.selected ? "active" : ""}" data-select-watch="${w.id}" data-focus="watch-${w.id}" aria-current="${w.id === state.selected ? "true" : "false"}" title="${escape(`${w.name}: ${watchStatus(w)}`)}"><span class="dot ${!w.active ? "" : w.error || state.monitoring?.state === "restricted" ? "error" : "online"}" aria-hidden="true"></span><span class="watch-title">${escape(w.name)}<small>${watchStatus(w)}</small></span><span class="watch-counter" aria-label="${w.counts.total || 0} saved matches">${w.counts.total || 0}</span></button>`).join("") : '<p class="sidebar-empty">No saved searches yet.</p>');
  const w = state.watches.find(w => w.id === state.selected);
  $("#page-title").textContent = w ? w.name : "All searches";
  $("#page-description").textContent = w ? `Keywords: ${w.query}` : "Saved listings from your monitored searches.";
  $("#selected-watch").hidden = !w;
  $("#overview-stats").hidden = !!w;
  if (!w) return;
  const f = w.filters;
  const tags = [f.min_price != null || f.max_price != null ? `${f.min_price ?? 0} – ${f.max_price ?? "any"} ${f.currency}` : f.currency,
    f.location && `Location: ${f.location}`,f.seller && `Seller: ${f.seller}`,conditionLabels[f.condition],
    f.buying_format !== "any" && (f.buying_format === "auction" ? "Auction" : "Buy It Now"),
    f.free_shipping && "Free shipping",f.include_shipping && "Shipping included in limits",
    ...(f.exclude_keywords || []).map(v => `Exclude: ${v}`),...Object.entries(f.attributes || {}).map(([k,v]) => `${k}: ${v}`),
    f.category_id && `Category: ${f.category_id}`].filter(Boolean);
  const blocked = state.monitoring?.state === "restricted";
  const nextAttempt = blocked && w.error ? state.monitoring.retry_at : Math.max(w.next_check_at, blocked ? state.monitoring.retry_at : 0);
  const queued = w.active && !blocked && nextAttempt <= Date.now()/1000;
  const checkDisabled = !w.active || blocked || queued || (w.last_checked_at && Date.now()/1000 - w.last_checked_at < 60);
  const interval = w.interval_seconds >= 3600 ? `${w.interval_seconds/3600} hr` : `${w.interval_seconds/60} min`;
  setHTML($("#selected-watch"),`<div class="selected-watch-top"><div><h3>${watchStatus(w)}</h3><div class="watch-meta">Every ${interval} · Last check: ${date(w.last_checked_at)}${w.active ? `<br>${blocked ? `Retries resume: ${date(state.monitoring.retry_at)}${nextAttempt > state.monitoring.retry_at + 1 ? `<br>Scheduled check: ${date(nextAttempt)}` : ""}` : queued ? "Next check: queued or in progress" : `Next attempt: ${date(nextAttempt)}`}` : ""}</div></div><div class="watch-actions"><button class="button subtle" data-action="check" data-focus="check" ${checkDisabled ? "disabled" : ""} title="${blocked ? `eBay requests paused until ${date(state.monitoring.retry_at)}` : queued ? "Already queued or in progress" : "Request a check (once per minute)"}">${queued ? "Check queued" : "Check now"}</button><button class="button subtle" data-action="toggle" data-focus="toggle">${w.active ? "Pause" : "Resume"}</button><button class="button subtle" data-action="edit" data-focus="edit">Edit</button><button class="button subtle danger" data-action="delete" data-focus="delete">Delete</button></div></div><div class="filter-tags">${tags.map(t => `<span class="tag">${escape(t)}</span>`).join("")}</div>${!w.active ? '<p class="watch-meta pause-note">Checks and pending alerts are paused. Saved results stay available. Resuming saves a new snapshot; listings added while paused will not trigger alerts.</p>' : !w.initialized ? '<p class="watch-meta pause-note">The first successful check saves existing listings without alerts.</p>' : ""}${w.counts.pending ? `<p class="watch-meta">${w.counts.pending} item${w.counts.pending === 1 ? " is" : "s are"} cannot be confirmed from the information on the search cards.</p>` : ""}${w.error ? `<div class="watch-error">${escape(w.error)}${w.active ? " Saved results remain available; retries are automatic." : ""}</div>` : ""}`);
}
function renderItems() {
  $("#items-count").textContent = state.watches.filter(w => state.selected === null || w.id === state.selected).reduce((sum,w) => sum + (w.counts.total || 0),0);
  $("#pagination").hidden = state.total <= state.limit;
  $("#prev-page").disabled = state.offset === 0; $("#next-page").disabled = state.offset + state.limit >= state.total;
  $("#page-info").textContent = state.total ? `${state.offset + 1}–${Math.min(state.offset + state.limit,state.total)} of ${state.total}` : "0 results";
  if (!state.items.length) {
    const w = state.watches.find(w => w.id === state.selected);
    const failed = state.watches.filter(w => w.error && w.active);
    const pending = (w ? [w] : state.watches).reduce((sum,x) => sum + (x.counts.pending || 0),0);
    const action = failed.length && !w ? `<div class="empty-actions">${failed.map(x => `<button class="button subtle" data-select-watch="${x.id}">View ${escape(x.name)} status</button>`).join("")}</div>` : "";
    $("#results").innerHTML = !state.user ? empty("Sign in to view your searches","Use Telegram to access your saved searches and listings.")
      : !state.watches.length ? empty("Add your first search","Enter eBay keywords, then set any price, location or item filters you need.",'<button class="button primary" data-new-watch>＋ Add search</button>')
      : state.onlyNew ? empty("No new matches yet","This view includes title, price and location combinations first seen after the initial snapshot.",'<button class="button subtle" data-show-all>View all listings</button>')
      : empty(w?.error || failed.length ? "Waiting for a successful check" : w && !w.active ? "Search paused" : pending ? "Filter information unavailable" : (w ? w.initialized : state.watches.every(x => x.initialized)) ? "No matching listings" : "Creating the initial snapshot",w && !w.active ? "Resume the search to check eBay. Listings added while paused will be saved without alerts." : pending ? `${pending} items lack information needed to confirm your local filters. Simplify those filters or inspect the listings on eBay.` : w?.error || failed.length ? "eBay or the monitoring service could not complete a check. Open a search's status for the error and next attempt." : "Results appear after a successful check. If this search stays empty, try broader keywords or fewer filters.",action);
    return;
  }
  setHTML($("#results"),`<div class="item-list">${state.items.map(item => {
    const image = imageURL(item.image_url);
    const status = item.baseline ? "Initial snapshot" : item.is_new ? "New match" : "Already seen";
    const reason = item.baseline ? "Saved in the initial snapshot. No Telegram alert." : item.is_new ? "This title, price and location combination was first seen after the initial snapshot." : "This title, price and location combination has already been seen for your account.";
    return `<article class="item-row">${image ? `<img class="item-image" src="${escape(image)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : '<div class="item-image image-placeholder" aria-label="No item image">No image</div>'}<div class="item-content"><div class="item-labels"><span class="status-tag ${item.is_new ? "fresh" : ""}" title="${reason}">${status}</span><button class="text-button item-watch" data-select-watch="${item.watch_id}" data-focus="item-watch-${item.id}">${escape(item.watch_name)}</button></div><button class="item-title" data-item="${item.id}" data-focus="item-${item.id}">${escape(item.title)}</button><div class="item-meta"><span>${escape(item.condition || "Condition not specified")}</span><span>${escape(item.location || "Location not specified")}</span></div>${item.notification_error ? '<span class="notification-warning">Telegram delivery will retry</span>' : item.is_new ? `<span class="notification-status">${item.notification_sent ? "Telegram alert sent" : item.notification_pending ? "Telegram alert queued" : "No pending Telegram alert"}</span>` : ""}</div><div class="item-right"><div class="item-price">${escape(money(item))}</div><div class="item-shipping">${escape(item.shipping_text || "Shipping not specified")}</div><a class="item-link" href="${escape(itemURL(item.url))}" data-focus="ebay-${item.id}" target="_blank" rel="noopener noreferrer">View on eBay ↗</a></div></article>`;
  }).join("")}</div>`);
}
function connectionError(message) {
  $("#load-error-text").textContent = `${message} Displayed results may be out of date.`;
  $("#load-error").hidden = false; $("#connection-dot").className = "dot error";
  $("#connection-text").textContent = "Connection interrupted"; $("#sync-text").textContent = "Refresh failed · retrying automatically";
  $("#connection-detail").textContent = "Monitoring status unavailable · reconnecting";
}
function renderMonitoring(status) {
  const m = status.monitoring || {state:"offline"};
  const browser = {chromium:"Chromium",firefox:"Firefox"}[m.browser];
  const labels = {monitoring:"Monitoring",checking:`Checking eBay${browser ? ` · ${browser}` : "…"}`,restricted:`eBay paused until ${date(m.retry_at)}`,
    paused:"All searches paused",no_searches:"No saved searches",offline:"Monitoring worker offline",
    error:"Monitoring error",retrying:"Searches waiting to retry"};
  const details = [];
  if (m.state === "restricted") {
    details.push("Access restricted · retry is automatic");
    if (m.next_check_at > m.retry_at + 1) details.push(`Next attempt: ${date(m.next_check_at)}`);
  }
  else if (["monitoring","retrying"].includes(m.state)) details.push(m.next_check_at > Date.now()/1000 ? `Next attempt: ${date(m.next_check_at)}` : "Next check queued");
  else if (m.state === "offline") details.push("Checks will resume when the worker reconnects");
  else if (m.state === "error") details.push(m.error || "The worker will retry automatically");
  if (["monitoring","checking"].includes(m.state) && m.failed_searches) details.push(`${m.failed_searches} ${m.failed_searches === 1 ? "search" : "searches"} waiting to retry`);
  const bot = status.services.bot;
  if (!bot?.online) details.push("Telegram bot offline");
  else if (bot.error) details.push("Telegram bot needs attention");
  $("#connection-text").textContent = labels[m.state] || "Monitoring status unavailable";
  $("#connection-detail").textContent = details.join("\n");
  $("#connection-dot").className = `dot ${["monitoring","checking"].includes(m.state) && bot?.online && !bot.error ? "online" : ["no_searches","paused"].includes(m.state) ? "" : "error"}`;
}
async function load() {
  if (!state.user || state.loading) return;
  state.loading = true; $("#results").setAttribute("aria-busy","true");
  const generation = state.generation;
  try {
    const params = new URLSearchParams({only_new:state.onlyNew,offset:state.offset,limit:state.limit});
    if (state.selected !== null) params.set("watch_id",state.selected);
    const [watches,page,status] = await Promise.all([api("/api/watches"),api(`/api/items?${params}`),api("/api/status")]);
    if (generation !== state.generation) return;
    state.watches = watches;
    if (state.selected !== null && !watches.some(w => w.id === state.selected)) {state.selected = null; state.offset = 0; state.generation++; return;}
    if (state.offset && state.offset >= page.total) {state.offset = Math.max(0,Math.floor((page.total-1)/state.limit)*state.limit); state.generation++; return;}
    state.monitoring = status.monitoring;
    state.items = page.items; state.total = page.total; renderWatches(); renderItems();
    if ($("#item-dialog").open && state.openItem) showItem(state.openItem);
    $("#load-error").hidden = true;
    const services = status.services;
    renderMonitoring(status);
    const warnings = [];
    for (const [key,label] of [["worker","Monitoring worker"],["bot","Telegram bot"]]) {
      const s = services[key];
      if (!s?.online) warnings.push(`${label} is offline. ${key === "worker" ? "Searches cannot run" : "Sign-in codes cannot be delivered"} until it reconnects.`);
      else if (s.error) warnings.push(`${label}: ${s.error}`);
    }
    const failed = watches.filter(w => w.error && w.active);
    if (failed.length && state.selected === null) warnings.push(`${failed.length} search${failed.length === 1 ? " has" : "es have"} a retry scheduled. Select a search for details.`);
    showError("#service-warning",warnings.join(" "));
    $("#sync-text").textContent = `Updated ${new Date().toLocaleTimeString("en-GB",{hour:"2-digit",minute:"2-digit"})} · refreshes every 15s`;
  } catch(error) {
    if (generation !== state.generation || !state.user) return;
    // A search may have been deleted in another tab between requests.
    if (error.status === 404 && state.selected !== null) {state.selected = null; state.offset = 0; state.generation++;}
    else connectionError(error.message);
  } finally {
    state.loading = false; $("#results").setAttribute("aria-busy","false");
    if (generation !== state.generation && state.user) load();
  }
}
function selectWatch(id) {state.selected = id; state.offset = 0; state.generation++; load();}
function addAttribute(key = "",value = "") {
  const row = document.createElement("div"); row.className = "attribute-row";
  row.innerHTML = `<input aria-label="Attribute name" placeholder="Brand" maxlength="100" required value="${escape(key)}"><input aria-label="Attribute value" placeholder="Sony" maxlength="300" required value="${escape(value)}"><button type="button" class="icon-button remove-attribute" aria-label="Remove attribute">×</button>`;
  $("#attribute-rows").append(row);
}
function watchPayload() {
  const form = new FormData($("#watch-form")); const attributes = {};
  for (const row of $("#attribute-rows").children) {
    const [k,v] = row.querySelectorAll("input"); const key = k.value.trim();
    if (!key || !v.value.trim()) throw new Error("Enter a name and value for each attribute, or remove the empty row.");
    if (Object.keys(attributes).some(x => x.toLowerCase() === key.toLowerCase())) throw new Error(`The attribute “${key}” is listed twice. Keep one value per attribute.`);
    attributes[key] = v.value.trim();
  }
  const query = form.get("query").trim();
  return {name:form.get("name").trim() || query.slice(0,100),query,interval_seconds:Number(form.get("interval_seconds")),filters:{
    min_price:form.get("min_price") === "" ? null : Number(form.get("min_price")),max_price:form.get("max_price") === "" ? null : Number(form.get("max_price")),currency:form.get("currency"),condition:form.get("condition"),buying_format:form.get("buying_format"),location:form.get("location").trim(),seller:form.get("seller").trim(),free_shipping:form.has("free_shipping"),include_shipping:form.has("include_shipping"),exclude_keywords:form.get("exclude_keywords").split(",").map(s => s.trim()).filter(Boolean),category_id:form.get("category_id"),attributes}};
}
function criteriaChanged() {
  if (!state.editing) return false;
  const original = state.watches.find(w => w.id === state.editing); if (!original) return false;
  const canonical = value => value && typeof value === "object" ? Array.isArray(value) ? value.map(canonical) : Object.fromEntries(Object.keys(value).sort().map(k => [k,canonical(value[k])])) : value;
  try {const body = watchPayload(); return body.query !== original.query || JSON.stringify(canonical(body.filters)) !== JSON.stringify(canonical(original.filters));}
  catch {return true;}
}
function updateChangeNotice() {
  const changed = criteriaChanged(); $("#watch-change-note").hidden = !changed;
  $("#watch-save").textContent = changed ? "Replace results & save" : "Save search";
}
function openWatch(watch = null) {
  if (!state.user) return showLogin();
  state.editing = watch?.id || null;
  const form = $("#watch-form"); form.reset(); $("#attribute-rows").replaceChildren();
  showError("#watch-error",""); $("#watch-dialog-title").textContent = watch ? "Edit search" : "Add search";
  if (watch) {
    for (const [key,value] of Object.entries({name:watch.name,query:watch.query,interval_seconds:watch.interval_seconds,...watch.filters})) {
      const input = form.elements.namedItem(key); if (!input || key === "attributes") continue;
      if (input.type === "checkbox") input.checked = value;
      else input.value = Array.isArray(value) ? value.join(", ") : value ?? "";
    }
    // Preserve intervals created through the API even if not in the preset list.
    if (!form.elements.interval_seconds.value) {
      form.elements.interval_seconds.add(new Option(`${watch.interval_seconds} seconds`,watch.interval_seconds));
      form.elements.interval_seconds.value = watch.interval_seconds;
    }
    Object.entries(watch.filters.attributes || {}).forEach(([k,v]) => addAttribute(k,v));
    $("#watch-advanced").open = Object.keys(watch.filters.attributes || {}).length > 0 || !!watch.filters.category_id;
  } else $("#watch-advanced").open = false;
  $("#watch-create-note").hidden = !!watch; updateChangeNotice(); $("#watch-dialog").showModal();
}
function showItem(id) {
  const item = state.items.find(x => x.id === id); if (!item) return;
  const version = JSON.stringify(item);
  const sameItem = state.openItem === id && $("#item-dialog").open;
  if (sameItem && version === state.detailVersion) return;
  const expanded = sameItem && $("#item-detail .advanced")?.open;
  state.openItem = id; state.detailVersion = version;
  const image = imageURL(item.image_url);
  const attrs = {"Condition":item.condition,"Location":item.location,"Seller":item.seller,"Shipping":item.shipping_text,
    "eBay item ID":item.ebay_id,"Search":item.watch_name,"First found":date(item.first_seen_at),"Last seen":date(item.last_seen_at),
    "Notification identity":"Title + price + location, shared across your searches",
    "Telegram alert":item.notification_sent ? "Sent" : item.notification_error || (item.notification_pending ? "Queued for delivery" : item.baseline ? "Not sent: initial snapshot" : item.is_new ? "No pending alert (the search may have been paused)" : "Not sent: combination already seen"),...item.attributes};
  setHTML($("#item-detail"),`<div class="dialog-heading"><h2 id="item-detail-title">Item details</h2><button class="icon-button" data-close="item-dialog" data-focus="item-close" aria-label="Close item details">×</button></div><div class="item-detail-top">${image ? `<img class="detail-image" src="${escape(image)}" alt="" referrerpolicy="no-referrer">` : ""}<div><h3 class="detail-title">${escape(item.title)}</h3><div class="detail-price">${escape(money(item))}</div><a class="button primary" href="${escape(itemURL(item.url))}" target="_blank" rel="noopener noreferrer">View on eBay ↗</a></div></div><table class="detail-table"><caption class="sr-only">Saved listing information and eBay attributes</caption><tbody>${Object.entries(attrs).filter(([,v]) => v != null && v !== "").map(([k,v]) => `<tr><th scope="row">${escape(k)}</th><td>${escape(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`).join("")}</tbody></table><p class="field-help">Saved from eBay search results. Confirm current price and availability on eBay.</p><details class="advanced"><summary data-focus="item-raw">All captured eBay data</summary><pre class="raw-data">${escape(JSON.stringify(item.raw || {},null,2))}</pre></details>`);
  if (expanded) $("#item-detail .advanced").open = true;
  if (!$("#item-dialog").open) $("#item-dialog").showModal();
}
async function busy(form,action,errorId) {
  const button = form.querySelector('[type="submit"]'); const text = button.textContent;
  button.disabled = true; button.textContent = "Please wait…"; showError(errorId,"");
  try {await action();} catch(error) {showError(errorId,error.message);}
  finally {button.disabled = false; button.textContent = text;}
}
function loginGuide(step) {document.querySelectorAll("[data-login-step]").forEach(el => el.classList.toggle("current",Number(el.dataset.loginStep) === step));}
function loginCountdown() {
  if (!state.challenge) return;
  const remaining = Math.max(0,Math.ceil((state.loginDeadline-Date.now())/1000));
  const ended = !remaining || ["expired","locked","used"].includes(state.loginStatus);
  $("#login-submit").disabled = state.verifying || (ended && !state.loginAuthenticated);
  if (state.loginAuthenticated) {$("#login-submit").textContent = state.verifying ? "Please wait…" : "Retry sign-in"; return;}
  $("#login-code").disabled = ended;
  $("#login-expiry").textContent = !remaining ? "Code expired. Request a new code below." : `Code expires in ${Math.floor(remaining/60)}:${String(remaining%60).padStart(2,"0")}.`;
  if (ended) {
    $("#login-delivery").textContent = state.loginStatus === "locked" ? "Too many incorrect attempts. Request a new code." : state.loginStatus === "used" ? "This code has already been used. Sign in with a new code." : "This sign-in request has expired.";
    clearInterval(state.challengeTimer); clearTimeout(state.loginPoll);
  }
}
async function pollLogin(cid) {
  if (cid !== state.challenge || !$("#login-dialog").open || state.pollingChallenge === cid) return;
  clearTimeout(state.loginPoll); state.pollingChallenge = cid;
  try {
    const result = await raw(`/api/auth/challenges/${encodeURIComponent(cid)}`);
    if (cid !== state.challenge) return;
    const wasSent = state.loginStatus === "sent"; state.loginStatus = result.status;
    state.loginDeadline = Date.now() + result.expires_in*1000;
    if (result.status === "sent") {
      setText($("#login-delivery"),`Code sent in Telegram. Enter it below. ${result.attempts_remaining} attempts remaining.`);
      loginGuide(3); if (!wasSent && !$("#login-code").disabled) $("#login-code").focus();
    } else if (result.status === "waiting") setText($("#login-delivery"),"Waiting for you to press Start in Telegram…");
    loginCountdown();
  } catch(error) {
    if (cid !== state.challenge) return;
    $("#login-delivery").textContent = "Delivery status unavailable. If the bot sent a code, you can still enter it below.";
  } finally {if (state.pollingChallenge === cid) state.pollingChallenge = null;}
  if (cid === state.challenge && ["waiting","sent"].includes(state.loginStatus) && state.loginDeadline > Date.now()) state.loginPoll = setTimeout(() => pollLogin(cid),3000);
}
$("#login-dialog").addEventListener("cancel",e => e.preventDefault());
$("#username").addEventListener("input",e => {e.target.value = e.target.value.replace(/^@+/,"");});
$("#login-start-form").addEventListener("submit",e => {e.preventDefault(); busy(e.currentTarget,async () => {
  const username = $("#username").value.trim().replace(/^@+/,"").toLowerCase();
  const result = await raw("/api/auth/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username})});
  state.challenge = result.challenge_id; state.loginStatus = "waiting"; state.loginDeadline = Date.now() + result.expires_in*1000;
  $("#bot-link").href = result.bot_url; $("#login-username").textContent = "@" + username;
  $("#login-start-form").hidden = true; $("#login-code-step").hidden = false;
  $("#login-code").disabled = false; $("#login-delivery").textContent = "Waiting for you to press Start in Telegram…";
  loginGuide(2); loginCountdown(); state.challengeTimer = setInterval(loginCountdown,1000);
  $("#bot-link").focus(); pollLogin(state.challenge);
},"#login-error");});
$("#login-verify-form").addEventListener("submit",e => {e.preventDefault(); busy(e.currentTarget,async () => {
  state.verifying = true; $("#login-back").disabled = true;
  try {
    if (!state.loginAuthenticated) {
      acceptTokens(await raw("/api/auth/verify",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({challenge_id:state.challenge,code:$("#login-code").value.trim()})}));
      state.loginAuthenticated = true;
    }
    await startSession();
  } catch(error) {if (state.challenge) await pollLogin(state.challenge); throw error;}
  finally {state.verifying = false; $("#login-back").disabled = false;}
},"#login-error").then(loginCountdown);});
$("#login-back").addEventListener("click",() => {resetLogin(); $("#login-code").disabled = false; $("#username").focus();});
$("#login-code").addEventListener("input",e => {e.target.value = e.target.value.replace(/\D/g,"").slice(0,6);});
$("#logout").addEventListener("click",async () => {
  $("#logout").disabled = true;
  try {await raw("/api/auth/logout",{method:"POST"}); showLogin();}
  catch(error) {toast(error.message);} finally {$("#logout").disabled = false;}
});
$("#watch-form").addEventListener("input",updateChangeNotice);
$("#watch-form").addEventListener("change",updateChangeNotice);
$("#watch-form").addEventListener("submit",e => {e.preventDefault(); busy(e.currentTarget,async () => {
  const body = watchPayload();
  if (!body.query) throw new Error("Enter your eBay search keywords.");
  if (body.filters.min_price != null && body.filters.max_price != null && body.filters.min_price > body.filters.max_price) {$("#max-price").focus(); throw new Error("Maximum price must be at least the minimum price.");}
  const edited = !!state.editing; const replaced = criteriaChanged();
  const watch = await api(edited ? `/api/watches/${state.editing}` : "/api/watches",{method:edited ? "PUT" : "POST",body:JSON.stringify(body)});
  $("#watch-dialog").close(); toast(replaced ? "Search updated. Creating a new initial snapshot." : edited ? "Search updated." : "Search saved. The first check will save existing listings without alerts."); selectWatch(watch.id);
},"#watch-error");});
$("#delete-form").addEventListener("submit",e => {e.preventDefault(); busy(e.currentTarget,async () => {
  await api(`/api/watches/${state.deleting}`,{method:"DELETE"}); $("#delete-dialog").close(); state.deleting = null; selectWatch(null); toast("Search and its saved results deleted.");
},"#delete-error");});
$("#add-attribute").addEventListener("click",() => {addAttribute(); updateChangeNotice(); $("#attribute-rows").lastElementChild.querySelector("input").focus();});
$("#new-watch").addEventListener("click",() => openWatch());
$("#sidebar-add").addEventListener("click",() => openWatch());
$("#all-watches").addEventListener("click",() => selectWatch(null));
$("#mobile-watch-select").addEventListener("change",e => selectWatch(e.target.value ? Number(e.target.value) : null));
$("#tab-all").addEventListener("click",() => setTab(false)); $("#tab-new").addEventListener("click",() => setTab(true));
function updateTabs() {for (const [id,active] of [["#tab-all",!state.onlyNew],["#tab-new",state.onlyNew]]) {$(id).classList.toggle("active",active); $(id).setAttribute("aria-pressed",String(active));}}
function setTab(onlyNew) {state.onlyNew = onlyNew; state.offset = 0; state.generation++; updateTabs(); load();}
$("#prev-page").addEventListener("click",() => {state.offset = Math.max(0,state.offset-state.limit); state.generation++; load();});
$("#next-page").addEventListener("click",() => {state.offset += state.limit; state.generation++; load();});
$("#retry-load").addEventListener("click",() => load());
document.addEventListener("click",async e => {
  const close = e.target.closest("[data-close]"); if (close) $("#" + close.dataset.close).close();
  const select = e.target.closest("[data-select-watch]"); if (select) selectWatch(Number(select.dataset.selectWatch));
  if (e.target.closest("[data-new-watch]")) openWatch();
  if (e.target.closest("[data-show-all]")) setTab(false);
  if (e.target.closest(".remove-attribute")) {e.target.closest(".attribute-row").remove(); updateChangeNotice(); $("#add-attribute").focus();}
  const item = e.target.closest("[data-item]"); if (item) showItem(Number(item.dataset.item));
  const action = e.target.closest("[data-action]"); if (!action) return;
  const watch = state.watches.find(w => w.id === state.selected); if (!watch) return;
  if (action.dataset.action === "edit") return openWatch(watch);
  if (action.dataset.action === "delete") {
    state.deleting = watch.id; $("#delete-description").textContent = `Delete “${watch.name}”? This removes its saved results and cancels pending Telegram alerts. This cannot be undone.`;
    showError("#delete-error",""); $("#delete-dialog").showModal(); return;
  }
  action.disabled = true;
  try {
    if (action.dataset.action === "toggle") {await api(`/api/watches/${watch.id}`,{method:"PATCH",body:JSON.stringify({active:!watch.active})}); toast(watch.active ? "Search paused. Pending alerts cancelled; saved results kept." : "Search resumed. Listings added while paused will be saved without alerts.");}
    if (action.dataset.action === "check") {await api(`/api/watches/${watch.id}/check`,{method:"POST"}); toast("Check queued. Results will refresh automatically.");}
    await load();
  } catch(error) {toast(error.message);} finally {action.disabled = false;}
});
async function startSession() {
  state.user = await api("/api/me");
  clearInterval(state.challengeTimer); clearTimeout(state.loginPoll); state.challenge = null;
  $("#user-name").textContent = "@" + state.user.username; $("#user-name").title = "@" + state.user.username;
  $("#avatar").textContent = state.user.username.slice(0,2).toUpperCase(); $("#logout").hidden = false;
  if ($("#login-dialog").open) $("#login-dialog").close();
  $("#results").innerHTML = '<div class="loading" role="status">Loading your searches…</div>'; await load();
}
async function init() {
  renderItems();
  try {await refreshToken(); await startSession();}
  catch {showLogin(); raw("/api/config").then(config => {if(config.error) showError("#login-error",config.error);}).catch(error => showError("#login-error",error.message));}
  setInterval(() => {if(state.user && !document.hidden) load();},15000);
  document.addEventListener("visibilitychange",() => {if (!document.hidden && state.user) load();});
}
init();
