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

    function parseId(value) {
        const parsed = Number.parseInt(value, 10);
        return Number.isFinite(parsed) ? parsed : null;
    }

    let oldestId = incidentListContainer ? parseId(incidentListContainer.dataset.oldestId) : null;
    let newestId = incidentListContainer ? parseId(incidentListContainer.dataset.newestId) : null;
    let hasOlder = incidentListContainer ? incidentListContainer.dataset.hasOlder === "1" : false;

    function bindEvidenceGuards(scope) {
        const root = scope || document;
        root.querySelectorAll(".evidence-guard").forEach((el) => {
            el.setAttribute("draggable", "false");
            el.addEventListener("dragstart", (event) => event.preventDefault());
            el.addEventListener("contextmenu", (event) => event.preventDefault());
        });
    }

    function blockClipboardForEvidence() {
        document.addEventListener("copy", (event) => {
            const activeEvidence = document.activeElement && document.activeElement.closest(".evidence-wrap, .candidate-detail-shell");
            if (activeEvidence) {
                event.preventDefault();
            }
        });

        document.addEventListener("keydown", (event) => {
            if ((event.ctrlKey || event.metaKey) && ["s", "u", "p"].includes(event.key.toLowerCase())) {
                const evidenceVisible = document.querySelector(".evidence-wrap img, .evidence-wrap video, .candidate-detail-shell img, .candidate-detail-shell video");
                if (evidenceVisible) {
                    event.preventDefault();
                }
            }
        });
    }

    function updateConnectionStatus(text) {
        if (!liveConnectionStatus) {
            return;
        }
        liveConnectionStatus.textContent = text;
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

        const historyUrl = monitorRoot.dataset.historyUrl;
        if (!historyUrl) {
            return;
        }

        loadingOlder = true;
        updateTopStatus("Loading older messages...");

        try {
            const response = await fetch(`${historyUrl}?before=${encodeURIComponent(oldestId)}`, {
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            if (!response.ok) {
                updateTopStatus("Failed to load older messages.");
                return;
            }

            const payload = await response.json();
            const added = prependIncidents(payload.incidents_html);
            if (payload.oldest_id) {
                oldestId = payload.oldest_id;
            }
            if (newestId === null && payload.newest_id) {
                newestId = payload.newest_id;
            }
            hasOlder = Boolean(payload.has_older);

            if (!hasOlder) {
                updateTopStatus("You reached the first message.");
            } else if (!added) {
                updateTopStatus("");
            } else {
                updateTopStatus("");
            }
        } catch (error) {
            updateTopStatus("Failed to load older messages.");
        } finally {
            loadingOlder = false;
        }
    }

    async function loadNewMessages(forceStickBottom) {
        if (loadingUpdates || !monitorRoot) {
            return;
        }

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
        if (!monitorRoot) {
            return;
        }

        const socketUrl = buildWebsocketUrl();
        if (!socketUrl) {
            return;
        }

        if (liveSocket && (liveSocket.readyState === WebSocket.OPEN || liveSocket.readyState === WebSocket.CONNECTING)) {
            return;
        }

        updateConnectionStatus("Connecting websocket...");

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

        liveSocket.addEventListener("message", (event) => {
            try {
                const payload = JSON.parse(event.data);
                if (payload.type !== "live_event") {
                    return;
                }
                loadNewMessages(false);
            } catch (error) {
                console.debug("Invalid websocket payload:", error);
            }
        });

        liveSocket.addEventListener("error", () => {
            updateConnectionStatus("Realtime reconnecting...");
        });

        liveSocket.addEventListener("close", () => {
            liveSocket = null;
            updateConnectionStatus("Realtime disconnected. Reconnecting...");
            window.setTimeout(() => {
                connectLiveSocket();
            }, reconnectDelayMs);
            reconnectDelayMs = Math.min(reconnectDelayMs * 2, 30000);
        });
    }

    async function openCandidateDetail(sbd) {
        if (!detailContent || !detailCanvas) {
            return;
        }

        detailContent.innerHTML = '<div class="text-center py-4 text-muted">Loading...</div>';
        detailCanvas.show();

        try {
            const response = await fetch(`/stats/candidate/${encodeURIComponent(sbd)}/`, {
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
            });
            if (!response.ok) {
                detailContent.innerHTML = '<div class="alert alert-danger">Could not load candidate details.</div>';
                return;
            }
            detailContent.innerHTML = await response.text();
            bindEvidenceGuards(detailContent);
        } catch (error) {
            detailContent.innerHTML = '<div class="alert alert-danger">Could not load candidate details.</div>';
        }
    }

    function openEvidencePreview(kind, src) {
        if (!evidenceModal || !evidenceBody) {
            return;
        }

        evidenceBody.innerHTML = "";
        if (kind === "video") {
            const video = document.createElement("video");
            video.src = src;
            video.controls = true;
            video.className = "w-100 rounded evidence-guard";
            video.setAttribute("controlsList", "nodownload noplaybackrate");
            video.setAttribute("disablePictureInPicture", "");
            video.setAttribute("oncontextmenu", "return false");
            evidenceBody.appendChild(video);
        } else {
            const image = document.createElement("img");
            image.src = src;
            image.alt = "Evidence";
            image.className = "img-fluid rounded evidence-guard";
            image.setAttribute("draggable", "false");
            evidenceBody.appendChild(image);
        }

        bindEvidenceGuards(evidenceBody);
        evidenceModal.show();
    }

    document.addEventListener("click", (event) => {
        const candidateButton = event.target.closest(".js-open-candidate-detail");
        if (candidateButton) {
            openCandidateDetail(candidateButton.dataset.sbd);
            return;
        }

        const evidencePreview = event.target.closest(".js-evidence-preview");
        if (evidencePreview) {
            openEvidencePreview(evidencePreview.dataset.kind, evidencePreview.dataset.src);
        }
    });

    bindEvidenceGuards(document);
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
            if (!document.hidden) {
                connectLiveSocket();
                loadNewMessages(false);
            }
        });
    }
})();
