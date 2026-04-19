(function () {
    const monitorRoot = document.getElementById("monitor-tabs-content");
    const incidentTopStatus = document.getElementById("incident-top-status");
    const incidentListContainer = document.getElementById("incident-list-container");
    const composerDock = document.querySelector(".composer-dock");
    const composerForm = document.querySelector(".composer-form");
    const composerSubmitButton = composerForm ? composerForm.querySelector('button[type="submit"]') : null;
    const statsTableContainer = document.getElementById("stats-table-container");
    const liveConnectionStatus = document.getElementById("live-connection-status");
    const detailContent = document.getElementById("candidate-detail-content");
    const detailCanvasEl = document.getElementById("candidateDetailCanvas");
    const detailCanvas = detailCanvasEl ? new bootstrap.Offcanvas(detailCanvasEl) : null;
    const evidenceModalEl = document.getElementById("evidencePreviewModal");
    const evidenceModal = evidenceModalEl ? new bootstrap.Modal(evidenceModalEl) : null;
    const evidenceBody = document.getElementById("evidence-preview-body");
    let liveSocket = null;
    let reconnectDelayMs = 1000;
    let loadingOlder = false;
    let loadingUpdates = false;
    let hasInitializedBottomView = false;
    let sendingComposer = false;

    if ("scrollRestoration" in history) {
        history.scrollRestoration = "manual";
    }

    const monitorRoot           = document.getElementById("messenger-shell") || document.getElementById("monitor-tabs-content");
    const incidentFeed          = document.getElementById("incident-feed");
    const incidentTopStatus     = document.getElementById("incident-top-status");
    const incidentListContainer = document.getElementById("incident-list-container");
    const statsTableContainer   = document.getElementById("stats-table-container");
    const liveConnectionStatus  = document.getElementById("live-connection-status");
    const detailContent         = document.getElementById("candidate-detail-content");
    const detailCanvasEl        = document.getElementById("candidateDetailCanvas");
    const detailCanvas          = detailCanvasEl ? new bootstrap.Offcanvas(detailCanvasEl) : null;

    let liveSocket       = null;
    let reconnectDelayMs = 1000;
    let loadingOlder     = false;
    let loadingUpdates   = false;

    function parseId(v) {
        const p = Number.parseInt(v, 10);
        return Number.isFinite(p) ? p : null;
    }

    let oldestId = incidentListContainer ? parseId(incidentListContainer.dataset.oldestId) : null;
    let newestId = incidentListContainer ? parseId(incidentListContainer.dataset.newestId) : null;
    let hasOlder = incidentListContainer ? incidentListContainer.dataset.hasOlder === "1" : false;

    function escHtml(s) {
        return String(s)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    const SBD_SYNTAX_RE  = /^[A-Za-z0-9]{1,9}$/;

    const SBD_SHAPE_RE   = /^(?=.{2,9}$)[A-Za-z]{0,2}\d{2,}$/;

    const SBD_SUGGEST_RE = /(?<![{@])\b([A-Za-z]{0,2}\d{2,9})$/;

    function isValidSbd(s) {
        const v = (s || "").trim();
        return SBD_SYNTAX_RE.test(v) && SBD_SHAPE_RE.test(v);
    }

    function bindEvidenceGuards(scope) {
        (scope || document).querySelectorAll(".evidence-guard").forEach((el) => {
            el.setAttribute("draggable", "false");
            el.addEventListener("dragstart",   (e) => e.preventDefault());
            el.addEventListener("contextmenu", (e) => e.preventDefault());
        });
    }

    function isEvidenceMediaReady(mediaEl) {
        if (!mediaEl) {
            return true;
        }
        if (mediaEl.tagName === "IMG") {
            return mediaEl.complete && mediaEl.naturalWidth > 0;
        }
        if (mediaEl.tagName === "VIDEO") {
            return mediaEl.readyState >= 1;
        }
        return true;
    }

    function applyEvidenceIntrinsicMetrics(wrapEl, mediaEl) {
        if (!wrapEl || !mediaEl) {
            return;
        }

        let width = null;
        let height = null;

        if (mediaEl.tagName === "IMG") {
            width = mediaEl.naturalWidth || null;
            height = mediaEl.naturalHeight || null;
        } else if (mediaEl.tagName === "VIDEO") {
            width = mediaEl.videoWidth || null;
            height = mediaEl.videoHeight || null;
        }

        if (width && height) {
            wrapEl.style.setProperty("--evidence-natural-w", String(width));
            wrapEl.style.setProperty("--evidence-ar", `${width} / ${height}`);
        }
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
                applyEvidenceIntrinsicMetrics(wrapEl, mediaEl);
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

    function blockClipboardForEvidence() {
        document.addEventListener("copy", (e) => {
            if (document.activeElement?.closest(".evidence-wrap,.candidate-detail-shell"))
                e.preventDefault();
        });
        document.addEventListener("keydown", (e) => {
            if ((e.ctrlKey || e.metaKey) && ["s","u","p"].includes(e.key.toLowerCase())) {
                if (document.querySelector(".incident-video-wrap,.incident-legacy-img,.candidate-detail-shell video"))
                    e.preventDefault();
            }
        });
    }

    const LG_LICENSE = "0000-0000-000-0000"; 

    function buildGalleryItems(incidentEl) {
        const items = [];

        incidentEl.querySelectorAll(".markdown-body img").forEach((img) => {
            const src = img.src || img.dataset.src;
            if (src) {
                items.push({ src, thumb: src, subHtml: img.alt ? `<p>${escHtml(img.alt)}</p>` : "" });
            }
        });

        incidentEl.querySelectorAll(".incident-legacy-img").forEach((img) => {
            const src = img.src || img.dataset.lgSrc;
            if (src) items.push({ src, thumb: src });
        });

        const videoWrap = incidentEl.querySelector(".incident-video-wrap");
        if (videoWrap) {
            const videoSrc = videoWrap.dataset.videoSrc;
            if (videoSrc) {
                items.push({
                    video: {
                        source: [{ src: videoSrc, type: "video/mp4" }],
                        attributes: { preload: "metadata", controls: true },
                    },
                    thumb: "",
                    subHtml: "<p>Video Evidence</p>",
                });
            }
        }

        return items;
    }

    function updateTopStatus(text) {
        if (!incidentTopStatus) {
            return;
        }
        incidentTopStatus.textContent = text || "";
    }

    function setComposerSubmittingState(isSubmitting) {
        if (!composerSubmitButton) {
            return;
        }
        composerSubmitButton.disabled = isSubmitting;
    }

    function buildWebsocketUrl() {
        if (!monitorRoot) {
            return "";
        }

        const wsPath = monitorRoot.dataset.wsPath;
        if (!wsPath) {
            return "";
        }

        const protocol = window.location.protocol === "https:" ? "wss" : "ws";
        return `${protocol}://${window.location.host}${wsPath}`;
    }

    function isNearBottom() {
        if (!incidentListContainer) {
            return true;
        }
        const doc = document.documentElement;
        return window.innerHeight + window.scrollY >= doc.scrollHeight - 96;
    }

    function instantScrollTo(topValue) {
        const doc = document.documentElement;
        const previousInlineBehavior = doc.style.scrollBehavior;
        doc.style.scrollBehavior = "auto";
        window.scrollTo(0, Math.max(topValue, 0));
        doc.style.scrollBehavior = previousInlineBehavior;
    }

    function scrollToBottom() {
        if (!incidentListContainer) {
            return;
        }
        syncComposerOffset();
        const doc = document.documentElement;
        instantScrollTo(doc.scrollHeight - window.innerHeight);
    }

    function forceScrollToBottom() {
        scrollToBottom();
        window.requestAnimationFrame(scrollToBottom);
        window.setTimeout(scrollToBottom, 80);
    }

    function initializeBottomView() {
        if (!incidentListContainer || hasInitializedBottomView) {
            return;
        }

        hasInitializedBottomView = true;
        syncComposerOffset();
        forceScrollToBottom();
        window.requestAnimationFrame(() => {
            forceScrollToBottom();
        });
        window.setTimeout(() => {
            forceScrollToBottom();
        }, 180);
    }

    function syncComposerOffset() {
        if (!composerDock) {
            return;
        }
        const composerHeight = composerDock.getBoundingClientRect().height;
        document.documentElement.style.setProperty("--composer-offset", `${Math.ceil(composerHeight)}px`);
    }

    function htmlToNodes(html) {
        const wrapper = document.createElement("div");
        wrapper.innerHTML = html || "";
        return Array.from(wrapper.children).filter((node) => !node.classList.contains("empty-state"));
    }

    function removeEmptyState() {
        if (!incidentListContainer) {
            return;
        }
        const emptyState = incidentListContainer.querySelector(".empty-state");
        if (emptyState) {
            emptyState.remove();
        }
    }

    function prependIncidents(html) {
        if (!incidentListContainer) {
            return 0;
        }

        const nodes = htmlToNodes(html);
        if (!nodes.length) {
            return 0;
        }

        removeEmptyState();
        const doc = document.documentElement;
        const previousHeight = doc.scrollHeight;
        const previousTop = window.scrollY;
        nodes.forEach((node) => incidentListContainer.prepend(node));
        bindEvidenceGuards(incidentListContainer);
        nodes.forEach((node) => bindEvidencePlaceholders(node));
        const heightDelta = doc.scrollHeight - previousHeight;
        instantScrollTo(previousTop + heightDelta);
        return nodes.length;
    }

    function appendIncidents(html) {
        if (!incidentListContainer) {
            return 0;
        }

        const nodes = htmlToNodes(html);
        if (!nodes.length) {
            return 0;
        }

        removeEmptyState();
        nodes.forEach((node) => incidentListContainer.append(node));
        bindEvidenceGuards(incidentListContainer);
        nodes.forEach((node) => bindEvidencePlaceholders(node));
        return nodes.length;
    }

    function mergeStatsHtml(payload) {
        if (statsTableContainer && payload.stats_html) {
            statsTableContainer.innerHTML = payload.stats_html;
        }
    }

    async function loadOlderMessages() {
        if (loadingOlder || !hasOlder || !monitorRoot || !oldestId) {
            return;
        }

        const container = document.createElement("div");
        container.style.display = "none";
        document.body.appendChild(container);

        const plugins = [];
        if (typeof lgZoom  !== "undefined") plugins.push(lgZoom);
        if (typeof lgVideo !== "undefined") plugins.push(lgVideo);

        const lg = lightGallery(container, {
            plugins,
            dynamic: true,
            dynamicEl: items,
            index: startIndex,
            licenseKey: LG_LICENSE,
            speed: 380,
            mobileSettings: { controls: true, showCloseIcon: true, download: false },
            download: false,

            zoom: true,
            showZoomInOutIcons: true,
            actualSize: true,
            scale: 1,
            enableZoomAfter: 300,
            zoomFromOrigin: false,
        });

        requestAnimationFrame(() => lg.openGallery(startIndex));

        container.addEventListener("lgAfterClose", () => {
            lg.destroy();
            container.remove();
        }, { once: true });
    }

    function bindLightGallery(scope) {
        const root = scope || document;

        root.querySelectorAll(".markdown-body img").forEach((img) => {
            if (img.dataset.lgBound) return;
            img.dataset.lgBound = "1";
            img.addEventListener("click", () => {
                const incident = img.closest(".incident-item");
                if (!incident) return;
                const items = buildGalleryItems(incident);
                const idx   = items.findIndex((it) => it.src === (img.src || img.dataset.src));
                openLightGallery(items, Math.max(0, idx));
            });
        });

        root.querySelectorAll(".incident-legacy-img").forEach((img) => {
            if (img.dataset.lgBound) return;
            img.dataset.lgBound = "1";
            img.addEventListener("click", () => {
                const incident = img.closest(".incident-item");
                if (!incident) return;
                const items = buildGalleryItems(incident);
                const mdCount = incident.querySelectorAll(".markdown-body img").length;
                openLightGallery(items, mdCount);
            });
        });

        root.querySelectorAll(".incident-video-wrap").forEach((wrap) => {
            if (wrap.dataset.lgBound) return;
            wrap.dataset.lgBound = "1";
            wrap.addEventListener("click", () => {
                const incident = wrap.closest(".incident-item");
                if (!incident) return;
                const items = buildGalleryItems(incident);

                openLightGallery(items, items.length - 1);
            });
        });
    }

    function updateConnectionStatus(t) { if (liveConnectionStatus) liveConnectionStatus.textContent = t; }
    function updateTopStatus(t)        { if (incidentTopStatus)    incidentTopStatus.textContent = t || ""; }

        const updatesUrl = monitorRoot.dataset.updatesUrl;
        if (!updatesUrl) {
            return;
        }

        loadingUpdates = true;
        const shouldStickBottom = forceStickBottom || isNearBottom();

        try {
            const afterId = newestId || 0;
            const response = await fetch(`${updatesUrl}?after=${encodeURIComponent(afterId)}`, {
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            if (!response.ok) {
                return;
            }

            const payload = await response.json();
            const added = appendIncidents(payload.incidents_html);
            mergeStatsHtml(payload);

            if (payload.newest_id) {
                newestId = payload.newest_id;
            }
            if (oldestId === null && payload.oldest_id) {
                oldestId = payload.oldest_id;
            }

            if (shouldStickBottom && added > 0) {
                forceScrollToBottom();
            }
        } catch (error) {
            console.debug("Update fetch failed:", error);
        } finally {
            loadingUpdates = false;
        }
    }

    async function handleComposerSubmit(event) {
        event.preventDefault();

        if (!composerForm || sendingComposer) {
            return;
        }

        sendingComposer = true;
        setComposerSubmittingState(true);
        updateTopStatus("");

        try {
            const response = await fetch(composerForm.action, {
                method: "POST",
                body: new FormData(composerForm),
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
            });

            const contentType = response.headers.get("content-type") || "";
            const payload = contentType.includes("application/json") ? await response.json() : null;

            if (!response.ok || (payload && payload.ok === false)) {
                const errorText = payload && payload.error ? payload.error : "Could not send message.";
                updateTopStatus(errorText);
                return;
            }

            composerForm.reset();
            await loadNewMessages(true);
            updateTopStatus("");
        } catch (error) {
            updateTopStatus("Could not send message.");
        } finally {
            sendingComposer = false;
            setComposerSubmittingState(false);
        }
    }

    function connectLiveSocket() {
        if (!monitorRoot) return;
        const url = buildWsUrl();
        if (!url) return;
        if (liveSocket && (liveSocket.readyState === WebSocket.OPEN || liveSocket.readyState === WebSocket.CONNECTING)) return;

        updateConnectionStatus("Connecting websocket...");
        try { liveSocket = new WebSocket(url); }
        catch (_) { updateConnectionStatus("Realtime unavailable"); return; }

        try {
            liveSocket = new WebSocket(socketUrl);
        } catch (error) {
            console.debug("Websocket init failed:", error);
            updateConnectionStatus("Realtime unavailable");
            return;
        }

        liveSocket.addEventListener("open", () => {
            reconnectDelayMs = 1000;
            updateConnectionStatus("");
        });
        liveSocket.addEventListener("error",   () => updateConnectionStatus("Realtime reconnecting..."));
        liveSocket.addEventListener("close",   () => {
            liveSocket = null;
            updateConnectionStatus("Realtime disconnected. Reconnecting...");
            setTimeout(connectLiveSocket, reconnectDelayMs);
            reconnectDelayMs = Math.min(reconnectDelayMs * 2, 30000);
        });
    }

    function isNearBottom() {
        const doc = document.documentElement;
        return (doc.scrollHeight - (window.scrollY + window.innerHeight)) < 96;
    }
    function scrollToBottom() {
        window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "auto" });
    }

    function htmlToNodes(html) {
        const w = document.createElement("div");
        w.innerHTML = html || "";
        return Array.from(w.children).filter((n) => !n.classList.contains("empty-state"));
    }

    function removeEmptyState() {
        const e = incidentListContainer?.querySelector(".empty-state");
        if (e) e.remove();
    }

    function afterNodesAdded(scope) {
        bindEvidenceGuards(scope);
        bindLightGallery(scope);
    }

    function prependIncidents(html) {
        if (!incidentListContainer) return 0;
        const nodes = htmlToNodes(html);
        if (!nodes.length) return 0;
        removeEmptyState();
        const prevH = document.documentElement.scrollHeight;
        const prevT = window.scrollY;
        const frag = document.createDocumentFragment();
        nodes.forEach((n) => frag.appendChild(n));
        incidentListContainer.prepend(frag);
        afterNodesAdded(incidentListContainer);
        window.scrollTo({ top: prevT + (document.documentElement.scrollHeight - prevH), behavior: "auto" });
        return nodes.length;
    }

    function appendIncidents(html) {
        if (!incidentListContainer) return 0;
        const nodes = htmlToNodes(html);
        if (!nodes.length) return 0;
        removeEmptyState();
        const frag = document.createDocumentFragment();
        nodes.forEach((n) => frag.appendChild(n));
        incidentListContainer.append(frag);
        afterNodesAdded(incidentListContainer);
        return nodes.length;
    }

    function mergeStatsHtml(payload) {
        if (statsTableContainer && payload.stats_html) statsTableContainer.innerHTML = payload.stats_html;
    }

    async function loadOlderMessages() {
        if (loadingOlder || !hasOlder || !monitorRoot || !oldestId) return;
        const historyUrl = monitorRoot.dataset.historyUrl;
        if (!historyUrl) return;
        loadingOlder = true;
        updateTopStatus("Loading older messages...");
        try {
            const res = await fetch(`${historyUrl}?before=${encodeURIComponent(oldestId)}`, { headers: { "X-Requested-With": "XMLHttpRequest" } });
            if (!res.ok) { updateTopStatus("Failed to load older messages."); return; }
            const p = await res.json();
            prependIncidents(p.incidents_html);
            if (p.oldest_id) oldestId = p.oldest_id;
            if (newestId === null && p.newest_id) newestId = p.newest_id;
            hasOlder = Boolean(p.has_older);
            updateTopStatus(!hasOlder ? "You reached the first message." : "");
        } catch (_) { updateTopStatus("Failed to load older messages."); }
        finally { loadingOlder = false; }
    }

    async function loadNewMessages(forceStick) {
        if (loadingUpdates || !monitorRoot) return;
        const updatesUrl = monitorRoot.dataset.updatesUrl;
        if (!updatesUrl) return;
        loadingUpdates = true;
        const shouldStick = forceStick || isNearBottom();
        try {
            const res = await fetch(`${updatesUrl}?after=${encodeURIComponent(newestId || 0)}`, { headers: { "X-Requested-With": "XMLHttpRequest" } });
            if (!res.ok) return;
            const p = await res.json();
            const added = appendIncidents(p.incidents_html);
            mergeStatsHtml(p);
            if (p.newest_id) newestId = p.newest_id;
            if (oldestId === null && p.oldest_id) oldestId = p.oldest_id;
            if (shouldStick && added > 0) scrollToBottom();
        } catch (e) { console.debug("Update fetch failed:", e); }
        finally { loadingUpdates = false; }
    }

    async function openCandidateDetail(sbd) {
        if (!detailContent || !detailCanvas) return;
        detailContent.innerHTML = '<div class="text-center py-4 text-muted">Loading...</div>';
        detailCanvas.show();
        try {
            const res = await fetch(`/stats/candidate/${encodeURIComponent(sbd)}/`, { headers: { "X-Requested-With": "XMLHttpRequest" } });
            if (!res.ok) { detailContent.innerHTML = '<div class="alert alert-danger">Could not load candidate details.</div>'; return; }
            detailContent.innerHTML = await res.text();
            bindEvidenceGuards(detailContent);
            bindLightGallery(detailContent);
        } catch (_) { detailContent.innerHTML = '<div class="alert alert-danger">Could not load candidate details.</div>'; }
    }

    function initSbdValidation() {
        const sbdInput = document.getElementById("id_sbd");
        const sbdError = document.getElementById("sbd-error");
        if (!sbdInput) return;

        function validate() {
            const v = sbdInput.value.trim();
            if (!v) { sbdInput.classList.remove("is-valid","is-invalid"); if (sbdError) sbdError.textContent = ""; return false; }
            const ok = isValidSbd(v);
            sbdInput.classList.toggle("is-valid",   ok);
            sbdInput.classList.toggle("is-invalid", !ok);
            if (sbdError) sbdError.textContent = ok ? "" : "SBD phải từ 2 đến 9 ký tự, chỉ gồm chữ cái (a-z, A-Z) và/hoặc chữ số (0-9).";
            return ok;
        }

        sbdInput.addEventListener("input", validate);
        sbdInput.addEventListener("blur",  validate);

        const form = sbdInput.closest("form");
        if (form) form.addEventListener("submit", (e) => { if (!validate()) { e.preventDefault(); sbdInput.focus(); } });
    }

    const suggestTip = document.getElementById("mention-suggest-tip");

    let activeTa = null;
    let activeDropdown = null;
    let mentionState = { active: false, startPos: -1, query: "", items: [], activeIdx: -1, fetchTimer: null };

    function dropdownFor(ta) {
        if (!ta) return null;
        const wrap = ta.closest(".mention-textarea-wrap");
        return wrap ? wrap.querySelector(".mention-dropdown") : null;
    }

    async function fetchCandidates(q) {
        try {
            const res = await fetch(`/api/candidates/search/?q=${encodeURIComponent(q)}`, { headers: { "X-Requested-With": "XMLHttpRequest" } });
            if (!res.ok) return [];
            return (await res.json()).results || [];
        } catch (_) { return []; }
    }

    function renderMentionDropdown(items, activeIdx) {
        if (!activeDropdown) return;
        activeDropdown.innerHTML = "";
        if (!items.length) {
            activeDropdown.innerHTML = '<div class="mention-dropdown-empty">Không tìm thấy SBD</div>';
        } else {
            items.forEach((item, i) => {
                const el = document.createElement("div");
                el.className = "mention-item" + (i === activeIdx ? " active" : "");
                el.setAttribute("role", "option");
                el.dataset.sbd = item.sbd;
                el.innerHTML = `<span class="mention-item-sbd">${escHtml(item.sbd)}</span>
                                <span class="mention-item-name">${escHtml(item.full_name)}</span>`;
                el.addEventListener("mousedown", (e) => { e.preventDefault(); selectMention(item.sbd); });
                activeDropdown.appendChild(el);
            });
        }
        activeDropdown.classList.add("open");
        positionMentionDropdown();
    }

    function positionMentionDropdown() {
        if (!activeDropdown || !activeTa || !activeDropdown.classList.contains("open")) return;
        const rect = activeTa.getBoundingClientRect();
        const vh   = window.innerHeight || document.documentElement.clientHeight;
        const vw   = window.innerWidth  || document.documentElement.clientWidth;

        const ddH  = Math.min(activeDropdown.offsetHeight || 220, 220);
        const ddW  = activeDropdown.offsetWidth  || 260;

        let left = rect.left;
        if (left + ddW > vw - 8) left = Math.max(8, vw - ddW - 8);
        if (left < 8) left = 8;

        const spaceAbove = rect.top;
        const spaceBelow = vh - rect.bottom;
        let top;
        if (spaceAbove >= ddH + 6 || spaceAbove >= spaceBelow) {
            top = Math.max(8, rect.top - ddH - 4);
        } else {
            top = Math.min(vh - ddH - 8, rect.bottom + 4);
        }

        const width = Math.max(220, Math.min(rect.width, 380));

        activeDropdown.style.left  = `${Math.round(left)}px`;
        activeDropdown.style.top   = `${Math.round(top)}px`;
        activeDropdown.style.width = `${Math.round(width)}px`;
    }

    function closeMentionDropdown() {
        if (activeDropdown) activeDropdown.classList.remove("open");
        Object.assign(mentionState, { active: false, activeIdx: -1, query: "", startPos: -1, items: [] });
    }

    async function openMentionDropdown(query) {
        mentionState.active = true;
        mentionState.query  = query;
        if (mentionState.fetchTimer) clearTimeout(mentionState.fetchTimer);
        mentionState.fetchTimer = setTimeout(async () => {
            const items = await fetchCandidates(query);
            mentionState.items    = items;
            mentionState.activeIdx = items.length ? 0 : -1;
            renderMentionDropdown(items, mentionState.activeIdx);
        }, 120);
    }

    function selectMention(sbd) {
        if (!activeTa) return;
        const ta = activeTa;
        const val    = ta.value;
        const before = val.slice(0, mentionState.startPos - 1);
        const after  = val.slice(mentionState.startPos + mentionState.query.length);
        const token  = `@{${sbd}}`;
        const replacement = token + (after.startsWith(" ") ? "" : " ");
        insertTextAt(ta, replacement, before.length, before.length + (val.length - before.length - after.length));
        const cur = before.length + replacement.length;
        ta.setSelectionRange(cur, cur);
        ta.focus();
        closeMentionDropdown();
        hideSuggestTip();
        ta._previewDirty = true;
    }

    let suggestTimer = null, pendingSuggestWord = "";

    function showSuggestTip(word, rect) {
        if (!suggestTip) return;
        pendingSuggestWord = word;
        suggestTip.innerHTML = `Press <kbd>@</kbd> to mention <strong>${escHtml(word.toUpperCase())}</strong>`;
        suggestTip.style.left = rect.left + "px";
        suggestTip.style.top  = (rect.top - 44) + "px";
        suggestTip.classList.add("visible");
    }
    function hideSuggestTip() {
        if (suggestTip) suggestTip.classList.remove("visible");
        pendingSuggestWord = "";
        if (suggestTimer) clearTimeout(suggestTimer);
    }

    function checkSuggestTip(ta) {
        if (!ta) return;
        const upTo = ta.value.slice(0, ta.selectionStart);
        const m = SBD_SUGGEST_RE.exec(upTo);

        if (m && SBD_SHAPE_RE.test(m[1])) {
            if (suggestTimer) clearTimeout(suggestTimer);
            suggestTimer = setTimeout(() => showSuggestTip(m[1], ta.getBoundingClientRect()), 700);
        } else {
            hideSuggestTip();
        }
    }

    function handleTextareaInput(ev) {
        const ta = ev.target;
        activeTa = ta;
        activeDropdown = dropdownFor(ta);
        ta._previewDirty = true;
        const val   = ta.value;
        const caret = ta.selectionStart;
        const upTo  = val.slice(0, caret);
        const atMatch = upTo.match(/@([^\s@]*)$/);
        if (atMatch) {
            mentionState.startPos = caret - atMatch[1].length;
            openMentionDropdown(atMatch[1]);
            hideSuggestTip();
            return;
        }
        if (mentionState.active) closeMentionDropdown();
        checkSuggestTip(ta);
    }

    function handleTextareaKeydown(ev) {
        const ta = ev.target;
        activeTa = ta;
        activeDropdown = dropdownFor(ta);
        if (suggestTip?.classList.contains("visible") && ev.key === "@") {
            const word = pendingSuggestWord;
            if (word) {
                ev.preventDefault();
                const val = ta.value, caret = ta.selectionStart;
                const wordStart = val.slice(0, caret).lastIndexOf(word);
                if (wordStart >= 0) {
                    insertTextAt(ta, "@", wordStart, wordStart);
                    ta.setSelectionRange(wordStart + 1, wordStart + 1);
                    hideSuggestTip();
                }
                return;
            }
            detailContent.innerHTML = await response.text();
            bindEvidenceGuards(detailContent);
            bindEvidencePlaceholders(detailContent);
        } catch (error) {
            detailContent.innerHTML = '<div class="alert alert-danger">Could not load candidate details.</div>';
        }
    }

    function bindMentionOn(ta) {
        if (!ta || ta._mentionBound) return;
        ta._mentionBound = true;
        ta._previewDirty = true;
        ta.addEventListener("input",   handleTextareaInput);
        ta.addEventListener("keydown", handleTextareaKeydown);
        ta.addEventListener("focus",   (ev) => { activeTa = ev.target; activeDropdown = dropdownFor(activeTa); });
        ta.addEventListener("blur",    () => setTimeout(() => { closeMentionDropdown(); hideSuggestTip(); }, 150));
        ta.addEventListener("click",   (ev) => { activeTa = ev.target; activeDropdown = dropdownFor(activeTa); if (!mentionState.active) checkSuggestTip(activeTa); });
    }

    function initMentionSystem() {
        document.querySelectorAll(".mention-textarea-wrap input, .mention-textarea-wrap textarea").forEach(bindMentionOn);
        window.addEventListener("scroll", positionMentionDropdown, { passive: true, capture: true });
        window.addEventListener("resize", positionMentionDropdown);
    }

    const MENTION_TOKEN_RE = /@\{([A-Za-z0-9]{1,9})\}/g;
    const PREVIEW_URL      = "/incidents/preview/";

    function getCsrfToken() {
        const el = document.querySelector("input[name=csrfmiddlewaretoken]");
        if (el) return el.value;
        const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : "";
    }

    function renderPreviewHtmlClient(mdText) {
        if (typeof marked === "undefined") {
            return '<em class="text-muted">Preview not available (marked.js not loaded).</em>';
        }
        const chips = {};
        const processed = mdText.replace(MENTION_TOKEN_RE, (full, sbd) => {
            const key = `CHIPPH${Object.keys(chips).length}ENDCHIP`;
            chips[key] = `<span class="mention-preview-chip">@${escHtml(sbd.toUpperCase())}</span>`;
            return key;
        });
        marked.setOptions({ breaks: true, gfm: true });
        let html = marked.parse(processed);
        Object.entries(chips).forEach(([k, v]) => { html = html.replace(k, v); });
        return (
            '<div class="alert alert-warning small py-2 mb-2">' +
            '<i class="bi bi-exclamation-triangle me-1"></i>' +
            'Preview offline — mention links are not verified against the candidate list.' +
            '</div>' + html
        );
    }

    function showPreviewSkeleton(content) {
        content.innerHTML =
            '<div class="md-preview-skeleton" aria-hidden="true">' +
            '<div class="skel-line"></div>' +
            '<div class="skel-line"></div>' +
            '<div class="skel-line"></div>' +
            '<div class="skel-line"></div>' +
            '</div>';
    }

    async function refreshPreview(editorWrap) {
        const ta      = editorWrap.querySelector("textarea");
        const preview = editorWrap.querySelector(".md-pane-preview");
        const content = preview?.querySelector(".md-preview-content");
        if (!ta || !content) return;

        showPreviewSkeleton(content);

        const sbdInput = document.getElementById("id_sbd");
        const form = new FormData();
        form.append("violation_text", ta.value);
        form.append("sbd", sbdInput ? sbdInput.value : "");
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

            if (typeof bindLightGallery === "function") bindLightGallery(content);
            if (typeof bindEvidenceGuards === "function") bindEvidenceGuards(content);
        } catch (err) {
            console.warn("Preview endpoint failed, falling back to client render:", err);
            content.innerHTML = renderPreviewHtmlClient(ta.value);
        }

        ta._previewDirty = false;
    }

    function initMarkdownTabs() {
        document.querySelectorAll(".md-editor-wrap").forEach((wrap) => {
            const tabBtns     = wrap.querySelectorAll(".md-tab-btn");
            const inputPane   = wrap.querySelector(".md-pane-input");
            const previewPane = wrap.querySelector(".md-pane-preview");
            if (!tabBtns.length || !inputPane || !previewPane) return;

            tabBtns.forEach((btn) => {
                btn.addEventListener("click", () => {
                    tabBtns.forEach((b) => b.classList.remove("active"));
                    btn.classList.add("active");

                    if (btn.dataset.tab === "input") {
                        inputPane.style.display   = "";
                        previewPane.style.display = "none";
                    } else {
                        inputPane.style.display   = "none";
                        previewPane.style.display = "";
                        refreshPreview(wrap);
                    }
                });
            });

            const ta = wrap.querySelector("textarea");
            if (ta) {
                ta.addEventListener("input", () => { ta._previewDirty = true; });
            }

            const reloadBtn = wrap.querySelector(".md-preview-reload");
            if (reloadBtn) {
                reloadBtn.addEventListener("click", (e) => {
                    e.preventDefault();
                    refreshPreview(wrap);
                });
            }
        });
    }

    function insertTextAt(ta, text, replaceStart, replaceEnd) {
        ta.focus();
        ta.setSelectionRange(replaceStart, replaceEnd);
        let ok = false;
        try {
            ok = document.execCommand("insertText", false, text);
        } catch (_) { ok = false; }
        if (!ok) {

            const v = ta.value;
            ta.value = v.slice(0, replaceStart) + text + v.slice(replaceEnd);
            ta.dispatchEvent(new Event("input", { bubbles: true }));
        }
    }

    function insertMarkdown(ta, opts) {
        const { before = "", after = before, placeholder = "text", linePrefix = "", block = false } = opts;
        ta.focus();
        const start = ta.selectionStart, end = ta.selectionEnd, val = ta.value;
        const sel = val.slice(start, end);
        let insert, cursorStart, cursorEnd;

        if (linePrefix) {
            const lines = (sel || placeholder).split("\n").map((l) => linePrefix + l).join("\n");
            const prefix = (block && start > 0 && val[start-1] !== "\n") ? "\n" : "";
            const suffix = (block && end < val.length && val[end] !== "\n") ? "\n" : "";
            insert = prefix + lines + suffix;
            cursorStart = start + prefix.length;
            cursorEnd   = cursorStart + insert.trim().length;
        } else if (sel) {
            insert = before + sel + after;
            cursorStart = start + before.length;
            cursorEnd   = start + before.length + sel.length;
        } else {
            insert = before + placeholder + after;
            cursorStart = start + before.length;
            cursorEnd   = start + before.length + placeholder.length;
        }

        insertTextAt(ta, insert, start, end);
        ta.setSelectionRange(cursorStart, cursorEnd);
    }

    const TOOLBAR_ACTIONS = {
        bold:      (ta) => insertMarkdown(ta, { before: "**", placeholder: "bold text" }),
        italic:    (ta) => insertMarkdown(ta, { before: "*",  placeholder: "italic text" }),
        strike:    (ta) => insertMarkdown(ta, { before: "~~", placeholder: "strikethrough" }),
        code:      (ta) => insertMarkdown(ta, { before: "`",  placeholder: "code" }),
        codeblock: (ta) => {
            const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd);
            insertMarkdown(ta, { before: "```\n", after: "\n```", placeholder: sel || "code block", block: true });
        },
        quote:  (ta) => insertMarkdown(ta, { linePrefix: "> ",  placeholder: "quote", block: true }),
        ul:     (ta) => insertMarkdown(ta, { linePrefix: "- ",  placeholder: "item",  block: true }),
        ol:     (ta) => insertMarkdown(ta, { linePrefix: "1. ", placeholder: "item",  block: true }),
        link: (ta) => {
            const s = ta.selectionStart, e = ta.selectionEnd, sel = ta.value.slice(s, e);
            if (sel) {
                insertTextAt(ta, `[${sel}](url)`, s, e);
                const urlStart = s + sel.length + 3;
                ta.setSelectionRange(urlStart, urlStart + 3);
            } else {
                insertMarkdown(ta, { before: "[", after: "](url)", placeholder: "link text" });
            }
        },
        image: (ta) => {
            const s = ta.selectionStart, e = ta.selectionEnd, sel = ta.value.slice(s, e);
            if (sel) {
                insertTextAt(ta, `![${sel}](url)`, s, e);
                const urlStart = s + sel.length + 4;
                ta.setSelectionRange(urlStart, urlStart + 3);
            } else {
                insertMarkdown(ta, { before: "![", after: "](url)", placeholder: "alt text" });
            }
        },
        upload: (ta) => {

            const input = document.createElement("input");
            input.type = "file";
            input.accept = "image/jpeg,image/png,image/gif,image/webp";
            input.addEventListener("change", () => {
                const f = input.files && input.files[0];
                if (f) uploadImageForTextarea(ta, f);
            });
            input.click();
        },
        mention: (ta) => {
            const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd).trim();
            if (sel && isValidSbd(sel)) {
                const s = ta.selectionStart, e = ta.selectionEnd;
                const token = `@{${sel.toUpperCase()}}`;
                insertTextAt(ta, token, s, e);
                ta.setSelectionRange(s, s + token.length);
            } else {
                insertMarkdown(ta, { before: "@{", after: "}", placeholder: "Số báo danh" });
            }
        },
        undo:   (ta) => { ta.focus(); try { document.execCommand("undo"); } catch(_) {} },
        redo:   (ta) => { ta.focus(); try { document.execCommand("redo"); } catch(_) {} },
    };

    function initMarkdownToolbars() {
        document.querySelectorAll(".md-toolbar").forEach((toolbar) => {
            const targetId = toolbar.dataset.target;
            const ta = targetId ? document.getElementById(targetId) : null;
            if (!ta) return;
            toolbar.querySelectorAll(".md-tb-btn[data-action]").forEach((btn) => {
                btn.addEventListener("click", (e) => {
                    e.preventDefault();
                    const action = btn.dataset.action;
                    if (TOOLBAR_ACTIONS[action]) TOOLBAR_ACTIONS[action](ta);
                });
            });
        });

        document.querySelectorAll(".md-textarea").forEach((ta) => {
            ta.addEventListener("keydown", (e) => {
                if (!e.ctrlKey && !e.metaKey) return;
                const k = e.key.toLowerCase();
                switch (k) {
                    case "b": e.preventDefault(); TOOLBAR_ACTIONS.bold(ta);   break;
                    case "i": e.preventDefault(); TOOLBAR_ACTIONS.italic(ta); break;
                    case "k": e.preventDefault(); TOOLBAR_ACTIONS.link(ta);   break;

                }
            });

            ta.addEventListener("paste", (e) => {
                if (!e.clipboardData || !e.clipboardData.items) return;
                for (const item of e.clipboardData.items) {
                    if (item.kind === "file" && item.type.startsWith("image/")) {
                        const file = item.getAsFile();
                        if (file) {
                            e.preventDefault();  
                            uploadImageForTextarea(ta, file);
                            return;
                        }
                    }
                }
            });

            ta.addEventListener("dragover", (e) => {
                if (e.dataTransfer && Array.from(e.dataTransfer.items || []).some(
                    (it) => it.kind === "file" && it.type.startsWith("image/"))
                ) {
                    e.preventDefault();
                }
            });
            ta.addEventListener("drop", (e) => {
                if (!e.dataTransfer || !e.dataTransfer.files) return;
                const file = Array.from(e.dataTransfer.files).find(
                    (f) => f.type.startsWith("image/")
                );
                if (file) {
                    e.preventDefault();
                    uploadImageForTextarea(ta, file);
                }
            });
        });
    }

    const UPLOAD_URL = "/incidents/upload-image/";

    function baseNameForFile(file) {
        const n = (file && file.name) || "image";

        return n.replace(/\.[^.]+$/, "") || "image";
    }

    function showToast(msg, variant) {

        const container = document.querySelector(".toast-container");
        if (!container || typeof bootstrap === "undefined") {
            console.warn("[upload]", msg);
            return;
        }
        const el = document.createElement("div");
        el.className = `toast app-toast align-items-center text-bg-${variant || "danger"} border-0`;
        el.setAttribute("role", "alert");
        el.innerHTML = `
            <div class="d-flex">
              <div class="toast-body">${escHtml(msg)}</div>
              <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
            </div>`;
        container.appendChild(el);
        new bootstrap.Toast(el, { delay: 5000 }).show();
        el.addEventListener("hidden.bs.toast", () => el.remove());
    }

    async function uploadImageForTextarea(ta, file) {
        const selStart = ta.selectionStart, selEnd = ta.selectionEnd;
        const selected = ta.value.slice(selStart, selEnd);
        const alt = (selected && selected.trim()) || baseNameForFile(file);
        const placeholder = `![Uploading ${alt}…]()`;

        insertTextAt(ta, placeholder, selStart, selEnd);

        const afterIdx = selStart + placeholder.length;
        ta.setSelectionRange(afterIdx, afterIdx);

        const form = new FormData();
        form.append("image", file);

        let replacement;
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
            if (!res.ok) {
                const msg = data.error || `Upload failed (HTTP ${res.status})`;
                showToast(msg);
                replacement = `![Upload failed]()`;
            } else if (!data.url) {
                showToast("Upload succeeded but server did not return a URL.");
                replacement = `![Upload failed]()`;
            } else {
                replacement = `![${alt}](${data.url})`;
            }
        } catch (err) {
            console.warn("Image upload error:", err);
            showToast("Upload failed: " + (err.message || err));
            replacement = `![Upload failed]()`;
        }

        const cur = ta.value;
        const idx = cur.indexOf(placeholder);
        if (idx !== -1) {
            insertTextAt(ta, replacement, idx, idx + placeholder.length);
            const caret = idx + replacement.length;
            ta.setSelectionRange(caret, caret);
        }
        ta._previewDirty = true;
    }

    document.addEventListener("click", (e) => {
        const candidateBtn = e.target.closest(".js-open-candidate-detail");
        if (candidateBtn) { openCandidateDetail(candidateBtn.dataset.sbd); return; }
    });

    document.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const link = e.target.closest(".mention-link.js-open-candidate-detail");
        if (link) { e.preventDefault(); openCandidateDetail(link.dataset.sbd); }
    });

    function initComposeBar() {
        const form = document.getElementById("create-incident-form");
        const viewerBar = document.querySelector(".compose-bar--viewer");
        const root = document.documentElement;

        if (!form) {
            if (viewerBar) {
                const h = Math.round(viewerBar.getBoundingClientRect().height);
                root.style.setProperty("--compose-h", h + "px");
                if (window.ResizeObserver) {
                    const ro = new ResizeObserver(() => {
                        const hh = Math.round(viewerBar.getBoundingClientRect().height);
                        root.style.setProperty("--compose-h", hh + "px");
                    });
                    ro.observe(viewerBar);
                }
            } else {
                root.style.setProperty("--compose-h", "0px");
            }
            return;
        }

        const composeBar   = form;
        const simpleInput  = document.getElementById("id_violation_text_simple");
        const fullTextarea = document.getElementById("id_violation_text_full");
        const isMarkdownF  = document.getElementById("id_is_markdown");
        const expandBtn    = document.getElementById("compose-expand-btn");
        const collapseBtn  = document.getElementById("compose-collapse-btn");
        const expandedWrap = document.getElementById("compose-expanded");
        const videoInput   = document.getElementById("id_evidence");
        const videoLabel   = form.querySelector(".compose-video");
        const videoName    = document.getElementById("video-filename");

        function updateComposeHeight() {
            const h = composeBar.getBoundingClientRect().height;
            document.documentElement.style.setProperty("--compose-h", h + "px");
        }

        function setExpanded(expanded) {
            if (expanded) {
                if (simpleInput && fullTextarea && simpleInput.value && !fullTextarea.value) {
                    fullTextarea.value = simpleInput.value;
                }
                composeBar.dataset.mode = "expanded";
                composeBar.classList.add("is-expanded");
                if (expandedWrap) expandedWrap.hidden = false;
                if (isMarkdownF) isMarkdownF.value = "1";
                if (fullTextarea) {
                    fullTextarea.setAttribute("required", "required");
                    fullTextarea.name = "violation_text";
                }
                if (simpleInput) {
                    simpleInput.removeAttribute("required");
                    simpleInput.name = "violation_text_simple_ignored";
                }
                setTimeout(() => {
                    if (fullTextarea) fullTextarea.focus();
                    updateComposeHeight();
                }, 0);
            } else {
                if (simpleInput && fullTextarea && fullTextarea.value && !simpleInput.value) {
                    simpleInput.value = fullTextarea.value.split("\n")[0];
                }
                composeBar.dataset.mode = "simple";
                composeBar.classList.remove("is-expanded");
                if (expandedWrap) expandedWrap.hidden = true;
                if (isMarkdownF) isMarkdownF.value = "0";
                if (simpleInput) {
                    simpleInput.setAttribute("required", "required");
                    simpleInput.name = "violation_text";
                }
                if (fullTextarea) {
                    fullTextarea.removeAttribute("required");
                    fullTextarea.name = "violation_text_full_ignored";
                }
                setTimeout(() => {
                    if (simpleInput) simpleInput.focus();
                    updateComposeHeight();
                }, 0);
            }
        }

        if (expandBtn) expandBtn.addEventListener("click", () => setExpanded(true));
        if (collapseBtn) collapseBtn.addEventListener("click", () => setExpanded(false));

        if (videoInput && videoName && videoLabel) {
            videoInput.addEventListener("change", () => {
                const f = videoInput.files && videoInput.files[0];
                if (f) {
                    videoName.textContent = f.name;
                    videoLabel.classList.add("has-file");
                } else {
                    videoName.textContent = "";
                    videoLabel.classList.remove("has-file");
                }
            });
        }

        updateComposeHeight();
        window.addEventListener("resize", updateComposeHeight);
        if (window.ResizeObserver) {
            const ro = new ResizeObserver(updateComposeHeight);
            ro.observe(composeBar);
        }

        form.addEventListener("submit", () => {
            if (composeBar.dataset.mode === "expanded" && fullTextarea) {
                fullTextarea.name = "violation_text";
            } else if (simpleInput) {
                simpleInput.name = "violation_text";
            }
        });
    }

    function initLayoutVars() {
        const navbar = document.querySelector("nav.navbar");
        const subheader = document.querySelector(".subheader-bar");
        const root = document.documentElement;

        function measure() {
            if (navbar) {
                const h = Math.round(navbar.getBoundingClientRect().height);
                if (h > 0) root.style.setProperty("--navbar-h", h + "px");
            }
            if (subheader) {
                const h = Math.round(subheader.getBoundingClientRect().height);
                if (h > 0) root.style.setProperty("--subheader-h", h + "px");
            }
        }

        measure();
        window.addEventListener("resize", measure);
        window.addEventListener("load", measure);

        if (window.ResizeObserver) {
            const ro = new ResizeObserver(measure);
            if (navbar) ro.observe(navbar);
            if (subheader) ro.observe(subheader);
        }

        if (document.fonts && document.fonts.ready) {
            document.fonts.ready.then(measure).catch(() => {});
        }
    }

    bindEvidenceGuards(document);
    bindEvidencePlaceholders(document);
    blockClipboardForEvidence();
    syncComposerOffset();

    window.addEventListener("resize", () => {
        syncComposerOffset();
    }, { passive: true });

    if (incidentListContainer) {
        window.addEventListener("scroll", () => {
            if (window.scrollY < 80) {
                loadOlderMessages();
            }
        }, { passive: true });
    }

    if (monitorRoot) {
        if (composerForm) {
            composerForm.addEventListener("submit", handleComposerSubmit);
        }
        if (incidentListContainer) {
            if (document.readyState === "complete") {
                initializeBottomView();
            } else {
                window.addEventListener("load", initializeBottomView, { once: true });
                window.setTimeout(initializeBottomView, 700);
            }
        }
        connectLiveSocket();
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) { connectLiveSocket(); loadNewMessages(false); }
        });
    }
})();
