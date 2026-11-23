// Live updates over Socket.IO. app.py sends a "state" message on connect,
// on every MQTT change/event, and every few seconds.
// Shared by every page: each update below skips elements the page doesn't have.

(function () {
    const socket = io();

    function setText(id, value) {
        const el = document.getElementById(id);
        if (el) el.textContent = value;
    }

    function updateBadge(unread) {
        const badge = document.getElementById("bell-badge");
        if (!badge) return;
        badge.textContent = unread > 9 ? "9+" : unread;
        badge.hidden = unread === 0;
    }

    function updateStatus(id, state) {
        const item = document.getElementById(id);
        if (!item) return;
        item.dataset.state = state;   // CSS colors the dot from data-state
        const text = item.querySelector(".status-text");
        if (text) text.textContent = state.charAt(0).toUpperCase() + state.slice(1);
    }

    function updateLiveFeed(cameraState) {
        const feed = document.getElementById("live-feed");
        if (!feed) return;
        feed.dataset.state = cameraState;
        if (cameraState === "online") {
            feed.removeAttribute("aria-disabled");
            feed.removeAttribute("tabindex");
        } else {
            feed.setAttribute("aria-disabled", "true");
            feed.setAttribute("tabindex", "-1");
        }
    }

    socket.on("state", (s) => {
        // Notifications page: a new event arrived → reload to show it
        // (the reload also marks it read, like opening the page does).
        if (document.body.classList.contains("notifications-page") && s.unread > 0) {
            location.reload();
            return;
        }

        updateBadge(s.unread);

        updateStatus("status-camera", s.camera_state);
        updateStatus("status-doorbell", s.doorbell_state);
        const err = document.getElementById("doorbell-error");
        if (err) {
            err.textContent = `Error (${s.doorbell_error})`;
            err.hidden = s.doorbell_state === "online";
        }

        updateLiveFeed(s.camera_state);

        setText("temp-value", s.temperature ?? "---");
        setText("humidity-value", s.humidity ?? "---");
    });
})();
