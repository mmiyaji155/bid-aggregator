// app.js — gov-bid MVP 最小 JS（プログレッシブエンハンスメント。JS無効でもフォーム送信は機能する）
(function () {
  "use strict";

  // ---------------------------------------------------------------- サイドバー（モバイル off-canvas）
  var sidebar = document.querySelector(".sidebar");
  var toggleBtn = document.querySelector("[data-sidebar-toggle]");
  if (toggleBtn && sidebar) {
    toggleBtn.addEventListener("click", function () {
      var isOpen = sidebar.classList.toggle("is-open");
      toggleBtn.setAttribute("aria-expanded", String(isOpen));
    });
  }

  // ---------------------------------------------------------------- 検索フィルタの自動送信（debounce）
  var filterForm = document.querySelector("[data-filter-form]");
  if (filterForm) {
    var timer = null;
    filterForm.querySelectorAll("input[type=text], input[type=search]").forEach(function (input) {
      input.addEventListener("input", function () {
        clearTimeout(timer);
        timer = setTimeout(function () { filterForm.submit(); }, 300);
      });
    });
    filterForm.querySelectorAll("select").forEach(function (select) {
      select.addEventListener("change", function () { filterForm.submit(); });
    });
  }

  // ---------------------------------------------------------------- ボードのステータス変更（select即時submit）
  document.querySelectorAll("[data-status-select]").forEach(function (select) {
    select.addEventListener("change", function () {
      select.closest("form").submit();
    });
  });

  // ---------------------------------------------------------------- 送信ボタンの loading 状態
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function () {
      var btn = form.querySelector("button[type=submit]");
      if (btn && !btn.disabled) {
        btn.classList.add("is-loading");
        btn.setAttribute("aria-disabled", "true");
      }
    });
  });

  // ---------------------------------------------------------------- 汎用ダイアログ制御（APG modal: focus trap / Escape / トリガー復帰）
  // .scrim > .dialog 構造の要素であれば、動的生成・静的マークアップの両方で使い回せる。
  function getFocusables(dialogEl) {
    return dialogEl.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
  }

  function openDialog(scrimEl, triggerEl, initialFocusEl) {
    scrimEl.removeAttribute("hidden");
    var focusables = getFocusables(scrimEl);
    var toFocus = initialFocusEl || focusables[0];
    if (toFocus) toFocus.focus();

    function onKeydown(e) {
      if (e.key === "Escape") { e.preventDefault(); closeDialog(scrimEl, triggerEl); return; }
      if (e.key === "Tab") {
        var first = focusables[0], last = focusables[focusables.length - 1];
        if (!first || !last) return;
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    scrimEl._onKeydown = onKeydown;
    scrimEl._trigger = triggerEl;
    document.addEventListener("keydown", onKeydown);

    if (!scrimEl._outsideClickBound) {
      scrimEl.addEventListener("click", function (e) {
        if (e.target === scrimEl) closeDialog(scrimEl, scrimEl._trigger);
      });
      scrimEl._outsideClickBound = true;
    }
  }

  function closeDialog(scrimEl, triggerEl) {
    var isDynamic = scrimEl.parentNode === document.body && scrimEl.hasAttribute("data-dynamic-dialog");
    if (isDynamic) {
      scrimEl.parentNode.removeChild(scrimEl);
    } else {
      scrimEl.setAttribute("hidden", "");
    }
    if (scrimEl._onKeydown) document.removeEventListener("keydown", scrimEl._onKeydown);
    var focusTarget = triggerEl || scrimEl._trigger;
    if (focusTarget) focusTarget.focus();
  }

  // 静的マークアップのダイアログ（例: quick-add-dialog）を data 属性で開閉する
  document.querySelectorAll("[data-dialog-open]").forEach(function (trigger) {
    trigger.addEventListener("click", function () {
      var dlg = document.getElementById(trigger.getAttribute("data-dialog-open"));
      if (dlg) openDialog(dlg, trigger);
    });
  });
  document.querySelectorAll("[data-dialog-close]").forEach(function (trigger) {
    trigger.addEventListener("click", function () {
      var dlg = trigger.closest(".scrim");
      if (dlg) closeDialog(dlg, dlg._trigger);
    });
  });

  // ---------------------------------------------------------------- 確認ダイアログ（動的生成 + 危険度分離）
  // data-confirm-severity="soft"（可逆操作。例: 見送り） | "destructive"（既定・不可逆。例: 削除）
  document.querySelectorAll("[data-confirm]").forEach(function (trigger) {
    trigger.addEventListener("click", function (e) {
      e.preventDefault();
      openConfirmDialog(trigger);
    });
  });

  function openConfirmDialog(trigger) {
    var title = trigger.getAttribute("data-confirm-title") || "確認";
    var message = trigger.getAttribute("data-confirm") || "実行しますか？";
    var confirmLabel = trigger.getAttribute("data-confirm-label") || "実行する";
    var needsReason = trigger.hasAttribute("data-confirm-reason");
    var severity = trigger.getAttribute("data-confirm-severity") || "destructive";
    var confirmBtnClass = severity === "soft" ? "btn-secondary" : "btn-destructive";

    var scrim = document.createElement("div");
    scrim.className = "scrim";
    scrim.setAttribute("data-dynamic-dialog", "");
    scrim.innerHTML =
      '<div class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title">' +
      '<div class="dialog__title" id="dlg-title">' + title + "</div>" +
      "<div>" + message + "</div>" +
      (needsReason
        ? '<div class="field"><label for="dlg-reason">理由（任意）</label>' +
          '<textarea class="input" id="dlg-reason" name="reason"></textarea></div>'
        : "") +
      '<div class="dialog__actions">' +
      '<button type="button" class="btn btn-ghost" data-dlg-cancel>キャンセル</button>' +
      '<button type="button" class="btn ' + confirmBtnClass + '" data-dlg-confirm>' + confirmLabel + "</button>" +
      "</div></div>";
    document.body.appendChild(scrim);

    var cancelBtn = scrim.querySelector("[data-dlg-cancel]");
    var confirmBtn = scrim.querySelector("[data-dlg-confirm]");
    var reasonInput = scrim.querySelector("#dlg-reason");

    openDialog(scrim, trigger, cancelBtn); // APG: 初期フォーカス=キャンセル（破壊的操作の誤操作防止）

    cancelBtn.addEventListener("click", function () { closeDialog(scrim, trigger); });
    confirmBtn.addEventListener("click", function () {
      var targetForm = trigger.form || trigger.closest("form");
      if (reasonInput) {
        var hiddenInput = targetForm.querySelector("input[name=reason]");
        if (hiddenInput) hiddenInput.value = reasonInput.value;
      }
      closeDialog(scrim, trigger);
      targetForm.submit();
    });
  }

  // ---------------------------------------------------------------- 案件一覧: 複数選択→一括操作
  var bulkTable = document.querySelector("[data-bulk-table]");
  var bulkBar = document.querySelector("[data-bulk-bar]");
  var bulkCount = document.querySelector("[data-bulk-count]");
  var selectAll = document.querySelector("[data-select-all]");
  if (bulkTable && bulkBar && bulkCount) {
    function rowCheckboxes() {
      return Array.prototype.slice.call(bulkTable.querySelectorAll("[data-row-select]"));
    }
    function refreshBulkBar() {
      var checked = rowCheckboxes().filter(function (cb) { return cb.checked; });
      bulkCount.textContent = checked.length + " 件選択中";
      bulkBar.hidden = checked.length === 0;
      if (selectAll) {
        var all = rowCheckboxes();
        selectAll.checked = all.length > 0 && checked.length === all.length;
        selectAll.indeterminate = checked.length > 0 && checked.length < all.length;
      }
    }
    bulkTable.addEventListener("change", function (e) {
      if (e.target.matches("[data-row-select]")) refreshBulkBar();
    });
    if (selectAll) {
      selectAll.addEventListener("change", function () {
        rowCheckboxes().forEach(function (cb) { cb.checked = selectAll.checked; });
        refreshBulkBar();
      });
    }
    refreshBulkBar();
  }

  // ---------------------------------------------------------------- トースト（?toast=... クエリを読んで表示 → URL清掃）
  var params = new URLSearchParams(window.location.search);
  var toastMsg = params.get("toast");
  if (toastMsg) {
    var region = document.querySelector(".toast-region") || (function () {
      var r = document.createElement("div");
      r.className = "toast-region";
      r.setAttribute("role", "status");
      document.body.appendChild(r);
      return r;
    })();
    var toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = decodeURIComponent(toastMsg);
    region.appendChild(toast);
    setTimeout(function () { toast.remove(); }, 4000);
    params.delete("toast");
    var newUrl = window.location.pathname + (params.toString() ? "?" + params.toString() : "");
    window.history.replaceState({}, "", newUrl);
  }
})();
