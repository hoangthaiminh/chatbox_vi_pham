(function () {
    "use strict";

    const monitorRoot = document.getElementById("messenger-shell") || document.getElementById("monitor-tabs-content");
    const incidentFeed = document.getElementById("incident-feed");
    const incidentTopStatus = document.getElementById("incident-top-status");
    const incidentListContainer = document.getElementById("incident-list-container");
    const composerForm = document.getElementById("create-incident-form") || document.querySelector(".composer-form");
    const composerSubmitButton = composerForm ? composerForm.querySelector('button[type="submit"]') : null;
    const composerDock = document.querySelector(".compose-bar") || document.querySelector(".composer-dock");
    const statsTableContainer = document.getElementById("stats-table-container");
    const liveConnectionStatus = document.getElementById("live-connection-status");
    const detailContent = document.getElementById("candidate-detail-content");
    const detailCanvasEl = document.getElementById("candidateDetailCanvas");
    const detailCanvas = detailCanvasEl ? new bootstrap.Offcanvas(detailCanvasEl) : null;

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
        if (!inputEl) return;
        const feedback = document.getElementById('sbd-error');
        const setInvalid = (msg) => {
            inputEl.classList.add('is-invalid');
            inputEl.classList.remove('is-valid');
            if (feedback) feedback.textContent = msg || 'SBD không hợp lệ.';
        };
        const setValid = () => {
            inputEl.classList.remove('is-invalid');
            inputEl.classList.add('is-valid');
            if (feedback) feedback.textContent = '';
        };

        function validate() {
            const v = (inputEl.value || '').trim();
            if (!v) {
                setInvalid('SBD không được để trống.');
                return false;
            }
            if (!isValidSbdSyntax(v)) {
                setInvalid('SBD phải từ 1 đến 9 ký tự, chỉ gồm chữ cái và chữ số.');
                return false;
            }
            setValid();
            return true;
        }

        inputEl.addEventListener('input', () => {
            // live validate but don't be aggressive
            if (inputEl.value === '') {
                inputEl.classList.remove('is-valid', 'is-invalid');
                if (feedback) feedback.textContent = '';
                return;
            }
            validate();
        });

        // return validator for use on submit
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

        // client-side SBD validation
        if (typeof composeSbdValidate === 'function') {
            if (!composeSbdValidate()) {
                showToast('SBD không hợp lệ.', 'danger');
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
            if (typeof refreshComposeEvidenceIndicator === "function") {
                refreshComposeEvidenceIndicator();
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
            const response = await fetch(form.action, {
                method: "POST",
                headers: {
                    "X-CSRFToken": getCsrfToken(),
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: new FormData(form),
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
                if (payload.type === "live_event") loadNewMessages(false);
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

    function validateEvidenceInput(input) {
        if (!input || !input.files || !input.files[0]) return true;
        const file = input.files[0];
        if (fileLooksLikeVideo(file) && file.size > CLIENT_MAX_VIDEO_BYTES) {
            showToast(
                `Video quá lớn (${formatFileSize(file.size)}). Dung lượng tối đa là 40 MB.`,
                "danger",
            );
            input.value = "";
            return false;
        }
        return true;
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

        // add a clear (trash) button to allow removing a selected file
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
                const oldInput = getEvidenceInput();
                if (oldInput) {
                    try {
                        const newInput = oldInput.cloneNode(true);
                        newInput.value = '';
                        oldInput.parentNode.replaceChild(newInput, oldInput);
                        newInput.addEventListener('change', () => {
                            if (!validateEvidenceInput(newInput)) {
                                updateEvidenceIndicator();
                                return;
                            }
                            updateEvidenceIndicator();
                        });
                    } catch (_) {
                        oldInput.value = '';
                    }
                }
                updateEvidenceIndicator();
            });
        }

        const initialInput = getEvidenceInput();
        if (initialInput) {
            initialInput.addEventListener("change", () => {
                const curEl = getEvidenceInput();
                if (!curEl) return;
                if (!validateEvidenceInput(curEl)) {
                    updateEvidenceIndicator();
                    return;
                }
                updateEvidenceIndicator();
            });
            updateEvidenceIndicator();
        }

        // --- Unsaved-changes detection for composer (smart compare) ---------
        const getComposerText = () => {
            if (!simpleInput && !fullTextarea) return "";
            const mode = form.dataset.mode || (fullTextarea && fullTextarea.value ? "expanded" : "simple");
            if (mode === "expanded") return (fullTextarea && fullTextarea.value) ? fullTextarea.value : "";
            return (simpleInput && simpleInput.value) ? simpleInput.value : "";
        };

        const initialComposeMode = form.dataset.mode || "simple";
        const initialComposeText = (getComposerText() || "").trim();
        const initialEvidenceHasFile = (getEvidenceInput() && getEvidenceInput().files && getEvidenceInput().files.length > 0) || false;

        function isComposerDirty() {
            const curText = (getComposerText() || "").trim();
            const curEvidenceHasFile = (getEvidenceInput() && getEvidenceInput().files && getEvidenceInput().files.length > 0) || false;
            if (curText !== initialComposeText) return true;
            if (Boolean(curEvidenceHasFile) !== Boolean(initialEvidenceHasFile)) return true;
            // No meaningful changes
            return false;
        }

        form.addEventListener("submit", () => { /* on submit we consider changes saved */ __allowUnload = true; });

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

        // Prevent form submit if SBD invalid
        form.addEventListener('submit', (ev) => {
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
        if (evidenceInput) {
            evidenceInput.addEventListener("change", () => {
                validateEvidenceInput(evidenceInput);
            });
        }
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

    bindEvidenceGuards(document);
    bindEvidencePlaceholders(document);
    bindLightGallery(document);
    bindSbdHoverTooltips(document);
    formatLocalTimestamps(document);
    initComposeBar();
    initEditIncidentPage();
    syncComposerOffset();

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
