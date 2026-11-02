// Shared API client for the local Freight AI Agent backend.
// Every call goes to this same-origin server; the browser never talks to Ollama.
(function (global) {
  "use strict";

  class ApiError extends Error {
    constructor(message, status, type) {
      super(message);
      this.status = status;
      this.type = type || "";
    }
  }

  async function request(path, options) {
    let response;
    try {
      response = await fetch(path, options);
    } catch (exc) {
      throw new ApiError("backend unreachable: " + exc.message, 0, "NetworkError");
    }
    let payload = null;
    const text = await response.text();
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (exc) {
        payload = { error: text.slice(0, 400) };
      }
    }
    if (!response.ok) {
      throw new ApiError((payload && payload.error) || ("request failed with status " + response.status),
                         response.status, payload && payload.type);
    }
    return payload;
  }

  function get(path) {
    return request(path, { headers: { Accept: "application/json" } });
  }

  function post(path, body) {
    return request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}),
    });
  }

  function remove(path) {
    return request(path, { method: "DELETE", headers: { Accept: "application/json" } });
  }

  function upload(files, onDuplicate) {
    const form = new FormData();
    for (const file of files) form.append("files", file);
    form.append("on_duplicate", onDuplicate || "error");
    return request("/api/documents", { method: "POST", body: form });
  }

  // One verb for every console action so the backend stays the authority.
  function action(kind, payload) {
    return post("/api/action", Object.assign({ action: kind }, payload || {}));
  }

  async function multipart(path, fields, file, fieldName) {
    const form = new FormData();
    Object.entries(fields || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null) form.append(key, value);
    });
    if (file) form.append(fieldName || "file", file);
    return request(path, { method: "POST", body: form });
  }

  async function downloadBlob(path, body, filename) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/octet-stream" },
      body: JSON.stringify(body || {}),
    });
    if (!response.ok) {
      let message = "download failed with status " + response.status;
      try {
        message = (await response.json()).error || message;
      } catch (exc) { /* keep the generic message */ }
      throw new ApiError(message, response.status);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename || "download";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  function pollJob(jobId, onUpdate) {
    return new Promise((resolve, reject) => {
      let ticks = 0;
      const timer = setInterval(async () => {
        let job;
        try {
          job = await get("/api/jobs/" + encodeURIComponent(jobId));
        } catch (exc) {
          clearInterval(timer);
          reject(exc);
          return;
        }
        ticks += 1;
        if (onUpdate) onUpdate(job);
        if (job.status === "succeeded") {
          clearInterval(timer);
          resolve(job);
        } else if (job.status === "failed") {
          clearInterval(timer);
          reject(new ApiError(job.error || "the job failed", 500));
        } else if (ticks > 600) {
          clearInterval(timer);
          reject(new ApiError("still running after 10 minutes; check `freight review` jobs", 408));
        }
      }, 1000);
    });
  }

  global.api = { ApiError, get, post, remove, upload, multipart, action, downloadBlob, pollJob };
})(window);
