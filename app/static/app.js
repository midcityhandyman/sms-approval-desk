/* Approval Desk frontend. Plain JavaScript, no build step.
   All user supplied text is written with textContent, never innerHTML. */
(function () {
  "use strict";

  var TABS = [
    { key: "pending", label: "Pending" },
    { key: "sent", label: "Sent" },
    { key: "failed", label: "Failed" },
    { key: "rejected", label: "Rejected" },
    { key: "", label: "All" }
  ];

  var state = {
    initData: "",
    devUser: 1001,
    isDev: false,
    me: null,
    tab: "pending",
    drafts: [],
    counts: {},
    open: {},
    busy: false
  };

  var $ = function (id) { return document.getElementById(id); };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  // ---------------------------------------------------------------- API

  function api(path, options) {
    options = options || {};
    var headers = { Authorization: "tma " + state.initData };
    if (options.body) headers["Content-Type"] = "application/json";
    return fetch(path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var info = data.error || {};
          var error = new Error(info.message || "Request failed (" + response.status + ")");
          error.code = info.code || "";
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  // ------------------------------------------------------------ sign in

  function telegramApp() {
    return window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  }

  function signIn() {
    var tg = telegramApp();
    if (tg && tg.initData) {
      tg.ready();
      tg.expand();
      state.initData = tg.initData;
      return Promise.resolve();
    }
    // Not inside Telegram. In dev the server will sign init data for a fake
    // reviewer. In prod this endpoint is a 404 and the page stays locked.
    return fetch("/api/dev/init-data?user=" + state.devUser).then(function (response) {
      if (!response.ok) throw new Error("Open this page from the Telegram bot.");
      return response.json();
    }).then(function (data) {
      state.initData = data.init_data;
      state.isDev = true;
    });
  }

  // ------------------------------------------------------------- render

  function toast(message, isError) {
    var node = $("toast");
    node.textContent = message;
    node.className = "toast" + (isError ? " error" : "");
    node.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(function () { node.hidden = true; }, isError ? 5000 : 2500);
    var tg = telegramApp();
    if (tg && tg.HapticFeedback) {
      tg.HapticFeedback.notificationOccurred(isError ? "error" : "success");
    }
  }

  function renderTabs() {
    var nav = $("tabs");
    nav.replaceChildren();
    TABS.forEach(function (tab) {
      var button = el("button", "tab", tab.label);
      button.type = "button";
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(state.tab === tab.key));
      if (tab.key) button.appendChild(el("span", "count", String(state.counts[tab.key] || 0)));
      button.addEventListener("click", function () {
        state.tab = tab.key;
        refresh();
      });
      nav.appendChild(button);
    });
  }

  function renderWho() {
    var button = $("who");
    button.textContent = state.me ? state.me.name + (state.isDev ? " (dev)" : "") : "Not signed in";
    button.disabled = !state.isDev;
    button.title = state.isDev ? "Tap to switch between the two dev reviewers" : "Signed in reviewer";
  }

  function historyList(detail) {
    var list = el("ul", "history");
    var bodies = {};
    detail.versions.forEach(function (version) { bodies[version.version_no] = version.body; });
    detail.events.forEach(function (event) {
      var item = el("li");
      item.appendChild(el("b", null, event.actor_name));
      var text = " " + event.action.replace("_", " ") + " v" + event.version_no;
      if (event.note) text += " (" + event.note + ")";
      item.appendChild(document.createTextNode(text + " at " + event.created_at.replace("T", " ").replace("Z", " UTC")));
      if ((event.action === "created" || event.action === "edited") && event.version_no !== detail.current_version) {
        item.appendChild(el("div", "old", bodies[event.version_no]));
      }
      list.appendChild(item);
    });
    return list;
  }

  function card(draft) {
    var node = el("article", "card");

    var head = el("div", "card-head");
    var who = el("div");
    who.appendChild(el("div", "name", draft.recipient_name));
    who.appendChild(el("div", "sub", draft.recipient_phone + (draft.purpose ? " · " + draft.purpose : "")));
    head.appendChild(who);
    head.appendChild(el("span", "badge " + draft.status, draft.status));
    node.appendChild(head);

    node.appendChild(el("div", "bubble", draft.body));

    var meta = el("div", "meta");
    meta.appendChild(el("span", null, "Version " + draft.current_version + " by " + draft.body_author));
    meta.appendChild(el("span", null, draft.body.length + " chars, " + draft.segments.segments +
      (draft.segments.segments === 1 ? " segment" : " segments") + " (" + draft.segments.encoding + ")"));
    node.appendChild(meta);

    var toggle = el("button", "btn link", state.open[draft.id] ? "Hide history" : "Show history");
    toggle.type = "button";
    toggle.addEventListener("click", function () {
      if (state.open[draft.id]) {
        delete state.open[draft.id];
        renderList();
        return;
      }
      api("/api/drafts/" + draft.id).then(function (detail) {
        state.open[draft.id] = detail;
        renderList();
      }).catch(showError);
    });
    node.appendChild(toggle);
    if (state.open[draft.id]) node.appendChild(historyList(state.open[draft.id]));

    var actions = el("div", "actions");
    function action(label, className, handler) {
      var button = el("button", "btn " + className, label);
      button.type = "button";
      button.disabled = state.busy;
      button.addEventListener("click", handler);
      actions.appendChild(button);
    }
    if (draft.status === "pending") {
      action("Reject", "danger", function () { openSheet("reject", draft); });
      action("Edit", "", function () { openSheet("edit", draft); });
      action("Approve", "primary", function () {
        act(draft, "approve", { version: draft.current_version }, "Approved and handed to the sender");
      });
    } else if (draft.status === "failed") {
      actions.classList.add("double");
      action("Edit", "", function () { openSheet("edit", draft); });
      action("Retry send", "primary", function () { act(draft, "retry", null, "Sent"); });
    } else if (draft.status === "approved") {
      // Approved but never handed to the sender (the server stopped in between).
      actions.classList.add("single");
      action("Send now", "primary", function () { act(draft, "retry", null, "Sent"); });
    } else if (draft.status === "rejected") {
      actions.classList.add("single");
      action("Edit and resubmit", "", function () { openSheet("edit", draft); });
    }
    if (actions.children.length) node.appendChild(actions);
    return node;
  }

  function renderList() {
    var list = $("list");
    list.replaceChildren();
    if (!state.drafts.length) {
      list.appendChild(el("p", "empty", state.tab === "pending" ? "Nothing waiting for approval." : "Nothing here."));
      return;
    }
    state.drafts.forEach(function (draft) { list.appendChild(card(draft)); });
  }

  // ------------------------------------------------------------ actions

  function showError(error) {
    toast(error.message || "Something went wrong", true);
    // A stale version or a state conflict means our copy is out of date.
    if (error.status === 409) refresh();
  }

  function refresh() {
    var query = state.tab ? "?status=" + encodeURIComponent(state.tab) : "";
    return api("/api/drafts" + query).then(function (data) {
      state.drafts = data.drafts;
      state.counts = data.counts;
      state.open = {};
      renderTabs();
      renderList();
    }).catch(showError);
  }

  function act(draft, verb, body, successMessage) {
    if (state.busy) return Promise.resolve();
    state.busy = true;
    renderList();
    return api("/api/drafts/" + draft.id + "/" + verb, { method: "POST", body: body || {} })
      .then(function (updated) {
        var failed = updated.status === "failed";
        toast(failed ? "Approved, but the send failed. See the Failed tab." : successMessage, failed);
      })
      .catch(showError)
      .then(function () {
        state.busy = false;
        return refresh();
      });
  }

  // -------------------------------------------------------------- sheet

  var sheet = { mode: "", draft: null };

  function updateMeter() {
    var meter = $("sheet-meter");
    var length = $("sheet-text").value.trim().length;
    var limit = sheet.mode === "edit" ? (state.me ? state.me.max_body_chars : 480) : 200;
    meter.textContent = length + " / " + limit;
    meter.className = "meter" + (length > limit ? " over" : "");
    $("sheet-submit").disabled = length > limit || (sheet.mode === "edit" && length === 0);
  }

  function openSheet(mode, draft) {
    sheet.mode = mode;
    sheet.draft = draft;
    var text = $("sheet-text");
    if (mode === "edit") {
      $("sheet-title").textContent = "Edit text for " + draft.recipient_name;
      $("sheet-hint").textContent = "Saving creates version " + (draft.current_version + 1) +
        ". It goes back to Pending and needs approval before it is sent.";
      text.value = draft.body;
      text.placeholder = "";
      $("sheet-submit").textContent = "Save new version";
    } else {
      $("sheet-title").textContent = "Reject text for " + draft.recipient_name;
      $("sheet-hint").textContent = "Nothing is sent. The reason is kept in the history.";
      text.value = "";
      text.placeholder = "Reason (optional)";
      $("sheet-submit").textContent = "Reject";
    }
    $("sheet").hidden = false;
    updateMeter();
    text.focus();
  }

  function closeSheet() { $("sheet").hidden = true; }

  function submitSheet(event) {
    event.preventDefault();
    var draft = sheet.draft;
    var value = $("sheet-text").value.trim();
    closeSheet();
    if (sheet.mode === "edit") {
      act(draft, "edit", { body: value, base_version: draft.current_version },
        "Saved as version " + (draft.current_version + 1) + ". Needs approval.");
    } else {
      act(draft, "reject", { version: draft.current_version, reason: value }, "Rejected");
    }
  }

  // --------------------------------------------------------------- boot

  function loadMe() {
    return api("/api/me").then(function (me) {
      state.me = me;
      state.counts = me.counts;
      renderWho();
    });
  }

  function start() {
    renderTabs();
    signIn().then(loadMe).then(refresh).catch(function (error) {
      $("list").replaceChildren(el("p", "empty", error.message || "Could not sign in."));
      renderWho();
    });
  }

  $("sheet-form").addEventListener("submit", submitSheet);
  $("sheet-text").addEventListener("input", updateMeter);
  $("sheet").addEventListener("click", function (event) {
    if (event.target.hasAttribute("data-close")) closeSheet();
  });
  $("who").addEventListener("click", function () {
    if (!state.isDev) return;
    state.devUser = state.devUser === 1001 ? 1002 : 1001;
    signIn().then(loadMe).then(refresh).catch(showError);
  });

  // The Telegram bridge script is async. Wait for it (it also delays the load
  // event), but never longer than 2.5 seconds so an offline laptop still works.
  var started = false;
  function startOnce() {
    if (started) return;
    started = true;
    start();
  }
  if (document.readyState === "complete") startOnce();
  else window.addEventListener("load", startOnce);
  setTimeout(startOnce, 2500);
})();
