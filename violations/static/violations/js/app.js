(function () {
    "use strict";

    const monitorRoot = document.getElementById("messenger-shell") || document.getElementById("monitor-tabs-content");
    const incidentFeed = document.getElementById("incident-feed");
    const incidentTopStatus = document.getElementById("incident-top-status");
    const incidentListContainer = document.getElementById("incident-list-container");
    const composerForm = document.getElementById("create-incident-form") || document.querySelector(".composer-form");
    const composerSubmitButton = composerForm ? composerForm.querySelector('button[type="submit"]') : null;
    // Prefer the unified bottom-bar stack on the chatbox page — its
    // measured height changes when the composer / action-bar / busy
    // notice swap, so ``--composer-offset`` (which drives the chat
    // list's bottom padding) stays correct in every state. Fall back
    // to the standalone composer / dock for pages that don't render
    // the stack.
    const composerDock = document.querySelector(".bottom-bar-stack")
        || document.querySelector(".compose-bar")
        || document.querySelector(".composer-dock");
    const statsTableContainer = document.getElementById("stats-table-container");
    const liveConnectionStatus = document.getElementById("live-connection-status");
    const detailContent = document.getElementById("candidate-detail-content");
    const detailCanvasEl = document.getElementById("candidateDetailCanvas");
    const detailCanvas = detailCanvasEl ? new bootstrap.Offcanvas(detailCanvasEl) : null;
    const is_chatbox_page = Boolean(document.getElementById("chatbox-page-indicator"));

    let liveSocket = null;
    let reconnectDelayMs = 1000;
    let loadingOlder = false;
    let loadingUpdates = false;
    let sendingComposer = false;
    let setComposeExpanded = null;
    let refreshComposeEvidenceIndicator = null;
    // When true, allow navigation without showing the native beforeunload prompt
    let __allowUnload = false;
    // refs to SBD validators (filled during init)
    let composeSbdValidate = null;
    let editSbdValidate = null;

    const PREVIEW_URL = "/incidents/preview/";
    const UPLOAD_URL = "/incidents/upload-image/";

    // ── Incident bulk-select limits ──────────────────────────────────────
    // Mirrors the server-side `INCIDENT_BULK_DELETE_MAX` constant. Kept as
    // a shared JS constant so the master-checkbox cap, the manual-tick
    // cap, and the user-facing error text all reference the same number.
    // If the server ever changes its cap, both halves should move together.
    const INCIDENT_BULK_DELETE_MAX = 50;
    const INCIDENT_BULK_OVER_LIMIT_MSG = `Không thể thao tác nhiều hơn ${INCIDENT_BULK_DELETE_MAX} tin nhắn.`;

    // Local lock identity. Set to the current user's id by `initAppBootstrap`
    // (read from <body data-user-id="...">) so the lock-event handler can
    // tell apart "I'm the holder" from "someone else is the holder" without
    // round-tripping through the server.
    let CURRENT_USER_ID = null;

    // Active lock-state snapshot. Updated by every `candidates_lock` ws
    // event so any component that needs to know whether a mutation is in
    // flight (e.g. to grey out a button) can ask without re-broadcasting.
    let candidatesLockState = { busy: false, owner_user_id: null, owner_username: "", operation: "" };

    // Mirror snapshot for the incident bulk-delete lock. Used both to
    // gate composer submits client-side AND to swap the bottom bar to
    // the "đang xoá nhiều tin nhắn" notice for non-owner viewers.
    let incidentsLockState = { busy: false, owner_user_id: null, owner_username: "", operation: "" };

    // Filled in by ``initIncidentBulkSelect`` once the chatbox page is
    // ready. Lets the WS / DOM-change handlers ask the bulk-select module
    // to re-sync its master indicator after rows arrive or get removed,
    // without having to import private helpers across module scope.
    let incidentBulkSelectApi = null;

    // Listeners that want to react to candidate row mutations broadcast by
    // the server (e.g. cache invalidation, stats refresh). Registered via
    // `onCandidatesChanged(handler)`.
    const candidatesChangedListeners = new Set();

    function onCandidatesChanged(handler) {
        if (typeof handler !== "function") return () => {};
        candidatesChangedListeners.add(handler);
        return () => candidatesChangedListeners.delete(handler);
    }

    function dispatchCandidatesChanged(payload) {
        candidatesChangedListeners.forEach((fn) => {
            try { fn(payload); } catch (err) { console.debug("candidatesChanged handler failed:", err); }
        });
    }

    function parseId(value) {
        const parsed = Number.parseInt(value, 10);
        return Number.isFinite(parsed) ? parsed : null;
    }

    let oldestId = incidentListContainer ? parseId(incidentListContainer.dataset.oldestId) : null;
    let newestId = incidentListContainer ? parseId(incidentListContainer.dataset.newestId) : null;
    let hasOlder = incidentListContainer ? incidentListContainer.dataset.hasOlder === "1" : false;

    function updateConnectionStatus(text) {
        if (liveConnectionStatus) liveConnectionStatus.textContent = text || "";
    }

    function updateTopStatus(text) {
        if (incidentTopStatus) incidentTopStatus.textContent = text || "";
    }

    function setComposerSubmittingState(isSubmitting) {
        if (composerSubmitButton) composerSubmitButton.disabled = isSubmitting;
    }

    function formatFileSize(bytes) {
        if (!Number.isFinite(bytes) || bytes <= 0) return "";
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    }

    // Client-side SBD syntax check (matches server's is_valid_sbd_syntax)
    const SBD_SYNTAX_RE = /^[A-Za-z0-9]{1,9}$/;
    function isValidSbdSyntax(value) {
        return SBD_SYNTAX_RE.test(String(value || "").trim());
    }

    function attachSbdValidation(inputEl) {
        if (!inputEl) return null;
        const feedback = document.getElementById('sbd-error');

        // On the chatbox page we deliberately don't render the detailed
        // message under the input (space-constrained compose bar). The
        // detailed reason surfaces via a toast on submit instead; while
        // typing, only the red border signals invalidity.
        const inlineFeedbackEnabled = Boolean(feedback) && !is_chatbox_page;

        const setInvalid = (msg) => {
            inputEl.classList.add('is-invalid');
            inputEl.classList.remove('is-valid');
            if (feedback) feedback.textContent = inlineFeedbackEnabled ? (msg || 'SBD không hợp lệ.') : '';
        };
        const setValid = () => {
            inputEl.classList.remove('is-invalid');
            inputEl.classList.add('is-valid');
            if (feedback) feedback.textContent = '';
        };
        const clear = () => {
            inputEl.classList.remove('is-invalid', 'is-valid');
            if (feedback) feedback.textContent = '';
            validate.lastError = null;
        };

        function validate() {
            const v = (inputEl.value || '').trim();
            if (!v) {
                const msg = 'SBD không được để trống.';
                setInvalid(msg);
                validate.lastError = msg;
                return false;
            }
            if (!isValidSbdSyntax(v)) {
                const msg = 'SBD phải từ 1 đến 9 ký tự, chỉ gồm chữ cái và chữ số.';
                setInvalid(msg);
                validate.lastError = msg;
                return false;
            }
            setValid();
            validate.lastError = null;
            return true;
        }
        validate.lastError = null;
        validate.clear = clear;

        inputEl.addEventListener('input', () => {
            // Live validate but quietly: empty input resets to neutral.
            if (inputEl.value === '') {
                clear();
                return;
            }
            validate();
        });

        // Return the validator; callers can also use .clear() / .lastError.
        return validate;
    }

    function getCsrfToken() {
        const el = document.querySelector("input[name=csrfmiddlewaretoken]");
        if (el) return el.value;
        const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : "";
    }

    function escHtml(value) {
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/\"/g, "&quot;");
    }

    function formatLocalTimestamps(scope) {
        const root = scope || document;
        const pad = (n) => String(n).padStart(2, "0");
        root.querySelectorAll("time.js-local-time").forEach((el) => {
            if (el.dataset.localized === "1") return;
            const iso = el.getAttribute("datetime");
            if (!iso) return;
            const d = new Date(iso);
            if (Number.isNaN(d.getTime())) return;
            const text = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
            el.textContent = text;
            try {
                const tzName = Intl.DateTimeFormat().resolvedOptions().timeZone;
                el.title = tzName ? `${text} (${tzName})` : text;
            } catch (_) {
                el.title = text;
            }
            el.dataset.localized = "1";
        });
    }

    function showConfirmDialog(options) {
        const dialog = document.getElementById("appConfirmDialog");
        if (!dialog) {
            // Fallback to native confirm if the dialog is missing.
            return Promise.resolve(window.confirm(options?.message || "Xác nhận?"));
        }
        const titleEl = dialog.querySelector(".app-confirm-title");
        const messageEl = dialog.querySelector(".app-confirm-message");
        const okBtn = dialog.querySelector(".app-confirm-ok");
        const cancelBtn = dialog.querySelector(".app-confirm-cancel");

        if (titleEl) titleEl.textContent = options?.title || "Xác nhận";
        if (messageEl) messageEl.textContent = options?.message || "Bạn có chắc chắn muốn thực hiện?";
        if (okBtn) {
            okBtn.innerHTML = `<i class="bi bi-trash me-1"></i>${options?.okText || "Xoá"}`;
            okBtn.classList.remove("btn-danger", "btn-dark");
            okBtn.classList.add(options?.variant === "dark" ? "btn-dark" : "btn-danger");
        }
        if (cancelBtn) cancelBtn.textContent = options?.cancelText || "Huỷ";

        return new Promise((resolve) => {
            const cleanup = (result) => {
                dialog.removeEventListener("click", onDialogClick);
                document.removeEventListener("keydown", onKeyDown);
                okBtn?.removeEventListener("click", onOk);
                dialog.setAttribute("hidden", "");
                dialog.setAttribute("aria-hidden", "true");
                dialog.classList.remove("is-open");
                resolve(result);
            };
            const onOk = () => cleanup(true);
            const onDialogClick = (event) => {
                if (event.target.closest("[data-confirm-close]")) cleanup(false);
            };
            const onKeyDown = (event) => {
                if (event.key === "Escape") cleanup(false);
                else if (event.key === "Enter") { event.preventDefault(); cleanup(true); }
            };
            okBtn?.addEventListener("click", onOk);
            dialog.addEventListener("click", onDialogClick);
            document.addEventListener("keydown", onKeyDown);

            dialog.removeAttribute("hidden");
            dialog.setAttribute("aria-hidden", "false");
            dialog.classList.add("is-open");
            requestAnimationFrame(() => okBtn?.focus());
        });
    }

    function showToast(message, variant) {
        const container = document.querySelector(".toast-container");
        if (!container || typeof bootstrap === "undefined") {
            updateTopStatus(message);
            return;
        }

        const el = document.createElement("div");
        el.className = `toast app-toast align-items-center text-bg-${variant || "danger"} border-0`;
        el.setAttribute("role", "alert");
        el.innerHTML = `
            <div class="d-flex">
              <div class="toast-body">${escHtml(message)}</div>
                            <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Đóng"></button>
            </div>`;
        container.appendChild(el);
        new bootstrap.Toast(el, { delay: 2800 }).show();
        el.addEventListener("hidden.bs.toast", () => el.remove());
    }

    function bindEvidenceGuards(scope) {
        (scope || document).querySelectorAll(".evidence-guard").forEach((el) => {
            el.setAttribute("draggable", "false");
            el.addEventListener("dragstart", (event) => event.preventDefault());
            el.addEventListener("contextmenu", (event) => event.preventDefault());
        });
    }

    function isEvidenceMediaReady(mediaEl) {
        if (!mediaEl) return true;
        if (mediaEl.tagName === "IMG") {
            return mediaEl.complete && mediaEl.naturalWidth > 0;
        }
        if (mediaEl.tagName === "VIDEO") {
            return mediaEl.readyState >= 1;
        }
        return true;
    }

    function bindEvidencePlaceholders(scope) {
        const root = scope || document;
        root.querySelectorAll(".evidence-wrap").forEach((wrapEl) => {
            const mediaEl = wrapEl.querySelector(".js-evidence-media");
            if (!mediaEl) {
                wrapEl.classList.remove("evidence-wrap-loading");
                return;
            }

            const revealMedia = () => {
                wrapEl.classList.remove("evidence-wrap-loading");
            };

            if (isEvidenceMediaReady(mediaEl)) {
                revealMedia();
                return;
            }

            if (mediaEl.dataset.placeholderBound === "1") {
                return;
            }

            mediaEl.dataset.placeholderBound = "1";
            const readyEvent = mediaEl.tagName === "VIDEO" ? "loadedmetadata" : "load";
            mediaEl.addEventListener(readyEvent, revealMedia, { once: true });
            mediaEl.addEventListener("error", revealMedia, { once: true });
        });
    }

    function buildGalleryItems(rootEl) {
        const items = [];
        const seen = new Set();

        rootEl.querySelectorAll(".markdown-body img, .incident-legacy-img").forEach((img) => {
            const src = img.dataset.lgSrc || img.src;
            if (!src || seen.has(src)) return;
            seen.add(src);
            items.push({ src, thumb: src });
        });

        rootEl.querySelectorAll(".incident-video-wrap").forEach((wrap) => {
            const videoSrc = wrap.dataset.videoSrc;
            if (!videoSrc || seen.has(videoSrc)) return;
            seen.add(videoSrc);
            items.push({
                video: {
                    source: [{ src: videoSrc, type: "video/mp4" }],
                    attributes: { preload: "metadata", controls: true },
                },
                thumb: "",
                subHtml: "<p>Video bằng chứng</p>",
            });
        });

        return items;
    }

    function openLightGallery(items, index) {
        if (!items.length || typeof lightGallery === "undefined") return;

        const container = document.createElement("div");
        container.style.display = "none";
        document.body.appendChild(container);

        const plugins = [];
        if (typeof lgZoom !== "undefined") plugins.push(lgZoom);
        if (typeof lgVideo !== "undefined") plugins.push(lgVideo);

        const lg = lightGallery(container, {
            plugins,
            dynamic: true,
            dynamicEl: items,
            index,
            licenseKey: "0000-0000-000-0000",
            speed: 320,
            mobileSettings: { controls: true, showCloseIcon: true, download: false },
            download: false,
            zoom: true,
            showZoomInOutIcons: true,
        });

        requestAnimationFrame(() => lg.openGallery(index));
        container.addEventListener("lgAfterClose", () => {
            lg.destroy();
            container.remove();
        }, { once: true });
    }

    function bindLightGallery(scope) {
        const root = scope || document;

        root.querySelectorAll(".markdown-body img, .incident-legacy-img").forEach((img) => {
            if (img.dataset.lgBound) return;
            img.dataset.lgBound = "1";
            img.addEventListener("click", () => {
                const incidentEl = img.closest(".incident-item, .incident-mini, .candidate-detail-shell") || document;
                const items = buildGalleryItems(incidentEl);
                const src = img.dataset.lgSrc || img.src;
                const idx = items.findIndex((it) => it.src === src);
                openLightGallery(items, Math.max(0, idx));
            });
        });

        root.querySelectorAll(".incident-video-wrap").forEach((wrap) => {
            if (wrap.dataset.lgBound) return;
            wrap.dataset.lgBound = "1";
            wrap.addEventListener("click", () => {
                const incidentEl = wrap.closest(".incident-item, .incident-mini, .candidate-detail-shell") || document;
                const items = buildGalleryItems(incidentEl);
                const src = wrap.dataset.videoSrc;
                const idx = items.findIndex((it) => it.video && it.video.source && it.video.source[0].src === src);
                openLightGallery(items, Math.max(0, idx));
            });
        });
    }

    function syncComposerOffset() {
        if (!composerDock) return;
        const composerHeight = composerDock.getBoundingClientRect().height;
        // Add a small breathing space so the last bubble never touches the fixed compose bar.
        document.documentElement.style.setProperty("--composer-offset", `${Math.ceil(composerHeight + 12)}px`);
    }

    function isNearBottom() {
        const doc = document.documentElement;
        return (doc.scrollHeight - (window.scrollY + window.innerHeight)) < 96;
    }

    function scrollToBottom() {
        syncComposerOffset();
        const doc = document.documentElement;
        doc.style.scrollBehavior = "auto";
        window.scrollTo(0, Math.max(doc.scrollHeight - window.innerHeight, 0));
        doc.style.scrollBehavior = "";
    }

    function forceInitialBottomScroll() {
        if (!monitorRoot) return;

        // Keep browser from restoring old scroll position when this page is reloaded.
        if (window.history && "scrollRestoration" in window.history) {
            window.history.scrollRestoration = "manual";
        }

        const syncBottom = () => scrollToBottom();
        syncBottom();
        requestAnimationFrame(syncBottom);
        window.setTimeout(syncBottom, 0);
        window.setTimeout(syncBottom, 180);

        if (document.readyState !== "complete") {
            window.addEventListener("load", syncBottom, { once: true });
        }
    }

    function htmlToNodes(html) {
        const wrapper = document.createElement("div");
        wrapper.innerHTML = html || "";
        return Array.from(wrapper.children).filter((node) => !node.classList.contains("empty-state"));
    }

    function getNodeIncidentId(node) {
        if (!node || node.nodeType !== 1) return null;
        if (node.matches(".chat-row[data-incident-id]")) {
            return parseId(node.dataset.incidentId);
        }
        const row = node.querySelector(".chat-row[data-incident-id]");
        return row ? parseId(row.dataset.incidentId) : null;
    }

    function filterOutExistingIncidentNodes(nodes) {
        if (!incidentListContainer || !nodes.length) return nodes;

        return nodes.filter((node) => {
            const incidentId = getNodeIncidentId(node);
            if (!Number.isFinite(incidentId)) return true;
            return !incidentListContainer.querySelector(`.chat-row[data-incident-id='${incidentId}']`);
        });
    }

    function removeEmptyState() {
        if (!incidentListContainer) return;
        const emptyState = incidentListContainer.querySelector(".empty-state");
        if (emptyState) emptyState.remove();
    }

    function ensureEmptyState() {
        if (!incidentListContainer) return;
        const hasRows = incidentListContainer.querySelector(".chat-row");
        const hasEmptyState = incidentListContainer.querySelector(".empty-state");
        if (hasRows || hasEmptyState) return;

        const empty = document.createElement("div");
        empty.className = "empty-state text-center py-5";
        empty.innerHTML = '<i class="bi bi-inbox display-6"></i><p class="mb-0 mt-2">Chưa có sự việc nào được ghi nhận.</p>';
        incidentListContainer.appendChild(empty);
    }

    function recomputeIncidentBounds() {
        if (!incidentListContainer) return;
        const rows = incidentListContainer.querySelectorAll(".chat-row[data-incident-id]");
        const ids = Array.from(rows)
            .map((row) => parseId(row.dataset.incidentId))
            .filter((id) => Number.isFinite(id));

        oldestId = ids.length ? Math.min(...ids) : null;
        newestId = ids.length ? Math.max(...ids) : null;

        incidentListContainer.dataset.oldestId = oldestId || "";
        incidentListContainer.dataset.newestId = newestId || "";
    }

    function prependIncidents(html) {
        if (!incidentListContainer) return 0;
        const nodes = filterOutExistingIncidentNodes(htmlToNodes(html));
        if (!nodes.length) return 0;

        removeEmptyState();
        const previousHeight = document.documentElement.scrollHeight;
        const previousTop = window.scrollY;
        nodes.forEach((node) => incidentListContainer.prepend(node));
        bindEvidenceGuards(incidentListContainer);
        nodes.forEach((node) => bindEvidencePlaceholders(node));
        nodes.forEach((node) => bindLightGallery(node));
        nodes.forEach((node) => bindSbdHoverTooltips(node));
        nodes.forEach((node) => formatLocalTimestamps(node));

        const heightDelta = document.documentElement.scrollHeight - previousHeight;
        window.scrollTo(0, previousTop + heightDelta);
        recomputeIncidentBounds();
        // Newly-arrived rows may carry their own ``.js-incident-bulk-cb``
        // checkboxes (deletable). If the user is in selection mode the
        // master indicator must drop from "all" to "indeterminate" so they
        // notice the new arrivals.
        if (incidentBulkSelectApi && typeof incidentBulkSelectApi.notifyDomChanged === "function") {
            incidentBulkSelectApi.notifyDomChanged();
        }
        return nodes.length;
    }

    function appendIncidents(html) {
        if (!incidentListContainer) return 0;
        const nodes = filterOutExistingIncidentNodes(htmlToNodes(html));
        if (!nodes.length) return 0;

        removeEmptyState();
        nodes.forEach((node) => incidentListContainer.append(node));
        bindEvidenceGuards(incidentListContainer);
        nodes.forEach((node) => bindEvidencePlaceholders(node));
        nodes.forEach((node) => bindLightGallery(node));
        nodes.forEach((node) => bindSbdHoverTooltips(node));
        nodes.forEach((node) => formatLocalTimestamps(node));
        recomputeIncidentBounds();
        if (incidentBulkSelectApi && typeof incidentBulkSelectApi.notifyDomChanged === "function") {
            incidentBulkSelectApi.notifyDomChanged();
        }
        return nodes.length;
    }

    function mergeStatsHtml(payload) {
        if (statsTableContainer && payload.stats_html) {
            statsTableContainer.innerHTML = payload.stats_html;
        }
    }

    async function loadOlderMessages() {
        if (loadingOlder || !hasOlder || !monitorRoot || !oldestId) return;

        const historyUrl = monitorRoot.dataset.historyUrl;
        if (!historyUrl) return;

        loadingOlder = true;
        updateTopStatus("Đang tải các tin nhắn cũ hơn...");

        try {
            const response = await fetch(`${historyUrl}?before=${encodeURIComponent(oldestId)}`, {
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
            if (!response.ok) {
                updateTopStatus("Không thể tải tin nhắn cũ hơn.");
                return;
            }

            const payload = await response.json();
            prependIncidents(payload.incidents_html);
            if (payload.oldest_id) oldestId = payload.oldest_id;
            if (newestId === null && payload.newest_id) newestId = payload.newest_id;
            hasOlder = Boolean(payload.has_older);
            updateTopStatus(!hasOlder ? "Bạn đã đến tin nhắn đầu tiên." : "");
        } catch (_) {
            updateTopStatus("Không thể tải tin nhắn cũ hơn.");
        } finally {
            loadingOlder = false;
        }
    }

    async function loadNewMessages(forceStickBottom) {
        if (loadingUpdates || !monitorRoot) return;

        const updatesUrl = monitorRoot.dataset.updatesUrl;
        if (!updatesUrl) return;

        loadingUpdates = true;
        const shouldStickBottom = forceStickBottom || isNearBottom();

        try {
            const afterId = newestId || 0;
            const response = await fetch(`${updatesUrl}?after=${encodeURIComponent(afterId)}`, {
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
            if (!response.ok) return;

            const payload = await response.json();
            const added = appendIncidents(payload.incidents_html);
            mergeStatsHtml(payload);

            if (payload.newest_id) newestId = payload.newest_id;
            if (oldestId === null && payload.oldest_id) oldestId = payload.oldest_id;

            if (shouldStickBottom && added > 0) scrollToBottom();
        } catch (error) {
            console.debug("Update fetch failed:", error);
        } finally {
            loadingUpdates = false;
        }
    }

    async function handleComposerSubmit(event) {
        event.preventDefault();
        if (!composerForm || sendingComposer) return;

        // Defense in depth: hard-block submits while a bulk-delete is in
        // flight or while the user is in selection mode. The server has
        // its own 409 guard for the busy case, but bouncing here saves a
        // round trip and avoids the user typing into a hidden composer.
        const blockedReason = composerBlockedReason();
        if (blockedReason) {
            showToast(blockedReason, "warning");
            return;
        }

        // client-side SBD validation — surface the detailed reason via the
        // existing toast instead of cramming it under the compose input.
        if (typeof composeSbdValidate === 'function') {
            if (!composeSbdValidate()) {
                showToast(composeSbdValidate.lastError || 'SBD không hợp lệ.', 'danger');
                return;
            }
        }

        sendingComposer = true;
        setComposerSubmittingState(true);
        updateTopStatus("");

        try {
            const response = await fetch(composerForm.action, {
                method: "POST",
                body: new FormData(composerForm),
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });

            const contentType = response.headers.get("content-type") || "";
            const payload = contentType.includes("application/json") ? await response.json() : null;

            if (!response.ok || (payload && payload.ok === false)) {
                const errorText = payload && payload.error ? payload.error : "Không thể gửi tin nhắn.";
                showToast(errorText, "danger");
                return;
            }

            composerForm.reset();
            // form.reset() only clears values — the previous is-valid/
            // is-invalid classes + feedback text would persist against an
            // empty field, which is visually misleading. Reset them too.
            if (composeSbdValidate && typeof composeSbdValidate.clear === "function") {
                composeSbdValidate.clear();
            }
            if (typeof refreshComposeEvidenceIndicator === "function") {
                refreshComposeEvidenceIndicator();
            }
            // Reset the dirty-detection baseline so a subsequent navigation
            // doesn't think the (now-empty) composer is dirty against the
            // pre-submit text.
            if (composerForm && typeof composerForm.__refreshComposerBaseline === "function") {
                // Run on next tick so form.reset() has settled.
                setTimeout(() => composerForm.__refreshComposerBaseline(), 0);
            }
            if (payload && payload.incident_html) {
                appendIncidents(payload.incident_html);
                mergeStatsHtml(payload);
                if (payload.newest_id) newestId = payload.newest_id;
                if (oldestId === null && payload.newest_id) oldestId = payload.newest_id;
                scrollToBottom();
            } else {
                await loadNewMessages(true);
            }

            if (typeof setComposeExpanded === "function") {
                setComposeExpanded(false);
            }
            updateTopStatus("");
        } catch (_) {
            showToast("Không thể gửi tin nhắn.", "danger");
        } finally {
            sendingComposer = false;
            setComposerSubmittingState(false);
        }
    }

    async function deleteIncidentWithoutReload(form) {
        try {
            // NOTE: we intentionally avoid `new FormData(form)` here — when
            // the form contains only the CSRF token, the resulting tiny
            // multipart body has been observed to occasionally trip
            // Daphne's multipart parser and surface as a 400 Bad Request
            // with an empty body (Django middleware swallows
            // MultiPartParserError into HttpResponseBadRequest()). Sending
            // application/x-www-form-urlencoded sidesteps that path
            // entirely. The CSRF token is duplicated in the body as a
            // belt-and-suspenders alongside the X-CSRFToken header.
            const csrf = getCsrfToken();
            const params = new URLSearchParams();
            if (csrf) params.append("csrfmiddlewaretoken", csrf);
            // Preserve any extra hidden inputs the template might add later.
            try {
                for (const [k, v] of new FormData(form).entries()) {
                    if (k === "csrfmiddlewaretoken") continue;
                    if (typeof v === "string") params.append(k, v);
                }
            } catch (_) { /* ignore — params already has csrf */ }

            const response = await fetch(form.action, {
                method: "POST",
                headers: {
                    "X-CSRFToken": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                body: params.toString(),
                credentials: "same-origin",
            });

            const payload = await response.json().catch(() => ({}));
            if (!response.ok || !payload.ok) {
                showToast(payload.error || "Xoá không thành công.", "danger");
                return;
            }

            const incidentId = parseId(form.dataset.incidentId || payload.incident_id);
            if (Number.isFinite(incidentId)) {
                incidentListContainer
                    ?.querySelectorAll(`.chat-row[data-incident-id='${incidentId}']`)
                    .forEach((row) => row.remove());
            } else {
                const row = form.closest(".chat-row");
                if (row) row.remove();
            }

            recomputeIncidentBounds();
            ensureEmptyState();
            updateTopStatus("");
            await loadNewMessages(false);
        } catch (_) {
            showToast("Xoá không thành công.", "danger");
        }
    }

    // ── BUSY overlay (candidate-mutation lock) ──────────────────────────
    // Singleton helpers around #appBusyOverlay. Visible ONLY for the holder
    // of the lock (so a long DB write doesn't lock other admins' UI). When
    // a release event arrives — or when the local request finishes, even
    // if the broadcast race-loses — the overlay is hidden idempotently.
    function getBusyOverlay() { return document.getElementById("appBusyOverlay"); }

    function showBusyOverlay(message) {
        const overlay = getBusyOverlay();
        if (!overlay) return;
        const msgEl = overlay.querySelector("#appBusyOverlayMessage");
        if (msgEl && message) msgEl.textContent = message;
        overlay.hidden = false;
        overlay.setAttribute("aria-hidden", "false");
        // While busy, suppress accidental Esc / backdrop dismissals — the
        // overlay has no Cancel button by design (per spec).
        document.body.classList.add("app-busy-active");
    }

    function hideBusyOverlay() {
        const overlay = getBusyOverlay();
        if (!overlay) return;
        overlay.hidden = true;
        overlay.setAttribute("aria-hidden", "true");
        document.body.classList.remove("app-busy-active");
    }

    // ── Incident-bulk-delete BUSY indicator ─────────────────────────────
    // Driven by the ``incidents_lock`` WS event: ALL connected clients flip
    // ``body.bulk-delete-busy`` so the composer is hidden behind the
    // "đang xoá nhiều tin nhắn" notice (CSS-only swap). The owner stays in
    // selection mode while busy — their action bar's Delete button is
    // explicitly disabled below to prevent a double-fire.
    function applyBulkBusyState() {
        const busy = !!incidentsLockState.busy;
        document.body.classList.toggle("bulk-delete-busy", busy);
        const notice = document.getElementById("composeBulkBusyNotice");
        if (notice) {
            notice.hidden = !busy || document.body.classList.contains("selection-mode-active");
            notice.setAttribute("aria-hidden", notice.hidden ? "true" : "false");
        }
        const deleteBtn = document.getElementById("incidentBulkDeleteBtn");
        if (deleteBtn) {
            // Re-evaluate disabled state. While busy, never accept clicks.
            // When idle, defer to the bulk-select module's own enable rule
            // (which sets ``disabled`` based on selection count).
            if (busy) {
                deleteBtn.disabled = true;
                deleteBtn.dataset.bulkBusyDisabled = "1";
            } else if (deleteBtn.dataset.bulkBusyDisabled === "1") {
                delete deleteBtn.dataset.bulkBusyDisabled;
                if (incidentBulkSelectApi && typeof incidentBulkSelectApi.syncCount === "function") {
                    incidentBulkSelectApi.syncCount();
                }
            }
        }
        // The visible bottom-bar height just changed (composer ↔ busy
        // notice), so recompute the chat list's bottom padding before
        // the user notices the last bubble being clipped.
        if (typeof syncComposerOffset === "function") syncComposerOffset();
    }

    // Defensive guard for any composer-like submit. Returns a Vietnamese
    // toast string when the submit must be blocked, ``null`` otherwise.
    function composerBlockedReason() {
        if (incidentsLockState.busy) {
            return "Đang có quá trình xoá nhiều tin nhắn diễn ra, hãy thử lại sau.";
        }
        if (document.body.classList.contains("selection-mode-active")) {
            return "Hãy thoát chế độ chọn nhiều trước khi gửi tin nhắn.";
        }
        return null;
    }

    function buildWebsocketUrl() {
        if (!monitorRoot) return "";
        const wsPath = monitorRoot.dataset.wsPath;
        if (!wsPath) return "";
        const protocol = window.location.protocol === "https:" ? "wss" : "ws";
        return `${protocol}://${window.location.host}${wsPath}`;
    }

    function connectLiveSocket() {
        if (!monitorRoot) return;

        const socketUrl = buildWebsocketUrl();
        if (!socketUrl) return;
        if (liveSocket && (liveSocket.readyState === WebSocket.OPEN || liveSocket.readyState === WebSocket.CONNECTING)) return;

        updateConnectionStatus("Đang kết nối thời gian thực...");

        try {
            liveSocket = new WebSocket(socketUrl);
        } catch (error) {
            console.debug("Websocket init failed:", error);
            updateConnectionStatus("Kết nối thời gian thực tạm thời không khả dụng");
            return;
        }

        liveSocket.addEventListener("open", () => {
            reconnectDelayMs = 1000;
            updateConnectionStatus("");
        });

        liveSocket.addEventListener("message", (event) => {
            try {
                const payload = JSON.parse(event.data);
                if (payload.type === "live_event") {
                    loadNewMessages(false);
                    return;
                }
                if (payload.type === "candidates_lock") {
                    // Snapshot the new state so any subsequent UI hook can
                    // ask without re-broadcasting through the network.
                    candidatesLockState = {
                        busy: !!payload.busy,
                        owner_user_id: payload.owner_user_id ?? null,
                        owner_username: payload.owner_username || "",
                        operation: payload.operation || "",
                    };
                    // Only the LOCK HOLDER sees the blocking modal. Other
                    // admins keep their UI fully usable; their own attempts
                    // to mutate hit a 409 from the server and get a toast.
                    if (candidatesLockState.busy
                        && CURRENT_USER_ID != null
                        && Number(candidatesLockState.owner_user_id) === Number(CURRENT_USER_ID)) {
                        showBusyOverlay();
                    } else {
                        // Either lock released, or we are not the holder.
                        // Hide is safe to call even if we never showed it.
                        hideBusyOverlay();
                    }
                    return;
                }
                if (payload.type === "candidates_changed") {
                    // Drop cached SBD → name mappings for affected rows so
                    // the next tooltip hover re-fetches fresh data.
                    const affected = Array.isArray(payload.affected_sbds) ? payload.affected_sbds : [];
                    if (payload.old_sbd) affected.push(payload.old_sbd);
                    if (payload.sbd) affected.push(payload.sbd);
                    affected.forEach((sbd) => {
                        if (sbd) _sbdNameCache.delete(String(sbd).toUpperCase());
                    });
                    if ((payload.kind === "csv_reload") || affected.length === 0) {
                        // Bulk / unknown-set change → wipe cache entirely.
                        _sbdNameCache.clear();
                    }
                    dispatchCandidatesChanged(payload);
                    return;
                }
                if (payload.type === "incidents_lock") {
                    incidentsLockState = {
                        busy: !!payload.busy,
                        owner_user_id: payload.owner_user_id ?? null,
                        owner_username: payload.owner_username || "",
                        operation: payload.operation || "",
                    };
                    applyBulkBusyState();
                    return;
                }
                if (payload.type === "incidents_changed") {
                    // Server broadcast: one or more incidents have been
                    // removed. Drop the matching rows from the DOM so
                    // every connected client converges to the post-delete
                    // state without a full reload.
                    const ids = Array.isArray(payload.deleted_ids) ? payload.deleted_ids : [];
                    if (!ids.length) return;
                    let removed = 0;
                    ids.forEach((rawId) => {
                        const id = Number(rawId);
                        if (!Number.isFinite(id) || id <= 0) return;
                        const row = document.querySelector(
                            `.chat-row[data-incident-id="${CSS.escape(String(id))}"]`
                        );
                        if (row) {
                            row.remove();
                            removed += 1;
                        }
                    });
                    if (removed > 0) {
                        if (typeof recomputeIncidentBounds === "function") {
                            recomputeIncidentBounds();
                        }
                        if (typeof ensureEmptyState === "function") {
                            ensureEmptyState();
                        }
                        if (incidentBulkSelectApi
                            && typeof incidentBulkSelectApi.notifyDomChanged === "function") {
                            incidentBulkSelectApi.notifyDomChanged();
                        }
                    }
                    return;
                }
            } catch (error) {
                console.debug("Invalid websocket payload:", error);
            }
        });

        liveSocket.addEventListener("error", () => {
            updateConnectionStatus("Đang kết nối lại thời gian thực...");
        });

        liveSocket.addEventListener("close", () => {
            liveSocket = null;
            updateConnectionStatus("Mất kết nối thời gian thực. Đang kết nối lại...");
            window.setTimeout(connectLiveSocket, reconnectDelayMs);
            reconnectDelayMs = Math.min(reconnectDelayMs * 2, 30000);
        });
    }

    async function openCandidateDetail(sbd) {
        if (!detailContent || !detailCanvas) return;

        detailContent.innerHTML = '<div class="text-center py-4 text-muted">Đang tải...</div>';
        detailCanvas.show();

        try {
            const response = await fetch(`/stats/candidate/${encodeURIComponent(sbd)}/`, {
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
            if (!response.ok) {
                detailContent.innerHTML = '<div class="alert alert-danger">Không thể tải chi tiết thí sinh.</div>';
                return;
            }
            detailContent.innerHTML = await response.text();
            bindEvidenceGuards(detailContent);
            bindEvidencePlaceholders(detailContent);
            bindLightGallery(detailContent);
            bindSbdHoverTooltips(detailContent);
            formatLocalTimestamps(detailContent);
        } catch (_) {
            detailContent.innerHTML = '<div class="alert alert-danger">Không thể tải chi tiết thí sinh.</div>';
        }
    }

    function insertTextAt(ta, text, replaceStart, replaceEnd) {
        ta.focus();
        ta.setSelectionRange(replaceStart, replaceEnd);
        let ok = false;
        try {
            ok = document.execCommand("insertText", false, text);
        } catch (_) {
            ok = false;
        }
        if (!ok) {
            const value = ta.value;
            ta.value = value.slice(0, replaceStart) + text + value.slice(replaceEnd);
            ta.dispatchEvent(new Event("input", { bubbles: true }));
        }
    }

    function insertMarkdown(ta, opts) {
        const {
            before = "",
            after = before,
            placeholder = "text",
            linePrefix = "",
            block = false,
            // When set, only this slice of `placeholder` is selected after
            // insertion (instead of the whole placeholder). Useful for
            // mention templates like "TS0000" where we want just the digits
            // to be replaceable.
            selectOffset = null,
            selectLength = null,
        } = opts;
        const start = ta.selectionStart;
        const end = ta.selectionEnd;
        const val = ta.value;
        const selected = val.slice(start, end);
        let insert;
        let cursorStart;
        let cursorEnd;

        if (linePrefix) {
            const lines = (selected || placeholder).split("\n").map((line) => linePrefix + line).join("\n");
            const prefix = (block && start > 0 && val[start - 1] !== "\n") ? "\n" : "";
            const suffix = (block && end < val.length && val[end] !== "\n") ? "\n" : "";
            insert = prefix + lines + suffix;
            cursorStart = start + prefix.length;
            cursorEnd = cursorStart + lines.length;
        } else if (selected) {
            insert = before + selected + after;
            cursorStart = start + before.length;
            cursorEnd = cursorStart + selected.length;
        } else {
            insert = before + placeholder + after;
            const placeholderStart = start + before.length;
            if (
                Number.isInteger(selectOffset) &&
                Number.isInteger(selectLength) &&
                selectOffset >= 0 &&
                selectOffset + selectLength <= placeholder.length
            ) {
                cursorStart = placeholderStart + selectOffset;
                cursorEnd = cursorStart + selectLength;
            } else {
                cursorStart = placeholderStart;
                cursorEnd = cursorStart + placeholder.length;
            }
        }

        insertTextAt(ta, insert, start, end);
        ta.setSelectionRange(cursorStart, cursorEnd);
    }

    async function refreshPreview(editorWrap) {
        const ta = editorWrap.querySelector("textarea");
        const previewPane = editorWrap.querySelector(".md-pane-preview");
        const content = previewPane?.querySelector(".md-preview-content");
        if (!ta || !content) return;

        content.innerHTML = "<div class='md-preview-skeleton'><div class='skel-line'></div><div class='skel-line'></div><div class='skel-line'></div></div>";

        const sbdInput = document.getElementById("id_sbd");
        const kindInput = document.getElementById("id_incident_kind");
        const form = new FormData();
        form.append("violation_text", ta.value);
        form.append("sbd", sbdInput ? sbdInput.value : "");
        form.append("incident_kind", kindInput ? kindInput.value : "violation");
        form.append("is_markdown", "1");

        try {
            const res = await fetch(PREVIEW_URL, {
                method: "POST",
                headers: {
                    "X-CSRFToken": getCsrfToken(),
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: form,
                credentials: "same-origin",
            });
            if (!res.ok) throw new Error(`Preview HTTP ${res.status}`);
            const data = await res.json();
            content.innerHTML = data.html || "";
            bindEvidenceGuards(content);
            bindLightGallery(content);
        } catch (_) {
            content.innerHTML = '<div class="text-muted small">Không thể xem trước lúc này.</div>';
        }
    }

    async function uploadImageForTextarea(ta, file) {
        const selStart = ta.selectionStart;
        const selEnd = ta.selectionEnd;
        const selected = ta.value.slice(selStart, selEnd);
        const alt = (selected && selected.trim()) || "ảnh";
        const placeholder = `![Đang tải ${alt}...]()`;

        insertTextAt(ta, placeholder, selStart, selEnd);

        const form = new FormData();
        form.append("image", file);

        try {
            const res = await fetch(UPLOAD_URL, {
                method: "POST",
                headers: {
                    "X-CSRFToken": getCsrfToken(),
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: form,
                credentials: "same-origin",
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.url) {
                showToast(data.error || "Tải ảnh lên thất bại.", "danger");
                return;
            }

            const replacement = `![${alt}](${data.url})`;
            const idx = ta.value.indexOf(placeholder);
            if (idx !== -1) {
                insertTextAt(ta, replacement, idx, idx + placeholder.length);
            }
        } catch (_) {
            showToast("Tải ảnh lên thất bại.", "danger");
        }
    }

    // Shared markdown-editor actions. Used by both the dashboard composer
    // and the standalone edit-incident page.
    const MD_ACTIONS = {
        bold: (ta) => insertMarkdown(ta, { before: "**", placeholder: "chữ đậm" }),
        italic: (ta) => insertMarkdown(ta, { before: "*", placeholder: "chữ nghiêng" }),
        strike: (ta) => insertMarkdown(ta, { before: "~~", placeholder: "gạch ngang" }),
        code: (ta) => insertMarkdown(ta, { before: "`", placeholder: "mã lệnh" }),
        codeblock: (ta) => insertMarkdown(ta, { before: "```\n", after: "\n```", placeholder: "khối mã", block: true }),
        quote: (ta) => insertMarkdown(ta, { linePrefix: "> ", placeholder: "trích dẫn", block: true }),
        ul: (ta) => insertMarkdown(ta, { linePrefix: "- ", placeholder: "mục", block: true }),
        ol: (ta) => insertMarkdown(ta, { linePrefix: "1. ", placeholder: "mục", block: true }),
        link: (ta) => insertMarkdown(ta, { before: "[", after: "](url)", placeholder: "văn bản liên kết" }),
        image: (ta) => insertMarkdown(ta, { before: "![", after: "](url)", placeholder: "mô tả ảnh" }),
        mention: (ta) => insertMarkdown(ta, {
            placeholder: "TS",
            selectOffset: 2,
            selectLength: 0,
        }),
        undo: (ta) => { ta.focus(); document.execCommand("undo"); },
        redo: (ta) => { ta.focus(); document.execCommand("redo"); },
    };

    function bindMarkdownEditorsIn(scopeEl) {
        if (!scopeEl) return;

        scopeEl.querySelectorAll(".md-toolbar").forEach((toolbar) => {
            if (toolbar.dataset.mdBound === "1") return;
            const target = toolbar.dataset.target ? document.getElementById(toolbar.dataset.target) : null;
            if (!target) return;
            toolbar.dataset.mdBound = "1";

            toolbar.querySelectorAll(".md-tb-btn[data-action]").forEach((btn) => {
                btn.addEventListener("click", (event) => {
                    event.preventDefault();
                    const action = btn.dataset.action;
                    if (action === "upload") {
                        const input = document.createElement("input");
                        input.type = "file";
                        input.accept = "image/jpeg,image/png,image/gif,image/webp";
                        input.addEventListener("change", () => {
                            const file = input.files && input.files[0];
                            if (file) uploadImageForTextarea(target, file);
                        });
                        input.click();
                        return;
                    }
                    if (MD_ACTIONS[action]) MD_ACTIONS[action](target);
                });
            });
        });

        scopeEl.querySelectorAll(".md-editor-wrap").forEach((wrap) => {
            if (wrap.dataset.mdTabsBound === "1") return;
            const tabBtns = wrap.querySelectorAll(".md-tab-btn");
            const inputPane = wrap.querySelector(".md-pane-input");
            const previewPane = wrap.querySelector(".md-pane-preview");
            if (!tabBtns.length || !inputPane || !previewPane) return;
            wrap.dataset.mdTabsBound = "1";

            tabBtns.forEach((btn) => {
                btn.addEventListener("click", (event) => {
                    event.preventDefault();
                    tabBtns.forEach((other) => other.classList.remove("active"));
                    btn.classList.add("active");
                    if (btn.dataset.tab === "input") {
                        inputPane.style.display = "";
                        previewPane.style.display = "none";
                    } else {
                        inputPane.style.display = "none";
                        previewPane.style.display = "";
                        refreshPreview(wrap);
                    }
                });
            });

            const reloadBtn = wrap.querySelector(".md-preview-reload");
            if (reloadBtn) {
                reloadBtn.addEventListener("click", (event) => {
                    event.preventDefault();
                    refreshPreview(wrap);
                });
            }
        });
    }

    // Client-side preflight for video attachments. Mirrors server-side
    // MAX_VIDEO_SIZE (40 MB) so the user gets instant feedback instead of
    // uploading the whole file only to be rejected.
    const VIDEO_EXT_SET = new Set(["mp4", "mov", "webm", "mkv", "avi"]);
    const CLIENT_MAX_VIDEO_BYTES = 40 * 1024 * 1024;

    function fileLooksLikeVideo(file) {
        if (!file) return false;
        if (file.type && file.type.startsWith("video/")) return true;
        const ext = (file.name || "").split(".").pop()?.toLowerCase();
        return Boolean(ext && VIDEO_EXT_SET.has(ext));
    }

    function validateEvidenceFile(file) {
        // Client-side preflight only. Server remains the source of truth; a
        // bypass just fails the upload there with the same error.
        if (!file) return true;
        if (fileLooksLikeVideo(file) && file.size > CLIENT_MAX_VIDEO_BYTES) {
            showToast(
                `Video quá lớn (${formatFileSize(file.size)}). Dung lượng tối đa là 40 MB.`,
                "danger",
            );
            return false;
        }
        return true;
    }

    // Wraps a <input type="file"> so that cancelling the native picker, or
    // picking a file that fails validation, leaves the previously-accepted
    // file intact instead of silently wiping the attachment. The previously
    // accepted File is kept in ``pendingFile`` and re-applied to the input
    // via DataTransfer whenever a new selection fails.
    //
    // Returns { clear, getFile } for callers that need to reset state (e.g.
    // a trash button, or post-submit form.reset()).
    function wireEvidenceInput(input, { onIndicatorUpdate } = {}) {
        if (!input) return { clear: () => {}, getFile: () => null };

        let pendingFile = null;

        const notify = () => {
            if (typeof onIndicatorUpdate === "function") onIndicatorUpdate();
        };

        const applyPending = () => {
            try {
                const dt = new DataTransfer();
                if (pendingFile) dt.items.add(pendingFile);
                input.files = dt.files;
            } catch (_) {
                // DataTransfer not available: best-effort fallback. We can
                // only clear, not restore, so drop pending to keep state
                // consistent with what the input actually holds.
                if (!pendingFile) {
                    try { input.value = ""; } catch (__) { /* noop */ }
                } else {
                    pendingFile = null;
                }
            }
            notify();
        };

        input.addEventListener("change", () => {
            const picked = input.files && input.files[0];
            if (!picked) {
                // User cancelled the picker, or the browser cleared the
                // selection — restore whatever we had committed before.
                applyPending();
                return;
            }
            if (!validateEvidenceFile(picked)) {
                // New file rejected — roll back to the previous attachment.
                applyPending();
                return;
            }
            pendingFile = picked;
            notify();
        });

        return {
            clear() {
                pendingFile = null;
                applyPending();
            },
            getFile() {
                return pendingFile;
            },
        };
    }

    function initComposeBar() {
        const form = document.getElementById("create-incident-form");
        if (!form) return;

        // attach SBD validator for composer
        const sbdInput = document.getElementById("id_sbd");
        composeSbdValidate = attachSbdValidation(sbdInput);

        const simpleInput = document.getElementById("id_violation_text_simple");
        const fullTextarea = document.getElementById("id_violation_text_full");
        const isMarkdownField = document.getElementById("id_is_markdown");
        const expandBtn = document.getElementById("compose-expand-btn");
        const collapseBtn = document.getElementById("compose-collapse-btn");
        const expandedWrap = document.getElementById("compose-expanded");
        const evidenceLabel = form.querySelector(".compose-video");
        const evidenceName = document.getElementById("video-filename");

        const getEvidenceInput = () => form.querySelector('input[type="file"][name="evidence"]');

        const updateEvidenceIndicator = () => {
            const cur = getEvidenceInput();
            if (!cur || !evidenceLabel || !evidenceName) return;
            const file = cur.files && cur.files[0];
            if (!file) {
                evidenceLabel.classList.remove("has-file");
                evidenceName.textContent = "";
                evidenceName.title = "";
                const clear = evidenceLabel.querySelector('.compose-video-clear');
                if (clear) clear.style.display = 'none';
                return;
            }

            const kind = file.type && file.type.startsWith("image/") ? "Ảnh" : "Tệp";
            const sizeText = formatFileSize(file.size);
            const info = sizeText ? `${kind}: ${file.name} (${sizeText})` : `${kind}: ${file.name}`;
            evidenceLabel.classList.add("has-file");
            evidenceName.textContent = info;
            evidenceName.title = info;
            const clear = evidenceLabel.querySelector('.compose-video-clear');
            if (clear) clear.style.display = '';
        };

        refreshComposeEvidenceIndicator = updateEvidenceIndicator;

        const evidenceInput = getEvidenceInput();
        const evidenceControl = wireEvidenceInput(evidenceInput, {
            onIndicatorUpdate: updateEvidenceIndicator,
        });

        // Trash button clears the attachment. This is the only entry point
        // that should drop the current file — cancelling the native picker
        // or picking an invalid file must not wipe it.
        if (evidenceLabel) {
            const clearBtn = document.createElement('button');
            clearBtn.type = 'button';
            clearBtn.className = 'btn btn-link btn-sm compose-video-clear';
            clearBtn.title = 'Huỷ đính kèm';
            clearBtn.style.display = 'none';
            clearBtn.innerHTML = '<i class="bi bi-trash" style="color:rgb(255,0,0)"></i>';
            evidenceLabel.appendChild(clearBtn);
            clearBtn.addEventListener('click', (ev) => {
                ev.preventDefault();
                evidenceControl.clear();
            });
        }

        // form.reset() runs after a successful AJAX submit — mirror that into
        // our backing state so the indicator clears and we don't restore a
        // stale file on the next pick.
        form.addEventListener("reset", () => {
            // Let the browser clear input.files first, then sync state.
            setTimeout(() => evidenceControl.clear(), 0);
        });

        updateEvidenceIndicator();

        // --- Unsaved-changes detection for composer (smart compare) ---------
        //
        // Tracks SBD, incident_kind, the visible text (whichever pane is
        // active), and whether an evidence file is attached. Previous
        // version only watched the textareas, so changing SBD or kind and
        // navigating away skipped the prompt.
        const getComposerText = () => {
            if (!simpleInput && !fullTextarea) return "";
            const mode = form.dataset.mode || (fullTextarea && fullTextarea.value ? "expanded" : "simple");
            if (mode === "expanded") return (fullTextarea && fullTextarea.value) ? fullTextarea.value : "";
            return (simpleInput && simpleInput.value) ? simpleInput.value : "";
        };

        const sbdInputForDirty = document.getElementById("id_sbd");
        const kindInputForDirty = document.getElementById("id_incident_kind");

        const composerBaseline = {
            text: (getComposerText() || "").trim(),
            sbd: sbdInputForDirty ? (sbdInputForDirty.value || "").trim() : "",
            kind: kindInputForDirty ? (kindInputForDirty.value || "") : "",
            mode: form.dataset.mode || "simple",
            evidence: Boolean(
                getEvidenceInput() && getEvidenceInput().files && getEvidenceInput().files.length > 0
            ),
        };

        function refreshComposerBaseline() {
            composerBaseline.text = (getComposerText() || "").trim();
            composerBaseline.sbd = sbdInputForDirty ? (sbdInputForDirty.value || "").trim() : "";
            composerBaseline.kind = kindInputForDirty ? (kindInputForDirty.value || "") : "";
            composerBaseline.mode = form.dataset.mode || "simple";
            composerBaseline.evidence = Boolean(
                getEvidenceInput() && getEvidenceInput().files && getEvidenceInput().files.length > 0
            );
        }
        // Expose to handleComposerSubmit so it can refresh after success.
        form.__refreshComposerBaseline = refreshComposerBaseline;

        function isComposerDirty() {
            const curText = (getComposerText() || "").trim();
            const curSbd = sbdInputForDirty ? (sbdInputForDirty.value || "").trim() : "";
            const curKind = kindInputForDirty ? (kindInputForDirty.value || "") : "";
            const curMode = form.dataset.mode || "simple";
            const curEvidence = Boolean(
                getEvidenceInput() && getEvidenceInput().files && getEvidenceInput().files.length > 0
            );
            if (curText !== composerBaseline.text) return true;
            if (curSbd !== composerBaseline.sbd) return true;
            if (curKind !== composerBaseline.kind) return true;
            if (curMode !== composerBaseline.mode) return true;
            if (curEvidence !== composerBaseline.evidence) return true;
            return false;
        }

        // Intercept in-page navigation links so the user gets our nicer
        // confirm dialog instead of the (often-suppressed) native prompt.
        // Anchor handlers must be added once per page; a delegated handler
        // is the cleanest fit because navbar items can re-render.
        document.addEventListener("click", (ev) => {
            const a = ev.target.closest("a[href]");
            if (!a) return;
            // Ignore anchors that don't navigate the document (downloads,
            // new-tab middle-clicks, modifier-clicks).
            if (a.target && a.target !== "_self") return;
            if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button === 1) return;
            let url;
            try { url = new URL(a.href, window.location.href); } catch (_) { return; }
            if (url.origin !== window.location.origin) return;
            // Pure same-page hash jumps shouldn't prompt.
            if (a.hash && url.pathname === window.location.pathname && url.search === window.location.search) return;
            if (!isComposerDirty()) return;
            ev.preventDefault();
            showConfirmDialog({
                title: "Rời trang?",
                message: "Bạn có thay đổi chưa lưu trên ô soạn tin. Rời đi sẽ bỏ các thay đổi này.",
                okText: "Rời đi",
                cancelText: "Ở lại",
                variant: "dark",
            }).then((ok) => {
                if (!ok) return;
                __allowUnload = true;
                window.location.href = a.href;
            });
        }, true);

        // beforeunload fires for closing tab / typing a new URL / refresh.
        // We do NOT flip __allowUnload on submit-start any more — only on
        // confirmed success — so a failed submit still prompts.
        window.addEventListener("beforeunload", (ev) => {
            if (__allowUnload) return undefined;
            if (isComposerDirty()) {
                ev.preventDefault();
                ev.returnValue = "Có thay đổi chưa lưu, bạn có chắc chắn muốn rời đi?";
                return ev.returnValue;
            }
            return undefined;
        });

        const setExpanded = (expanded) => {
            if (!simpleInput || !fullTextarea || !isMarkdownField || !expandedWrap) return;

            if (expanded) {
                // Switching simple -> markdown: carry whatever the user has
                // already typed into the markdown textarea so the draft is not
                // lost. We overwrite any stale content in the markdown field
                // because the simple input was the one the user was just
                // editing and is therefore the authoritative draft.
                if (simpleInput.value) {
                    fullTextarea.value = simpleInput.value;
                }
                form.dataset.mode = "expanded";
                form.classList.add("is-expanded");
                expandedWrap.hidden = false;
                isMarkdownField.value = "1";
                fullTextarea.name = "violation_text";
                fullTextarea.setAttribute("required", "required");
                simpleInput.name = "violation_text_simple_ignored";
                simpleInput.removeAttribute("required");
                fullTextarea.focus();
            } else {
                // Switching markdown -> simple: mirror the markdown draft back
                // into the simple input so the user does not lose what they
                // have typed when collapsing the editor.
                if (fullTextarea.value) {
                    simpleInput.value = fullTextarea.value;
                }
                form.dataset.mode = "simple";
                form.classList.remove("is-expanded");
                expandedWrap.hidden = true;
                isMarkdownField.value = "0";
                simpleInput.name = "violation_text";
                simpleInput.setAttribute("required", "required");
                fullTextarea.name = "violation_text_full_ignored";
                fullTextarea.removeAttribute("required");
            }
            syncComposerOffset();
        };

        setComposeExpanded = setExpanded;

        if (expandBtn) expandBtn.addEventListener("click", () => setExpanded(true));
        if (collapseBtn) collapseBtn.addEventListener("click", () => setExpanded(false));

        form.addEventListener("submit", () => {
            if (form.dataset.mode === "expanded") {
                fullTextarea.name = "violation_text";
                isMarkdownField.value = "1";
            } else {
                simpleInput.name = "violation_text";
                isMarkdownField.value = "0";
            }
        });

        bindMarkdownEditorsIn(form);
    }

    function initEditIncidentPage() {
        const form = document.getElementById("edit-incident-form");
        if (!form) return;

        // attach SBD validator for edit page
        const sbdInput = form.querySelector('input[name="sbd"]');
        editSbdValidate = attachSbdValidation(sbdInput);

        // Prevent form submit if SBD invalid OR if a bulk-delete is in
        // progress on the live channel (stale tab guard — the server still
        // returns 409 in that case).
        form.addEventListener('submit', (ev) => {
            const blockedReason = composerBlockedReason();
            if (blockedReason) {
                ev.preventDefault();
                showToast(blockedReason, "warning");
                return false;
            }
            if (typeof editSbdValidate === 'function') {
                if (!editSbdValidate()) {
                    ev.preventDefault();
                    // focus the invalid field
                    if (sbdInput) sbdInput.focus();
                    return false;
                }
            }
            return true;
        });
        bindMarkdownEditorsIn(form);

        const evidenceInput = form.querySelector('input[type="file"][name="evidence"]');
        // Same sticky-attachment behaviour as the composer: cancelling the
        // native picker or picking an invalid file must not wipe a file the
        // user has already selected on this page.
        wireEvidenceInput(evidenceInput);
        // --- Unsaved-changes detection for edit page (smart compare) ---------
        (function attachEditUnsavedHandler() {
            const sbdInput = form.querySelector('input[name="sbd"]');
            const kindInput = form.querySelector('select[name="incident_kind"]');
            const fullTextarea = form.querySelector('textarea[name="violation_text"]') || form.querySelector('textarea[id="id_violation_text"]');
            const removeEvidence = form.querySelector('input[name="remove_evidence"]');
            const fileInput = form.querySelector('input[type="file"][name="evidence"]');

            const initial = {
                sbd: sbdInput ? (sbdInput.value || "") : "",
                kind: kindInput ? (kindInput.value || "") : "",
                violation: fullTextarea ? (fullTextarea.value || "") : "",
                removeEvidence: removeEvidence ? Boolean(removeEvidence.checked) : false,
                fileSelected: fileInput && fileInput.files && fileInput.files.length > 0,
            };

            function isEditDirty() {
                const cur = {
                    sbd: sbdInput ? (sbdInput.value || "") : "",
                    kind: kindInput ? (kindInput.value || "") : "",
                    violation: fullTextarea ? (fullTextarea.value || "") : "",
                    removeEvidence: removeEvidence ? Boolean(removeEvidence.checked) : false,
                    fileSelected: fileInput && fileInput.files && fileInput.files.length > 0,
                };
                if ((cur.sbd || "").trim() !== (initial.sbd || "").trim()) return true;
                if ((cur.kind || "") !== (initial.kind || "")) return true;
                if ((cur.violation || "").trim() !== (initial.violation || "").trim()) return true;
                if (Boolean(cur.removeEvidence) !== Boolean(initial.removeEvidence)) return true;
                if (Boolean(cur.fileSelected) !== Boolean(initial.fileSelected)) return true;
                return false;
            }

            // Clear on save -> allow unload
            form.addEventListener('submit', () => { __allowUnload = true; });

            // Intercept cancel/back link on the page
            document.querySelectorAll('a').forEach((a) => {
                if (!a.href) return;
                try {
                    const url = new URL(a.href, window.location.href);
                    if (url.origin !== window.location.origin) return;
                } catch (_) { return; }
                a.addEventListener('click', (ev) => {
                    if (!isEditDirty()) return;
                    if (a.hash && a.pathname === window.location.pathname) return;
                    ev.preventDefault();
                    showConfirmDialog({
                        title: 'Rời trang?',
                        message: 'Bạn có thay đổi chưa lưu. Rời đi sẽ bỏ các thay đổi này.',
                        okText: 'Rời đi',
                        cancelText: 'Ở lại',
                        variant: 'dark',
                    }).then((ok) => {
                        if (ok) {
                            __allowUnload = true;
                            window.location.href = a.href;
                        }
                    });
                });
            });

            // beforeunload
            window.addEventListener('beforeunload', (ev) => {
                if (__allowUnload) return undefined;
                if (isEditDirty()) {
                    ev.preventDefault();
                    ev.returnValue = 'Bạn có thay đổi chưa lưu. Rời trang sẽ bỏ các thay đổi.';
                    return ev.returnValue;
                }
                return undefined;
            });
        })();
    }

    document.addEventListener("click", (event) => {
        const candidateButton = event.target.closest(".js-open-candidate-detail");
        if (candidateButton) {
            openCandidateDetail(candidateButton.dataset.sbd);
        }
    });

    document.addEventListener("submit", (event) => {
        const deleteForm = event.target.closest(".js-delete-incident-form");
        if (!deleteForm) return;
        event.preventDefault();
        if (deleteForm.dataset.confirmInFlight === "1") return;
        deleteForm.dataset.confirmInFlight = "1";
        showConfirmDialog({
            title: deleteForm.dataset.confirmTitle || "Xoá tin nhắn này?",
            message: deleteForm.dataset.confirmMessage
                || "Tin nhắn và file đính kèm sẽ bị xoá vĩnh viễn.",
            okText: "Xoá",
            cancelText: "Huỷ",
        }).then((confirmed) => {
            deleteForm.dataset.confirmInFlight = "0";
            if (confirmed) deleteIncidentWithoutReload(deleteForm);
        });
    });

    // --- SBD hover tooltip support -------------------------------------------------
    const _sbdNameCache = new Map();

    async function fetchCandidateName(sbd) {
        if (!sbd) return sbd;
        const key = sbd.toUpperCase();
        if (_sbdNameCache.has(key)) return _sbdNameCache.get(key);
        try {
            const res = await fetch(`/stats/candidate/${encodeURIComponent(sbd)}/`, {
                headers: { "X-Requested-With": "XMLHttpRequest" },
            });
            if (!res.ok) {
                _sbdNameCache.set(key, sbd);
                return sbd;
            }
            const html = await res.text();
            const wrapper = document.createElement("div");
            wrapper.innerHTML = html;
            const nameEl = wrapper.querySelector(".detail-value-name");
            const name = nameEl ? nameEl.textContent.trim() : sbd;
            _sbdNameCache.set(key, name || sbd);
            return _sbdNameCache.get(key);
        } catch (_) {
            _sbdNameCache.set(key, sbd);
            return sbd;
        }
    }

    function createTooltip(text) {
        const el = document.createElement("div");
        el.className = "sbd-tooltip";
        el.setAttribute("role", "tooltip");
        el.textContent = text;
        document.body.appendChild(el);
        return el;
    }

    function placeTooltipWithinViewport(tooltipEl, targetRect) {
        const margin = 8;
        const pad = 6;
        const tw = tooltipEl.offsetWidth;
        const th = tooltipEl.offsetHeight;
        const vw = window.innerWidth;
        const vh = window.innerHeight;

        // Try positions in preference order: top, bottom, left, right
        const candidates = [];
        // top
        candidates.push({
            left: Math.min(Math.max(targetRect.left + (targetRect.width - tw) / 2, pad), vw - tw - pad),
            top: targetRect.top - th - margin,
            placement: "top",
        });
        // bottom
        candidates.push({
            left: Math.min(Math.max(targetRect.left + (targetRect.width - tw) / 2, pad), vw - tw - pad),
            top: targetRect.bottom + margin,
            placement: "bottom",
        });
        // left
        candidates.push({
            left: targetRect.left - tw - margin,
            top: Math.min(Math.max(targetRect.top + (targetRect.height - th) / 2, pad), vh - th - pad),
            placement: "left",
        });
        // right
        candidates.push({
            left: targetRect.right + margin,
            top: Math.min(Math.max(targetRect.top + (targetRect.height - th) / 2, pad), vh - th - pad),
            placement: "right",
        });

        for (const c of candidates) {
            const fitsHoriz = c.left >= 0 && (c.left + tw) <= vw;
            const fitsVert = c.top >= 0 && (c.top + th) <= vh;
            if (fitsHoriz && fitsVert) {
                tooltipEl.style.left = `${Math.round(c.left)}px`;
                tooltipEl.style.top = `${Math.round(c.top)}px`;
                tooltipEl.setAttribute("data-placement", c.placement);
                return;
            }
        }

        // Fallback: clamp to viewport
        tooltipEl.style.left = `${Math.min(Math.max( (targetRect.left + targetRect.right) / 2 - tw / 2, pad), vw - tw - pad)}px`;
        tooltipEl.style.top = `${Math.min(Math.max(targetRect.top - th - margin, pad), vh - th - pad)}px`;
        tooltipEl.setAttribute("data-placement", "top");
    }

    function bindSbdHoverTooltips(scope) {
        const root = scope || document;
        let activeTooltip = null;
        let hoverTimer = null;

        function clearTooltip() {
            if (hoverTimer) {
                clearTimeout(hoverTimer);
                hoverTimer = null;
            }
            if (activeTooltip) {
                try { activeTooltip.remove(); } catch (_) {}
                activeTooltip = null;
            }
        }

        function onEnter(e) {
            const btn = e.currentTarget;
            const sbd = btn.dataset.sbd;
            if (!sbd) return;
            clearTimeout(hoverTimer);
            hoverTimer = setTimeout(async () => {
                const name = await fetchCandidateName(sbd);
                if (!name) return;
                if (activeTooltip) activeTooltip.remove();
                activeTooltip = createTooltip(name);
                // measure then place
                requestAnimationFrame(() => {
                    placeTooltipWithinViewport(activeTooltip, btn.getBoundingClientRect());
                });
            }, 220);
        }

        function onLeave() {
            clearTooltip();
        }

        root.querySelectorAll(".js-open-candidate-detail").forEach((el) => {
            el.removeEventListener("mouseenter", el._sbdTooltipEnter || (()=>{}));
            el.removeEventListener("mouseleave", el._sbdTooltipLeave || (()=>{}));
            el._sbdTooltipEnter = onEnter;
            el._sbdTooltipLeave = onLeave;
            el.addEventListener("mouseenter", onEnter);
            el.addEventListener("mouseleave", onLeave);
            el.addEventListener("blur", onLeave);
        });

        // Global cleanup hooks: user might open a modal, navigate, or the
        // element may be removed without a mouseleave firing. Clear tooltip
        // on these global interactions to avoid it getting stuck.
        document.addEventListener('pointerdown', clearTooltip, { passive: true });
        document.addEventListener('scroll', clearTooltip, { passive: true });
        document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') clearTooltip(); });
        document.addEventListener('visibilitychange', () => { if (document.hidden) clearTooltip(); });
        window.addEventListener('pagehide', clearTooltip);
    }

    // ── Statistics page: candidate roster toggle ─────────────────────────
    function initCandidateListToggle() {
        const btn = document.getElementById("candidate-list-toggle");
        if (!btn) return;
        const target = document.getElementById(btn.dataset.target || "candidate-list-wrap");
        if (!target) return;
        const labelEl = btn.querySelector(".js-toggle-label");
        const iconEl = btn.querySelector("i");

        const setHidden = (hidden) => {
            target.hidden = hidden;
            btn.dataset.state = hidden ? "hidden" : "shown";
            btn.setAttribute("aria-expanded", hidden ? "false" : "true");
            if (labelEl) {
                labelEl.textContent = hidden
                    ? "Hiện danh sách thí sinh"
                    : "Ẩn danh sách thí sinh";
            }
            if (iconEl) {
                iconEl.classList.remove("bi-eye", "bi-eye-slash");
                iconEl.classList.add(hidden ? "bi-eye" : "bi-eye-slash");
            }
        };

        // Initial state mirrors whatever the markup said.
        setHidden(target.hidden !== false);

        btn.addEventListener("click", (ev) => {
            ev.preventDefault();
            setHidden(target.hidden === false ? true : false);
        });
    }

    // ── Statistics page: candidate roster CRUD ───────────────────────────
    function initCandidateManager() {
        const wrap = document.getElementById("candidate-list-wrap");
        if (!wrap) return;

        const tableWrap = document.getElementById("candidate-list-table-wrap");
        const tbody = document.getElementById("candidate-list-tbody");
        const emptyState = document.getElementById("candidate-empty-state");
        const countBadge = document.getElementById("candidate-list-count");
        const masterCb = document.getElementById("candidate-select-all");
        const bulkDeleteBtn = document.getElementById("candidate-bulk-delete-btn");
        const selectionCounter = document.getElementById("candidate-selection-counter");
        const emptyAddBtn = document.getElementById("candidate-empty-add-btn");
        const editModalEl = document.getElementById("candidateEditModal");
        const editForm = document.getElementById("candidate-edit-form");

        if (!tbody || !tableWrap) return;

        const createUrl = wrap.dataset.createUrl;
        const bulkDeleteUrl = wrap.dataset.bulkDeleteUrl;

        // Helper: build a candidate-detail URL by SBD (mirrors candidateDetail
        // calls elsewhere in this file; keeps the source of truth in JS only).
        const detailUrlFor = (sbd) => `/stats/candidate/${encodeURIComponent(sbd)}/`;

        const editModal = (editModalEl && typeof bootstrap !== "undefined")
            ? bootstrap.Modal.getOrCreateInstance(editModalEl)
            : null;

        // ── Selection / master checkbox ─────────────────────────────────
        function rowCheckboxes() {
            return Array.from(tbody.querySelectorAll(".js-candidate-row-cb"));
        }

        function syncSelectionUI() {
            const cbs = rowCheckboxes();
            const total = cbs.length;
            const selected = cbs.filter((cb) => cb.checked).length;

            if (masterCb) {
                if (total === 0) {
                    masterCb.checked = false;
                    masterCb.indeterminate = false;
                    masterCb.disabled = true;
                } else {
                    masterCb.disabled = false;
                    masterCb.checked = (selected === total);
                    masterCb.indeterminate = (selected > 0 && selected < total);
                }
            }

            // Toggle bulk delete + counter chips
            if (bulkDeleteBtn) {
                if (selected > 0) {
                    bulkDeleteBtn.classList.remove("d-none");
                    bulkDeleteBtn.classList.add("is-visible");
                } else {
                    bulkDeleteBtn.classList.add("d-none");
                    bulkDeleteBtn.classList.remove("is-visible");
                }
            }
            if (selectionCounter) {
                if (selected > 0) {
                    selectionCounter.classList.remove("d-none");
                    selectionCounter.classList.add("is-visible");
                } else {
                    selectionCounter.classList.add("d-none");
                    selectionCounter.classList.remove("is-visible");
                }
            }
            // Update the live counters inside both chips
            wrap.querySelectorAll(".js-selected-count").forEach((el) => {
                el.textContent = String(selected);
            });

            // Highlight selected rows
            cbs.forEach((cb) => {
                const tr = cb.closest("tr");
                if (!tr) return;
                tr.classList.toggle("is-selected", cb.checked);
            });
        }

        function getSelectedIds() {
            return rowCheckboxes()
                .filter((cb) => cb.checked)
                .map((cb) => {
                    const tr = cb.closest("tr");
                    return tr ? Number(tr.dataset.candidateId) : NaN;
                })
                .filter((n) => Number.isFinite(n) && n > 0);
        }

        // ── Empty-state / count book-keeping ────────────────────────────
        function refreshAfterMutation() {
            renumberRows();
            const count = tbody.querySelectorAll("tr[data-candidate-id]").length;
            if (countBadge) countBadge.textContent = String(count);
            if (count === 0) {
                tableWrap.classList.add("d-none");
                if (emptyState) emptyState.classList.remove("d-none");
            } else {
                tableWrap.classList.remove("d-none");
                if (emptyState) emptyState.classList.add("d-none");
            }
            syncSelectionUI();
        }

        function renumberRows() {
            const rows = tbody.querySelectorAll("tr[data-candidate-id]");
            rows.forEach((tr, idx) => {
                const numCell = tr.querySelector(".js-row-index");
                if (numCell) numCell.textContent = String(idx + 1);
            });
        }

        // ── Row rendering ───────────────────────────────────────────────
        function buildRowEl(candidate) {
            const tr = document.createElement("tr");
            tr.dataset.candidateId = String(candidate.id);
            applyRowDataset(tr, candidate);
            tr.innerHTML = `
                <td class="cb-col">
                    <label class="cb-pretty-wrap" title="Chọn thí sinh này">
                        <input type="checkbox" class="cb-pretty js-candidate-row-cb" aria-label="Chọn ${escHtml(candidate.sbd)}">
                    </label>
                </td>
                <td class="text-muted num-col js-row-index"></td>
                <td class="js-cell-sbd">
                    <button type="button" class="btn btn-link btn-sm p-0 fw-semibold js-open-candidate-detail" data-sbd="${escHtml(candidate.sbd)}">
                        ${escHtml(candidate.sbd)}
                    </button>
                </td>
                <td class="js-cell-full-name">${escHtml(candidate.full_name || "")}</td>
                <td class="js-cell-school">${escHtml(candidate.school || "")}</td>
                <td class="js-cell-supervisor-teacher">${escHtml(candidate.supervisor_teacher || "")}</td>
                <td class="js-cell-exam-room">${escHtml(candidate.exam_room || "—")}</td>
                <td class="action-col text-end">
                    <div class="candidate-action-group" role="group" aria-label="Thao tác cho thí sinh ${escHtml(candidate.sbd)}">
                        <button type="button" class="candidate-action-btn is-add js-candidate-add"
                                title="Thêm thí sinh mới (mẫu) bên dưới"
                                aria-label="Thêm thí sinh mới">
                            <i class="bi bi-plus-lg"></i>
                        </button>
                        <button type="button" class="candidate-action-btn is-edit js-candidate-edit"
                                title="Sửa thông tin thí sinh"
                                aria-label="Sửa thí sinh ${escHtml(candidate.sbd)}">
                            <i class="bi bi-pencil-square"></i>
                        </button>
                        <button type="button" class="candidate-action-btn is-delete js-candidate-delete"
                                title="Xoá thí sinh"
                                aria-label="Xoá thí sinh ${escHtml(candidate.sbd)}">
                            <i class="bi bi-trash"></i>
                        </button>
                    </div>
                </td>
            `;
            return tr;
        }

        function applyRowDataset(tr, candidate) {
            tr.dataset.sbd = candidate.sbd || "";
            tr.dataset.fullName = candidate.full_name || "";
            tr.dataset.school = candidate.school || "";
            tr.dataset.supervisorTeacher = candidate.supervisor_teacher || "";
            tr.dataset.examRoom = candidate.exam_room || "";
        }

        function updateRowFromCandidate(tr, candidate) {
            applyRowDataset(tr, candidate);
            const sbdBtn = tr.querySelector(".js-cell-sbd .js-open-candidate-detail");
            if (sbdBtn) {
                sbdBtn.textContent = candidate.sbd;
                sbdBtn.dataset.sbd = candidate.sbd;
            }
            const setText = (selector, value) => {
                const el = tr.querySelector(selector);
                if (el) el.textContent = value;
            };
            setText(".js-cell-full-name", candidate.full_name || "");
            setText(".js-cell-school", candidate.school || "");
            setText(".js-cell-supervisor-teacher", candidate.supervisor_teacher || "");
            setText(".js-cell-exam-room", candidate.exam_room || "—");
        }

        function flashRow(tr) {
            tr.classList.add("is-just-added");
            setTimeout(() => tr.classList.remove("is-just-added"), 1300);
        }

        // ── API helpers ─────────────────────────────────────────────────
        // Using ``application/x-www-form-urlencoded`` instead of multipart
        // FormData here. Reason: empty multipart bodies (or multipart bodies
        // produced by some browser/Daphne combinations) can trip Django's
        // multipart parser into raising ``MultiPartParserError``, which the
        // exception middleware turns into a 0-byte HTTP 400 response. URL-
        // encoded bodies are simple, well-defined even when empty, and need
        // no file upload anyway for candidate CRUD — so we use them.
        //
        // ``csrfmiddlewaretoken`` is also included in the body (in addition
        // to the X-CSRFToken header) as belt-and-suspenders — the body field
        // is the canonical Django CSRF channel and is always accepted.
        async function postJson(url, fields) {
            const params = new URLSearchParams();
            const csrf = getCsrfToken();
            if (csrf) params.append("csrfmiddlewaretoken", csrf);
            // ``fields`` may be:
            //   * a plain object  { sbd: "TS001", ... }
            //   * an iterable of [key, value] pairs (works for FormData too)
            //   * null/undefined → empty body except the CSRF token
            if (fields) {
                if (typeof fields.entries === "function") {
                    for (const [k, v] of fields.entries()) {
                        if (k === "csrfmiddlewaretoken") continue; // already set
                        params.append(k, v);
                    }
                } else if (typeof fields === "object") {
                    for (const k of Object.keys(fields)) {
                        const v = fields[k];
                        if (Array.isArray(v)) {
                            v.forEach((item) => params.append(k, item));
                        } else if (v !== undefined && v !== null) {
                            params.append(k, v);
                        }
                    }
                }
            }
            const response = await fetch(url, {
                method: "POST",
                headers: {
                    "X-CSRFToken": csrf,
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                credentials: "same-origin",
                body: params.toString(),
            });
            let data = null;
            try { data = await response.json(); } catch (_) { /* fallthrough */ }
            return { response, data };
        }

        // ── 409 BUSY helper ────────────────────────────────────────────
        // The server returns HTTP 409 with `{busy: true}` while another
        // super-admin holds the candidate-mutation lock. Both single-row
        // and bulk endpoints share this shape, so we centralise the check
        // here. Returns true when the response was a busy bounce so the
        // caller can short-circuit (keep modal open, surface toast).
        //
        // Why we don't pre-show the overlay locally: the server broadcasts
        // `candidates_lock busy=true` BEFORE starting the slow part, so
        // the holder's overlay appears via the WebSocket handler. That
        // path also knows when to hide — `busy=false` on release. Doing
        // it locally would race with the broadcast and risk flashing.
        function isBusyResponse(response, data) {
            return response && response.status === 409 && data && data.busy === true;
        }

        // ── Add ─────────────────────────────────────────────────────────
        async function addCandidate(afterRow) {
            if (!createUrl) return;
            const fd = new FormData();
            // Empty body → server allocates a unique sample SBD with default
            // names ("Nguyễn Văn A", "THCS ABC") that match the spec.
            try {
                const { response, data } = await postJson(createUrl, fd);
                if (isBusyResponse(response, data)) {
                    showToast(data.error, "warning");
                    return;
                }
                if (!response.ok || !data || !data.ok) {
                    showToast(data?.error || "Không thể thêm thí sinh.", "danger");
                    return;
                }
                const newRow = buildRowEl(data.candidate);
                if (afterRow && afterRow.parentNode === tbody) {
                    afterRow.after(newRow);
                } else {
                    tbody.appendChild(newRow);
                }
                refreshAfterMutation();
                flashRow(newRow);
                showToast(`Đã thêm ${data.candidate.sbd}.`, "success");
            } catch (e) {
                showToast("Lỗi mạng khi thêm thí sinh.", "danger");
            }
        }

        // ── Edit (modal) ────────────────────────────────────────────────
        // Fields tracked by the dirty-detection / no-change-no-op logic.
        // The keys are the form-field names (sent to the server); the values
        // are the corresponding DOM input IDs in the modal.
        const EDIT_FIELDS = Object.freeze({
            sbd: "cef-sbd",
            full_name: "cef-full-name",
            school: "cef-school",
            supervisor_teacher: "cef-supervisor-teacher",
            exam_room: "cef-exam-room",
        });
        // Baseline snapshot of field values captured when the modal opens.
        // Empty object means "modal not currently open / no baseline".
        let editBaseline = null;
        // When true, a programmatic .hide() has been pre-approved by the
        // unsaved-changes confirm flow (or by a successful save) and should
        // NOT trigger the dismiss-guard again. Cleared on `hidden.bs.modal`.
        let bypassDismissGuard = false;

        function readEditFieldValues() {
            const out = {};
            for (const [name, id] of Object.entries(EDIT_FIELDS)) {
                const el = document.getElementById(id);
                // Trim on read so trailing whitespace alone is not treated as
                // a change (matches what the server would normalise away).
                out[name] = (el ? (el.value || "") : "").trim();
            }
            return out;
        }

        function isEditModalDirty() {
            if (!editBaseline) return false;
            const cur = readEditFieldValues();
            for (const k of Object.keys(EDIT_FIELDS)) {
                if ((cur[k] || "") !== (editBaseline[k] || "")) return true;
            }
            return false;
        }

        function openEditModal(tr) {
            if (!editModal || !editForm) return;
            const setVal = (id, v) => {
                const el = document.getElementById(id);
                if (el) el.value = v || "";
            };
            setVal("cef-id", tr.dataset.candidateId || "");
            setVal("cef-sbd", tr.dataset.sbd || "");
            setVal("cef-full-name", tr.dataset.fullName || "");
            setVal("cef-school", tr.dataset.school || "");
            setVal("cef-supervisor-teacher", tr.dataset.supervisorTeacher || "");
            setVal("cef-exam-room", tr.dataset.examRoom || "");
            const errBox = document.getElementById("cef-error");
            if (errBox) {
                errBox.textContent = "";
                errBox.classList.add("d-none");
            }
            editForm.dataset.targetRowId = tr.dataset.candidateId || "";

            // Capture the baseline AFTER the inputs are populated so the
            // dirty-check has the right anchor point.
            editBaseline = readEditFieldValues();
            bypassDismissGuard = false;

            editModal.show();
            setTimeout(() => {
                const sbdInput = document.getElementById("cef-sbd");
                if (sbdInput) sbdInput.focus();
            }, 50);
        }

        async function submitEdit(ev) {
            ev.preventDefault();
            if (!editForm) return;
            const id = (document.getElementById("cef-id") || {}).value;
            if (!id) return;

            const errBox = document.getElementById("cef-error");
            const saveBtn = document.getElementById("cef-save-btn");
            const showError = (msg) => {
                if (errBox) {
                    errBox.textContent = msg;
                    errBox.classList.remove("d-none");
                }
            };
            const clearError = () => {
                if (errBox) {
                    errBox.textContent = "";
                    errBox.classList.add("d-none");
                }
            };
            clearError();

            // 3.1 — If nothing has changed since the modal opened, treat
            // Save as a silent close: don't hit the database, don't show a
            // success toast. This mirrors the user's expectation that "Lưu
            // không có thay đổi" should behave identically to "Huỷ".
            if (!isEditModalDirty()) {
                bypassDismissGuard = true;
                editModal?.hide();
                return;
            }

            const fd = new FormData();
            fd.append("sbd", (document.getElementById("cef-sbd") || {}).value || "");
            fd.append("full_name", (document.getElementById("cef-full-name") || {}).value || "");
            fd.append("school", (document.getElementById("cef-school") || {}).value || "");
            fd.append("supervisor_teacher", (document.getElementById("cef-supervisor-teacher") || {}).value || "");
            fd.append("exam_room", (document.getElementById("cef-exam-room") || {}).value || "");

            if (saveBtn) saveBtn.disabled = true;
            try {
                const { response, data } = await postJson(`/api/candidates/${encodeURIComponent(id)}/update/`, fd);
                // 409 BUSY: keep the modal open with the user's draft so they
                // can retry once the holder releases the lock. We surface
                // the message via a top-of-screen toast AND inside the modal
                // so it's impossible to miss whichever way the user looks.
                if (isBusyResponse(response, data)) {
                    showError(data.error);
                    showToast(data.error, "warning");
                    return;
                }
                if (!response.ok || !data || !data.ok) {
                    showError(data?.error || "Không thể lưu thay đổi.");
                    return;
                }
                const tr = tbody.querySelector(`tr[data-candidate-id="${CSS.escape(String(id))}"]`);
                if (tr) updateRowFromCandidate(tr, data.candidate);
                // Successful save → bypass the dismiss-guard so .hide() is
                // immediate (no spurious "discard?" prompt on a clean modal).
                bypassDismissGuard = true;
                editModal?.hide();
                showToast(`Đã cập nhật ${data.candidate.sbd}.`, "success");
            } catch (e) {
                showError("Lỗi mạng khi cập nhật thí sinh.");
            } finally {
                if (saveBtn) saveBtn.disabled = false;
            }
        }

        // ── Single delete ───────────────────────────────────────────────
        async function deleteCandidate(tr) {
            const id = tr.dataset.candidateId;
            if (!id) return;
            const sbd = tr.dataset.sbd || `#${id}`;
            const ok = await showConfirmDialog({
                title: "Xoá thí sinh?",
                message: `Bạn có chắc muốn xoá thí sinh ${sbd}? Tin nhắn cũ liên quan vẫn được giữ, nhưng sẽ hiện "không tìm thấy hồ sơ thí sinh".`,
                okText: "Xoá",
                cancelText: "Huỷ",
            });
            if (!ok) return;

            try {
                const { response, data } = await postJson(
                    `/api/candidates/${encodeURIComponent(id)}/delete/`,
                    new FormData(),
                );
                if (isBusyResponse(response, data)) {
                    showToast(data.error, "warning");
                    return;
                }
                if (!response.ok || !data || !data.ok) {
                    showToast(data?.error || "Không thể xoá thí sinh.", "danger");
                    return;
                }
                tr.remove();
                refreshAfterMutation();
                showToast(`Đã xoá ${sbd}.`, "success");
            } catch (e) {
                showToast("Lỗi mạng khi xoá thí sinh.", "danger");
            }
        }

        // ── Bulk delete ─────────────────────────────────────────────────
        async function bulkDelete() {
            const ids = getSelectedIds();
            if (!ids.length) return;
            const ok = await showConfirmDialog({
                title: `Xoá ${ids.length} thí sinh?`,
                message: `Bạn có chắc muốn xoá ${ids.length} thí sinh đã chọn? Thao tác này không thể hoàn tác. Tin nhắn cũ liên quan vẫn được giữ.`,
                okText: "Xoá",
                cancelText: "Huỷ",
            });
            if (!ok) return;

            const fd = new FormData();
            ids.forEach((id) => fd.append("ids", String(id)));

            if (bulkDeleteBtn) bulkDeleteBtn.disabled = true;
            try {
                const { response, data } = await postJson(bulkDeleteUrl, fd);
                if (isBusyResponse(response, data)) {
                    showToast(data.error, "warning");
                    return;
                }
                if (!response.ok || !data || !data.ok) {
                    showToast(data?.error || "Không thể xoá hàng loạt.", "danger");
                    return;
                }
                // Remove the rows we requested. The server returned the exact
                // ``ids`` list it processed; trust that for the DOM update.
                (data.ids || ids).forEach((id) => {
                    const tr = tbody.querySelector(`tr[data-candidate-id="${CSS.escape(String(id))}"]`);
                    if (tr) tr.remove();
                });
                refreshAfterMutation();
                showToast(`Đã xoá ${data.deleted ?? ids.length} thí sinh.`, "success");
            } catch (e) {
                showToast("Lỗi mạng khi xoá hàng loạt.", "danger");
            } finally {
                if (bulkDeleteBtn) bulkDeleteBtn.disabled = false;
            }
        }

        // ── Wire events ─────────────────────────────────────────────────

        // Delegated row-action clicks (keeps working after rows are added/removed).
        tbody.addEventListener("click", (ev) => {
            const target = ev.target;
            if (!(target instanceof Element)) return;

            // Don't intercept the SBD-detail button — it has its own handler
            // elsewhere in this file (js-open-candidate-detail).
            if (target.closest(".js-open-candidate-detail")) return;

            const addBtn = target.closest(".js-candidate-add");
            if (addBtn) {
                ev.preventDefault();
                const tr = addBtn.closest("tr");
                addCandidate(tr);
                return;
            }
            const editBtn = target.closest(".js-candidate-edit");
            if (editBtn) {
                ev.preventDefault();
                const tr = editBtn.closest("tr");
                if (tr) openEditModal(tr);
                return;
            }
            const delBtn = target.closest(".js-candidate-delete");
            if (delBtn) {
                ev.preventDefault();
                const tr = delBtn.closest("tr");
                if (tr) deleteCandidate(tr);
                return;
            }
        });

        // Delegated checkbox change → keep master + counters in sync.
        tbody.addEventListener("change", (ev) => {
            const target = ev.target;
            if (!(target instanceof Element)) return;
            if (target.classList.contains("js-candidate-row-cb")) {
                syncSelectionUI();
            }
        });

        // Master checkbox toggles all visible rows.
        if (masterCb) {
            masterCb.addEventListener("change", () => {
                const next = masterCb.checked;
                rowCheckboxes().forEach((cb) => { cb.checked = next; });
                masterCb.indeterminate = false;
                syncSelectionUI();
            });
        }

        if (bulkDeleteBtn) {
            bulkDeleteBtn.addEventListener("click", (ev) => {
                ev.preventDefault();
                bulkDelete();
            });
        }

        if (emptyAddBtn) {
            emptyAddBtn.addEventListener("click", (ev) => {
                ev.preventDefault();
                addCandidate(null);
            });
        }

        if (editForm) {
            editForm.addEventListener("submit", submitEdit);
        }

        // ── 3.2 — Confirm-on-discard for unsaved changes ──────────────────
        // Bootstrap fires `hide.bs.modal` for ALL ways a modal closes:
        // the × button, the "Huỷ" footer button, backdrop click, and Esc.
        // Cancelling the event keeps the modal open.
        if (editModalEl) {
            editModalEl.addEventListener("hide.bs.modal", (ev) => {
                if (bypassDismissGuard) return;
                if (!isEditModalDirty()) return;
                ev.preventDefault();
                showConfirmDialog({
                    title: "Bỏ thay đổi chưa lưu?",
                    message:
                        "Bạn có thay đổi chưa lưu trong thông tin thí sinh. " +
                        "Đóng hộp thoại sẽ bỏ các thay đổi này.",
                    okText: "Bỏ thay đổi",
                    cancelText: "Tiếp tục sửa",
                    variant: "dark",
                }).then((ok) => {
                    if (!ok) return;
                    bypassDismissGuard = true;
                    editModal?.hide();
                });
            });

            // Reset baseline + bypass flag once the modal is fully hidden so
            // the next `openEditModal` starts from a clean slate.
            editModalEl.addEventListener("hidden.bs.modal", () => {
                editBaseline = null;
                bypassDismissGuard = false;
            });
        }

        // Helper: is the edit modal currently open AND dirty? Used by the
        // page-leave guards below.
        function editModalIsOpenAndDirty() {
            if (!editModalEl) return false;
            // Bootstrap toggles the .show class on the modal element while
            // visible; checking it avoids a hard dependency on the bootstrap
            // JS API in case that is ever loaded lazily.
            if (!editModalEl.classList.contains("show")) return false;
            return isEditModalDirty();
        }

        // Same shape as the composer's anchor-click guard so the user gets
        // a consistent confirm dialog rather than the (often-suppressed)
        // native beforeunload prompt when they click an in-app link.
        document.addEventListener("click", (ev) => {
            if (!editModalIsOpenAndDirty()) return;
            const a = ev.target.closest("a[href]");
            if (!a) return;
            // Modal-internal anchors (rare, but possible — e.g., footer
            // links) shouldn't trip the guard.
            if (editModalEl && editModalEl.contains(a)) return;
            if (a.target && a.target !== "_self") return;
            if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey || ev.button === 1) return;
            let url;
            try { url = new URL(a.href, window.location.href); } catch (_) { return; }
            if (url.origin !== window.location.origin) return;
            if (a.hash && url.pathname === window.location.pathname && url.search === window.location.search) return;
            ev.preventDefault();
            showConfirmDialog({
                title: "Rời trang?",
                message: "Bạn có thay đổi chưa lưu trong thông tin thí sinh. Rời đi sẽ bỏ các thay đổi này.",
                okText: "Rời đi",
                cancelText: "Ở lại",
                variant: "dark",
            }).then((ok) => {
                if (!ok) return;
                bypassDismissGuard = true;
                window.location.href = a.href;
            });
        }, true);

        // Native beforeunload prompt for tab close / refresh / address-bar
        // navigation. The browser shows its own generic copy here (returnValue
        // text is ignored by modern engines) but the prompt itself still
        // appears, which is what the user expects.
        window.addEventListener("beforeunload", (ev) => {
            if (!editModalIsOpenAndDirty()) return undefined;
            ev.preventDefault();
            ev.returnValue = "Có thay đổi chưa lưu, bạn có chắc chắn muốn rời đi?";
            return ev.returnValue;
        });

        // Initial sync (esp. for the disabled-master state on empty rosters).
        refreshAfterMutation();
    }

    // ── Statistics page: confirm CSV overwrite before submitting ─────────
    function initCandidateImportConfirm() {
        const form = document.getElementById("candidate-import-form");
        if (!form) return;

        let confirmed = false;
        form.addEventListener("submit", (ev) => {
            if (confirmed) return; // already approved, let it through
            const file = form.querySelector('input[type="file"][name="csv_file"]');
            if (!file || !file.files || !file.files.length) return; // browser will block
            ev.preventDefault();
            showConfirmDialog({
                title: "Ghi đè danh sách thí sinh?",
                message:
                    "Thao tác này sẽ XOÁ TOÀN BỘ danh sách thí sinh hiện có và thay bằng nội dung trong tệp CSV. " +
                    "Tin nhắn cũ vẫn được giữ, nhưng các SBD không còn trong tệp mới sẽ hiện 'không tìm thấy hồ sơ thí sinh'.",
                okText: "Ghi đè",
                cancelText: "Huỷ",
                variant: "dark",
            }).then((ok) => {
                if (!ok) return;
                confirmed = true;
                form.submit();
            });
        });
    }

    // ── Incident bulk select / delete (chat page) ────────────────────────
    function initIncidentBulkSelect() {
        const toggleBtn = document.getElementById("incident-bulk-select-toggle");
        const actionbar = document.getElementById("incidentBulkActionbar");
        if (!toggleBtn || !actionbar) return; // Page does not expose this UI.

        const cancelBtn = document.getElementById("incidentBulkCancelBtn");
        const deleteBtn = document.getElementById("incidentBulkDeleteBtn");
        const masterCb = document.getElementById("incidentBulkMasterCb");
        const countEls = actionbar.querySelectorAll(".js-bulk-count");
        const maxEls = actionbar.querySelectorAll(".js-bulk-max");
        const toggleLabel = toggleBtn.querySelector(".js-toggle-label");

        // Pre-populate the "(tối đa N)" hint from the JS-side constant so
        // updating the cap is a one-place change.
        maxEls.forEach((el) => { el.textContent = String(INCIDENT_BULK_DELETE_MAX); });

        let active = false;
        // Cache of "all deletable IDs across DB, capped at MAX newest" —
        // refreshed each time the master toggles ON. Holding this on the
        // module lets us recognise indeterminate vs full-pool selection
        // without re-querying every checkbox change.
        let masterPool = []; // array of incident IDs (numbers)

        function setCount(n) {
            countEls.forEach((el) => { el.textContent = String(n); });
            if (deleteBtn) deleteBtn.disabled = (n <= 0);
        }

        function getRowCheckboxes() {
            return Array.from(document.querySelectorAll(".js-incident-bulk-cb"));
        }

        function getSelectedIds() {
            return getRowCheckboxes()
                .filter((cb) => cb.checked)
                .map((cb) => Number(cb.dataset.incidentId))
                .filter((id) => Number.isFinite(id) && id > 0);
        }

        function syncMasterIndicator() {
            if (!masterCb) return;
            // DOM-based rule:
            //   * 0 deletable rows ticked → unchecked (regardless of pool).
            //   * Every rendered deletable row ticked AND every id in the
            //     cached masterPool is ticked → checked (master "full").
            //   * Anything in between → indeterminate.
            // Using the rendered set means "new row arrived while user had
            // ticked all" naturally drops to indeterminate, as the spec
            // requires for live arrivals during selection mode.
            const cbs = getRowCheckboxes();
            const totalRendered = cbs.length;
            const selected = getSelectedIds();
            const selectedCount = selected.length;
            if (selectedCount === 0) {
                masterCb.checked = false;
                masterCb.indeterminate = false;
                return;
            }
            const poolSize = masterPool.length;
            const allRenderedTicked = totalRendered > 0
                && selectedCount === totalRendered;
            const poolFullyTicked = poolSize === 0
                || (selectedCount >= Math.min(poolSize, INCIDENT_BULK_DELETE_MAX)
                    && masterPool.every((id) => selected.includes(id)));
            if (allRenderedTicked && poolFullyTicked) {
                masterCb.checked = true;
                masterCb.indeterminate = false;
            } else {
                masterCb.checked = false;
                masterCb.indeterminate = true;
            }
        }

        function syncCount() {
            setCount(getSelectedIds().length);
            syncMasterIndicator();
        }

        function enterMode() {
            if (active) return;
            active = true;
            document.body.classList.add("selection-mode-active");
            actionbar.hidden = false;
            actionbar.setAttribute("aria-hidden", "false");
            toggleBtn.setAttribute("aria-pressed", "true");
            toggleBtn.classList.remove("btn-outline-dark");
            toggleBtn.classList.add("btn-dark");
            if (toggleLabel) toggleLabel.textContent = "Đang chọn nhiều";
            syncCount();
            // Composer just got hidden + action bar took its place →
            // dock height changed → refresh the offset so the last
            // bubble keeps its breathing room.
            if (typeof syncComposerOffset === "function") syncComposerOffset();
            // The bulk-busy notice rule depends on selection-mode too —
            // re-apply it so a busy notice that was up doesn't linger.
            if (typeof applyBulkBusyState === "function") applyBulkBusyState();
        }

        function exitMode() {
            if (!active) return;
            active = false;
            document.body.classList.remove("selection-mode-active");
            actionbar.hidden = true;
            actionbar.setAttribute("aria-hidden", "true");
            toggleBtn.setAttribute("aria-pressed", "false");
            toggleBtn.classList.add("btn-outline-dark");
            toggleBtn.classList.remove("btn-dark");
            if (toggleLabel) toggleLabel.textContent = "Chọn nhiều tin nhắn";
            // Clear all checkboxes so re-entering mode starts fresh — the
            // spec calls for state NOT to persist across page reloads, and
            // not persisting across mode toggles is the natural extension.
            getRowCheckboxes().forEach((cb) => { cb.checked = false; });
            setCount(0);
            if (typeof syncComposerOffset === "function") syncComposerOffset();
            if (typeof applyBulkBusyState === "function") applyBulkBusyState();
        }

        toggleBtn.addEventListener("click", (ev) => {
            ev.preventDefault();
            if (active) exitMode(); else enterMode();
        });

        if (cancelBtn) {
            cancelBtn.addEventListener("click", (ev) => {
                ev.preventDefault();
                exitMode();
            });
        }

        // Per-row checkbox change → enforce the manual cap and keep the
        // counter in sync. We let the user untick freely; we only block
        // ticking past the cap.
        document.addEventListener("change", (ev) => {
            const target = ev.target;
            if (!(target instanceof Element)) return;
            if (!target.classList.contains("js-incident-bulk-cb")) return;
            if (target.checked) {
                const total = getSelectedIds().length;
                if (total > INCIDENT_BULK_DELETE_MAX) {
                    target.checked = false; // roll back the offending tick
                    showToast(INCIDENT_BULK_OVER_LIMIT_MSG, "warning");
                    return;
                }
            }
            // Master no longer authoritative once the user touches a row
            // by hand — masterPool stays as the last-known pool, but the
            // indicator slides to the indeterminate / unchecked state via
            // syncMasterIndicator's pool-equality check.
            syncCount();
        });

        // Click-anywhere-on-bubble → toggle that row's checkbox while in
        // selection mode. Document-level delegation means lazy-loaded /
        // newly-arrived rows pick this up automatically without re-binding.
        // CAPTURE phase so we win the race against any inner listener
        // that might call ``stopPropagation`` (mention chips, lightgallery,
        // evidence handlers, etc.). The bubble's children also have
        // ``pointer-events: none`` via CSS in selection mode, so most
        // inner clicks don't even fire — this is belt-and-suspenders.
        document.addEventListener("click", (ev) => {
            if (!active) return;
            const target = ev.target instanceof Element ? ev.target : null;
            if (!target) return;
            // The actual checkbox / its label wraps the input — let the
            // browser handle it natively, our `change` listener picks up
            // the resulting state.
            if (target.closest(".chat-bulk-cb-wrap")) return;
            // Find the enclosing chat row. If we're not inside one, this
            // click has nothing to do with us.
            const row = target.closest(".chat-row");
            if (!row) return;
            // Non-deletable rows: still swallow clicks inside their
            // bubble so mention pop-ups, links, etc. stay disabled —
            // but don't pretend we selected anything.
            if (!row.classList.contains("chat-row-deletable")) {
                if (target.closest(".chat-bubble")) {
                    ev.preventDefault();
                    ev.stopPropagation();
                }
                return;
            }
            // Deletable row: only react when the click landed inside the
            // bubble itself. Clicks that miss the bubble (gutters, etc.)
            // do nothing.
            if (!target.closest(".chat-bubble")) return;
            const cb = row.querySelector(".js-incident-bulk-cb");
            if (!cb) return;
            ev.preventDefault();
            ev.stopPropagation();
            // Cap-respecting toggle: if turning ON would exceed the cap,
            // refuse and toast — same rule as the native cb change path.
            if (!cb.checked) {
                const wouldBeTotal = getSelectedIds().length + 1;
                if (wouldBeTotal > INCIDENT_BULK_DELETE_MAX) {
                    showToast(INCIDENT_BULK_OVER_LIMIT_MSG, "warning");
                    return;
                }
            }
            cb.checked = !cb.checked;
            // Manually fire `change` so the existing change-listener
            // path (cap re-check + counter sync) runs uniformly.
            cb.dispatchEvent(new Event("change", { bubbles: true }));
        }, true);

        // ── Master "select all deletable" ────────────────────────────────
        // Fetches up to ``INCIDENT_BULK_DELETE_MAX`` newest deletable IDs
        // across the entire DB (not just what's rendered), then ticks
        // every row that's currently visible. Rows not yet lazy-loaded
        // are NOT in the DOM — they will not be deleted by this master
        // tick. The user must scroll up to materialise them and the per-
        // row tick handler keeps the cap honest.
        async function fetchDeletablePool() {
            const res = await fetch("/api/incidents/deletable-ids/", {
                headers: { "X-Requested-With": "XMLHttpRequest" },
                credentials: "same-origin",
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            return data && Array.isArray(data.ids) ? data.ids.map(Number) : [];
        }

        async function masterTickAll() {
            try {
                masterPool = await fetchDeletablePool();
            } catch (_) {
                showToast("Không thể tải danh sách tin nhắn có thể xoá.", "danger");
                masterCb.checked = false;
                masterCb.indeterminate = false;
                return;
            }
            if (!masterPool.length) {
                showToast("Không có tin nhắn nào trong số bạn có thể xoá.", "warning");
                masterCb.checked = false;
                masterCb.indeterminate = false;
                return;
            }
            const poolSet = new Set(masterPool);
            // Only tick rows that are both rendered AND in the pool. We
            // never tick more than the cap (the server already capped the
            // pool at MAX, but we re-check defensively).
            let ticked = 0;
            getRowCheckboxes().forEach((cb) => {
                const id = Number(cb.dataset.incidentId);
                const shouldTick = poolSet.has(id) && ticked < INCIDENT_BULK_DELETE_MAX;
                cb.checked = shouldTick;
                if (shouldTick) ticked += 1;
            });
            syncCount();
        }

        function masterUntickAll() {
            getRowCheckboxes().forEach((cb) => { cb.checked = false; });
            masterPool = [];
            syncCount();
        }

        if (masterCb) {
            masterCb.addEventListener("change", () => {
                if (masterCb.checked) {
                    masterTickAll();
                } else {
                    masterUntickAll();
                }
            });
        }

        // Bulk delete action.
        if (deleteBtn) {
            deleteBtn.addEventListener("click", async (ev) => {
                ev.preventDefault();
                const ids = getSelectedIds();
                if (!ids.length) return;
                if (ids.length > INCIDENT_BULK_DELETE_MAX) {
                    showToast(INCIDENT_BULK_OVER_LIMIT_MSG, "warning");
                    return;
                }
                const ok = await showConfirmDialog({
                    title: `Xoá ${ids.length} tin nhắn?`,
                    message:
                        `Bạn có chắc muốn xoá ${ids.length} tin nhắn đã chọn? ` +
                        "Tin nhắn và file đính kèm sẽ bị xoá vĩnh viễn và không thể khôi phục.",
                    okText: "Xoá",
                    cancelText: "Huỷ",
                });
                if (!ok) return;

                const params = new URLSearchParams();
                const csrf = getCsrfToken();
                if (csrf) params.append("csrfmiddlewaretoken", csrf);
                ids.forEach((id) => params.append("ids", String(id)));

                deleteBtn.disabled = true;
                try {
                    const res = await fetch("/api/incidents/bulk-delete/", {
                        method: "POST",
                        headers: {
                            "X-CSRFToken": csrf,
                            "X-Requested-With": "XMLHttpRequest",
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        },
                        credentials: "same-origin",
                        body: params.toString(),
                    });
                    let data = null;
                    try { data = await res.json(); } catch (_) {}
                    if (!res.ok || !data || !data.ok) {
                        showToast(data?.error || "Không thể xoá hàng loạt.", "danger");
                        return;
                    }
                    // Drop the deleted rows from the DOM; the WebSocket
                    // broadcast will additionally trigger a load-fresh on
                    // every other connected client.
                    (data.deleted_ids || []).forEach((id) => {
                        const row = document.querySelector(`.chat-row[data-incident-id="${CSS.escape(String(id))}"]`);
                        if (row) row.remove();
                    });
                    let summary = `Đã xoá ${data.deleted_count || 0} tin nhắn.`;
                    if (data.forbidden_ids && data.forbidden_ids.length) {
                        summary += ` (Bỏ qua ${data.forbidden_ids.length} tin nhắn không đủ quyền.)`;
                    }
                    showToast(summary, "success");
                    // Auto-disable selection mode after a successful bulk
                    // delete (per spec); the user can re-enter if needed.
                    exitMode();
                    // Refresh the bound newest/oldest pointers so the
                    // load-older/loadNewMessages state stays consistent.
                    if (typeof recomputeIncidentBounds === "function") {
                        recomputeIncidentBounds();
                    }
                } catch (e) {
                    showToast("Lỗi mạng khi xoá hàng loạt tin nhắn.", "danger");
                } finally {
                    deleteBtn.disabled = false;
                }
            });
        }

        // Esc to exit selection mode (when no other modal is on top of us).
        document.addEventListener("keydown", (ev) => {
            if (!active) return;
            if (ev.key !== "Escape") return;
            // Don't fight with Bootstrap modals / confirm dialog.
            if (document.querySelector(".modal.show")) return;
            const confirmDlg = document.getElementById("appConfirmDialog");
            if (confirmDlg && !confirmDlg.hidden) return;
            exitMode();
        });

        // Expose the small surface other modules need (the WS message
        // handler and the prepend/append paths). Keeping this object
        // narrow avoids the temptation to reach into private state.
        incidentBulkSelectApi = {
            syncCount,
            notifyDomChanged: () => syncCount(),
            isActive: () => active,
        };
    }

    bindEvidenceGuards(document);
    bindEvidencePlaceholders(document);
    bindLightGallery(document);
    bindSbdHoverTooltips(document);
    formatLocalTimestamps(document);
    initComposeBar();
    initEditIncidentPage();
    initCandidateListToggle();
    initCandidateImportConfirm();
    initCandidateManager();
    initIncidentBulkSelect();
    syncComposerOffset();

    // Wire CURRENT_USER_ID from <body data-user-id="..."> so the lock event
    // handler can recognise itself as the holder vs a third-party admin.
    {
        const rawUid = document.body && document.body.dataset && document.body.dataset.userId;
        const parsed = Number.parseInt(rawUid || "", 10);
        if (Number.isFinite(parsed) && parsed > 0) CURRENT_USER_ID = parsed;
    }

    // When a candidate row changes, refresh the visible stats table on the
    // statistics page (best-effort: we re-fetch the live snapshot which
    // already includes a stats_html block). The dashboard page also benefits
    // because tooltip caches were invalidated upstream.
    onCandidatesChanged(async () => {
        const statsContainer = document.getElementById("stats-table-container");
        if (!statsContainer) return;
        try {
            const res = await fetch("/api/live/", { headers: { "X-Requested-With": "XMLHttpRequest" } });
            if (!res.ok) return;
            const payload = await res.json();
            if (payload && payload.stats_html) {
                statsContainer.innerHTML = payload.stats_html;
            }
        } catch (_) { /* swallow — best-effort refresh */ }
    });

    window.addEventListener("resize", syncComposerOffset, { passive: true });

    if (composerDock && window.ResizeObserver) {
        const composeObserver = new ResizeObserver(syncComposerOffset);
        composeObserver.observe(composerDock);
    }

    if (incidentFeed) {
        window.addEventListener("scroll", () => {
            if (window.scrollY < 80) loadOlderMessages();
        }, { passive: true });
    }

    if (monitorRoot) {
        forceInitialBottomScroll();
        if (composerForm) composerForm.addEventListener("submit", handleComposerSubmit);
        connectLiveSocket();
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) {
                connectLiveSocket();
                loadNewMessages(false);
            }
        });
    }
})();
