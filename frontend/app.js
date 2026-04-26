const state = {
  files: [],
  fileStatuses: new Map(),
  selectedPaths: new Set(),
  isUploading: false,
  currentPrefix: "",
  basePrefix: "",
  backendConfigured: false,
  selectedKey: "",
  tree: new Map(),
  expandedPrefixes: new Set([""]),
  loadingPrefixes: new Set(),
};

const runtimeConfig = window.AWS_BROWSER_CONFIG || {};
const apiBaseUrl = (runtimeConfig.apiBaseUrl || "").replace(/\/$/, "");
const apiToken = runtimeConfig.apiToken || localStorage.getItem("AWS_BROWSER_API_TOKEN") || "";

const els = {
  connectionStatus: document.getElementById("connectionStatus"),
  settingsIcon: document.getElementById("settingsIcon"),
  refreshButton: document.getElementById("refreshButton"),
  downloadPrefixButton: document.getElementById("downloadPrefixButton"),
  chooseFolderButton: document.getElementById("chooseFolderButton"),
  folderInput: document.getElementById("folderInput"),
  selectAllFiles: document.getElementById("selectAllFiles"),
  uploadAsJson: document.getElementById("uploadAsJson"),
  selectedCount: document.getElementById("selectedCount"),
  selectedSize: document.getElementById("selectedSize"),
  startUploadButton: document.getElementById("startUploadButton"),
  clearSelectionButton: document.getElementById("clearSelectionButton"),
  progressText: document.getElementById("progressText"),
  progressPercent: document.getElementById("progressPercent"),
  progressBar: document.getElementById("progressBar"),
  localFileList: document.getElementById("localFileList"),
  layout: document.querySelector(".layout"),
  splitResizer: document.getElementById("splitResizer"),
  s3ListShell: document.querySelector(".s3-list"),
  s3FileList: document.getElementById("s3FileList"),
  breadcrumb: document.getElementById("breadcrumb"),
  upButton: document.getElementById("upButton"),
  previewTabButton: document.getElementById("previewTabButton"),
  uploadTabButton: document.getElementById("uploadTabButton"),
  previewSection: document.getElementById("previewSection"),
  uploadSection: document.getElementById("uploadSection"),
  previewTitle: document.getElementById("previewTitle"),
  previewContent: document.getElementById("previewContent"),
  previewDownloadButton: document.getElementById("previewDownloadButton"),
};

els.chooseFolderButton.addEventListener("click", chooseFolder);
els.folderInput.addEventListener("change", handleFolderSelection);
els.startUploadButton.addEventListener("click", startUpload);
els.clearSelectionButton.addEventListener("click", clearSelection);
els.selectAllFiles.addEventListener("change", toggleSelectAllFiles);
els.refreshButton.addEventListener("click", refreshTree);
els.downloadPrefixButton.addEventListener("click", downloadCurrentPrefix);
els.upButton.addEventListener("click", goUp);
els.previewTabButton.addEventListener("click", () => switchWorkspace("preview"));
els.uploadTabButton.addEventListener("click", () => switchWorkspace("upload"));
els.previewDownloadButton.addEventListener("click", () => {
  if (state.selectedKey) downloadObject(state.selectedKey);
});
els.settingsIcon.innerHTML = iconSvg("settings");
els.refreshButton.innerHTML = iconSvg("refresh");
els.downloadPrefixButton.innerHTML = iconSvg("download");
els.upButton.innerHTML = iconSvg("arrowUp");

boot();

async function boot() {
  initSplitResizer();
  await checkHealth();
  await loadS3Prefix("", { showLoading: true });
}

async function checkHealth() {
  try {
    const health = await requestJson("/api/health");
    state.backendConfigured = Boolean(health.configured);
    state.basePrefix = health.base_prefix || "";
    const bucketText = health.configured ? health.bucket : "S3 bucket not configured";
    const prefixText = state.basePrefix ? ` / ${state.basePrefix}` : "";
    els.connectionStatus.textContent = `${bucketText}${prefixText}`;
  } catch (error) {
    state.backendConfigured = false;
    els.connectionStatus.textContent = error.message;
  }
}

function handleFolderSelection(event) {
  applySelectedFiles(Array.from(event.target.files || []));
}

function clearSelection() {
  state.files = [];
  state.fileStatuses.clear();
  state.selectedPaths.clear();
  els.folderInput.value = "";
  setProgress(0, "No upload running");
  renderLocalFiles();
}

async function chooseFolder() {
  if (typeof window.showDirectoryPicker === "function") {
    try {
      const rootHandle = await window.showDirectoryPicker();
      const pickedFiles = await collectFilesFromHandle(rootHandle, "");
      applySelectedFiles(pickedFiles);
      return;
    } catch (error) {
      if (error && error.name === "AbortError") return;
      setProgress(0, `Folder picker failed: ${error.message || "Unknown error"}`);
      return;
    }
  }

  // Fallback for browsers without File System Access API.
  els.folderInput.value = "";
  els.folderInput.click();
}

function applySelectedFiles(files) {
  state.files = files;
  state.fileStatuses.clear();
  state.selectedPaths = new Set();
  for (const file of state.files) {
    const path = relativePathFor(file);
    state.fileStatuses.set(path, "Waiting");
    state.selectedPaths.add(path);
  }
  if (!state.files.length) {
    setProgress(0, "No files found in selected folder");
  }
  renderLocalFiles();
}

async function startUpload() {
  const selectedFiles = selectedLocalFiles();
  if (!selectedFiles.length) {
    setProgress(0, "Select at least one file to upload");
    return;
  }
  if (!state.backendConfigured) {
    setProgress(0, "Configure S3_BUCKET and AWS credentials before uploading");
    return;
  }

  setUploading(true);
  setProgress(0, "Creating manifest");

  try {
    const manifestFiles = selectedFiles.map((file) => ({
      path: relativePathFor(file),
      size: file.size,
      content_type: file.type || "application/octet-stream",
    }));

    const manifest = await requestJson("/api/upload-manifest", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({
        destination_prefix: state.currentPrefix,
        ignore_obsidian: false,
        files: manifestFiles,
      }),
    });

    for (const skipped of manifest.skipped || []) {
      state.fileStatuses.set(skipped.path, `Skipped: ${skipped.reason}`);
    }

    const accepted = new Set((manifest.accepted || []).map((item) => item.path));
    let uploaded = 0;
    const uploadable = selectedFiles.filter((file) => accepted.has(relativePathFor(file)));

    await runLimited(uploadable, 1, async (file) => {
      const path = relativePathFor(file);
      state.fileStatuses.set(path, "Uploading");
      renderLocalFiles();

      try {
        if (els.uploadAsJson.checked) {
          const contentBase64 = await readFileAsBase64(file);
          await requestJson("/api/post-raw-json", {
            method: "POST",
            headers: jsonHeaders(),
            body: JSON.stringify({
              session_id: manifest.session_id,
              path,
              content_type: file.type || "application/octet-stream",
              content_base64: contentBase64,
            }),
          });
        } else {
          const form = new FormData();
          form.append("session_id", manifest.session_id);
          form.append("path", path);
          form.append("file", file, file.name);

          await requestJson("/api/upload-file", {
            method: "POST",
            body: form,
          });
        }
        uploaded += 1;
        state.fileStatuses.set(path, "Uploaded");
        setProgress(
          uploadable.length ? Math.round((uploaded / uploadable.length) * 100) : 100,
          `${uploaded} of ${uploadable.length} uploaded`,
        );
      } catch (error) {
        state.fileStatuses.set(path, `Failed: ${error.message}`);
      }
      renderLocalFiles();
    });

    const finished = await requestJson("/api/finish-upload", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ session_id: manifest.session_id }),
    });

    setProgress(
      finished.missing_count ? 99 : 100,
      `${finished.uploaded_count} uploaded, ${finished.missing_count} missing`,
    );
    await refreshTree();
  } catch (error) {
    setProgress(0, error.message);
  } finally {
    setUploading(false);
  }
}

async function loadS3Prefix(prefix, options = {}) {
  setCurrentPrefix(prefix || "");
  if (options.showLoading) {
    els.s3FileList.innerHTML = s3EmptyHtml("Loading");
  }

  try {
    state.loadingPrefixes.add(state.currentPrefix);
    renderS3Tree();
    const data = await requestJson(`/api/list?prefix=${encodeURIComponent(state.currentPrefix)}`);
    state.tree.set(state.currentPrefix, data.entries || []);
    state.expandedPrefixes.add(state.currentPrefix);
    state.loadingPrefixes.delete(state.currentPrefix);
    renderS3Tree();
  } catch (error) {
    state.loadingPrefixes.delete(state.currentPrefix);
    els.s3FileList.innerHTML = s3EmptyHtml(error.message);
  }
}

async function refreshTree() {
  state.tree.clear();
  state.expandedPrefixes = new Set([""]);
  await loadS3Prefix("", { showLoading: true });
}

async function toggleFolder(prefix) {
  setCurrentPrefix(prefix || "");
  if (state.expandedPrefixes.has(prefix)) {
    state.expandedPrefixes.delete(prefix);
    renderS3Tree();
    return;
  }

  state.expandedPrefixes.add(prefix);
  if (!state.tree.has(prefix)) {
    await loadS3Prefix(prefix);
    return;
  }
  renderS3Tree();
}

function renderS3Tree() {
  const rootEntries = state.tree.get("");
  if (!rootEntries && state.loadingPrefixes.has("")) {
    els.s3FileList.innerHTML = s3EmptyHtml("Loading");
    return;
  }
  if (!rootEntries?.length) {
    els.s3FileList.innerHTML = s3EmptyHtml("No objects in this prefix");
    return;
  }

  const rows = [];
  appendTreeRows(rows, "", 0);
  els.s3FileList.innerHTML = rows.join("");

  els.s3FileList.querySelectorAll("[data-toggle-prefix]").forEach((button) => {
    button.addEventListener("click", () => toggleFolder(button.dataset.togglePrefix));
  });
  els.s3FileList.querySelectorAll("[data-preview]").forEach((button) => {
    button.addEventListener("click", () => previewObject(button.dataset.preview));
  });
  els.s3FileList.querySelectorAll("[data-download]").forEach((button) => {
    button.addEventListener("click", () => downloadObject(button.dataset.download));
  });
  els.s3FileList.querySelectorAll("[data-delete]").forEach((button) => {
    button.addEventListener("click", () => deleteObject(button.dataset.delete));
  });
}

function appendTreeRows(rows, prefix, depth) {
  const entries = state.tree.get(prefix) || [];
  for (const entry of entries) {
      if (entry.type === "folder") {
        const nextPrefix = stripBasePrefix(entry.key).replace(/\/$/, "");
      const expanded = state.expandedPrefixes.has(nextPrefix);
      const loading = state.loadingPrefixes.has(nextPrefix);
      rows.push(`
          <div class="s3-row">
          <div class="s3-cell path-cell tree-name" style="--depth: ${depth}">
            <button class="tree-toggle" type="button" data-toggle-prefix="${escapeAttr(nextPrefix)}" aria-label="${expanded ? "Collapse" : "Expand"} ${escapeAttr(entry.name)}">
              ${expanded ? iconSvg("chevronDown") : iconSvg("chevronRight")}
            </button>
            <button class="folder-link" type="button" data-toggle-prefix="${escapeAttr(nextPrefix)}" title="${escapeAttr(entry.name)}">
              ${iconSvg("folder")}
              <span class="tree-label">${escapeHtml(entry.name)}</span>
            </button>
            </div>
            <div class="s3-cell"></div>
          <div class="s3-cell">${loading ? "Loading" : ""}</div>
            <div class="s3-cell actions"></div>
          </div>
      `);
      if (expanded) {
        if (loading && !state.tree.has(nextPrefix)) {
          rows.push(`
            <div class="s3-row">
              <div class="s3-cell path-cell tree-name muted-row s3-span-row" style="--depth: ${depth + 1}">Loading</div>
            </div>
          `);
        } else {
          appendTreeRows(rows, nextPrefix, depth + 1);
        }
      }
      continue;
    }

      const key = entry.key;
    rows.push(`
        <div class="s3-row ${entry.key === state.selectedKey ? "selected-row" : ""}">
        <div class="s3-cell path-cell tree-name" title="${escapeAttr(key)}" style="--depth: ${depth}">
          <span class="tree-spacer"></span>
          <button class="file-link" type="button" data-preview="${escapeAttr(key)}" title="${escapeAttr(entry.name)}">
            ${iconSvg("file")}
            <span class="tree-label">${escapeHtml(entry.name)}</span>
          </button>
          </div>
          <div class="s3-cell">${formatBytes(entry.size || 0)}</div>
          <div class="s3-cell">${entry.last_modified ? formatDate(entry.last_modified) : ""}</div>
          <div class="s3-cell actions">
          <button class="icon-action" type="button" data-preview="${escapeAttr(key)}" aria-label="Preview ${escapeAttr(entry.name)}" title="Preview">
            ${iconSvg("eye")}
          </button>
          <button class="icon-action" type="button" data-download="${escapeAttr(key)}" aria-label="Download ${escapeAttr(entry.name)}" title="Download">
            ${iconSvg("download")}
          </button>
          <button class="icon-action danger" type="button" data-delete="${escapeAttr(key)}" aria-label="Delete ${escapeAttr(entry.name)}" title="Delete">
            ${iconSvg("trash")}
          </button>
          </div>
        </div>
    `);
  }
}

async function previewObject(key) {
  openPreview(key, "Loading");
  try {
    const data = await requestJson(`/api/read-text?key=${encodeURIComponent(key)}`);
    openPreview(key, data.text);
  } catch (error) {
    openPreview(key, error.message);
  }
}

async function downloadObject(key) {
  const response = await fetch(apiUrl(`/api/download?key=${encodeURIComponent(key)}`), {
    headers: authHeaders(),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    throw new Error(payload?.detail || response.statusText || "Download failed");
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = key.split("/").pop() || "download";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);
}

async function downloadCurrentPrefix() {
  const prefix = state.currentPrefix || "";
  try {
    const response = await fetch(apiUrl(`/api/download-prefix?prefix=${encodeURIComponent(prefix)}`), {
      headers: authHeaders(),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new Error(payload?.detail || response.statusText || "Download failed");
    }

    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;

    const contentDisposition = response.headers.get("content-disposition") || "";
    const matched = contentDisposition.match(/filename="([^"]+)"/i);
    link.download = matched?.[1] || (prefix ? `${prefix.split("/").pop() || "prefix"}.zip` : "bucket-root.zip");

    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(objectUrl);
  } catch (error) {
    setProgress(0, `Prefix download failed: ${error.message}`);
    window.alert(`Prefix download failed: ${error.message}`);
  }
}

async function deleteObject(key) {
  const ok = window.confirm(`Delete ${key}?`);
  if (!ok) return;

  try {
    await requestJson(`/api/object?key=${encodeURIComponent(key)}`, {
      method: "DELETE",
    });
  } catch (error) {
    setProgress(0, `Delete failed: ${error.message}`);
    window.alert(`Delete failed: ${error.message}`);
    return;
  }

  if (state.selectedKey === key) {
    state.selectedKey = "";
    els.previewTitle.textContent = "No file selected";
    els.previewContent.textContent = "Select a text file from the S3 browser.";
    els.previewDownloadButton.disabled = true;
  }

  state.tree.clear();
  state.expandedPrefixes = new Set([""]);
  await loadS3Prefix("", { showLoading: true });
}

function openPreview(title, content) {
  state.selectedKey = title;
  els.previewTitle.textContent = title;
  els.previewContent.textContent = content;
  els.previewDownloadButton.disabled = !state.selectedKey;
  switchWorkspace("preview");
  renderSelectedRow();
}

function switchWorkspace(view) {
  const isPreview = view === "preview";
  els.previewTabButton.classList.toggle("active", isPreview);
  els.uploadTabButton.classList.toggle("active", !isPreview);
  els.previewSection.classList.toggle("active", isPreview);
  els.uploadSection.classList.toggle("active", !isPreview);
}

function initSplitResizer() {
  const saved = Number(localStorage.getItem("AWS_BROWSER_LEFT_PANE_PERCENT"));
  if (Number.isFinite(saved)) {
    setLeftPanePercent(saved);
  }

  els.splitResizer.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    els.splitResizer.setPointerCapture(event.pointerId);
    els.layout.classList.add("resizing");

    const onMove = (moveEvent) => {
      const rect = els.layout.getBoundingClientRect();
      const percent = ((moveEvent.clientX - rect.left) / rect.width) * 100;
      setLeftPanePercent(percent);
    };

    const onUp = () => {
      els.layout.classList.remove("resizing");
      els.splitResizer.removeEventListener("pointermove", onMove);
      els.splitResizer.removeEventListener("pointerup", onUp);
      localStorage.setItem("AWS_BROWSER_LEFT_PANE_PERCENT", currentLeftPanePercent());
    };

    els.splitResizer.addEventListener("pointermove", onMove);
    els.splitResizer.addEventListener("pointerup", onUp);
  });

  els.splitResizer.addEventListener("dblclick", () => {
    setLeftPanePercent(60);
    localStorage.setItem("AWS_BROWSER_LEFT_PANE_PERCENT", "60");
  });
}

function setLeftPanePercent(percent) {
  const clamped = Math.max(35, Math.min(75, percent));
  els.layout.style.setProperty("--left-pane", `${clamped}%`);
}

function currentLeftPanePercent() {
  const value = getComputedStyle(els.layout).getPropertyValue("--left-pane").trim();
  return String(Number.parseFloat(value) || 60);
}

function setCurrentPrefix(prefix) {
  state.currentPrefix = prefix || "";
  els.breadcrumb.textContent = `/${state.currentPrefix}`;
  els.upButton.disabled = !state.currentPrefix;
}

function renderSelectedRow() {
  els.s3FileList.querySelectorAll(".s3-row").forEach((row) => {
    const previewButton = row.querySelector("[data-preview]");
    row.classList.toggle("selected-row", previewButton?.dataset.preview === state.selectedKey);
  });
}

function goUp() {
  const current = state.currentPrefix.replace(/\/$/, "");
  const next = current.split("/").slice(0, -1).join("/");
  loadS3Prefix(next, { showLoading: false });
}

function renderLocalFiles() {
  const selectedFiles = selectedLocalFiles();
  els.selectedCount.textContent = String(selectedFiles.length);
  els.selectedSize.textContent = formatBytes(selectedFiles.reduce((total, file) => total + file.size, 0));
  els.startUploadButton.disabled = !selectedFiles.length;
  els.clearSelectionButton.disabled = !state.files.length;
  els.selectAllFiles.disabled = !state.files.length || state.isUploading;

  if (!state.files.length) {
    els.localFileList.innerHTML = rowHtml(5, "No local folder selected");
    els.selectAllFiles.checked = false;
    els.selectAllFiles.indeterminate = false;
    return;
  }

  els.localFileList.innerHTML = state.files
    .slice(0, 500)
    .map((file) => {
      const path = relativePathFor(file);
      const selected = state.selectedPaths.has(path);
      const status = selected ? state.fileStatuses.get(path) || "Waiting" : "Excluded";
      const modified = file.lastModified ? formatDate(file.lastModified) : "-";
      const modifiedTitle = file.lastModified ? new Date(file.lastModified).toISOString() : "";
      const disabled = state.isUploading ? "disabled" : "";
      return `
        <tr>
          <td class="upload-check-cell">
            <input type="checkbox" data-file-select="${escapeAttr(path)}" ${selected ? "checked" : ""} ${disabled} aria-label="Select ${escapeAttr(path)}" />
          </td>
          <td class="path-cell" title="${escapeAttr(path)}">${escapeHtml(path)}</td>
          <td class="upload-modified-col" title="${escapeAttr(modifiedTitle)}">${escapeHtml(modified)}</td>
          <td class="upload-size-col">${formatBytes(file.size)}</td>
          <td class="upload-status-col ${statusClass(status)}">${escapeHtml(status)}</td>
        </tr>
      `;
    })
    .join("");

  els.localFileList.querySelectorAll("[data-file-select]").forEach((input) => {
    input.addEventListener("change", () => toggleLocalFile(input.dataset.fileSelect, input.checked));
  });

  const selectedCount = state.selectedPaths.size;
  els.selectAllFiles.checked = selectedCount > 0 && selectedCount === state.files.length;
  els.selectAllFiles.indeterminate = selectedCount > 0 && selectedCount < state.files.length;
}

function setUploading(isUploading) {
  state.isUploading = isUploading;
  els.startUploadButton.disabled = isUploading || !selectedLocalFiles().length;
  els.chooseFolderButton.disabled = isUploading;
  els.clearSelectionButton.disabled = isUploading || !state.files.length;
  els.selectAllFiles.disabled = isUploading || !state.files.length;
  els.uploadAsJson.disabled = isUploading;
  if (state.files.length) {
    renderLocalFiles();
  }
}

function setProgress(percent, text) {
  els.progressBar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
  els.progressPercent.textContent = `${Math.max(0, Math.min(100, percent))}%`;
  els.progressText.textContent = text;
}

async function requestJson(url, options) {
  const mergedOptions = {
    ...options,
    headers: {
      ...authHeaders(),
      ...(options?.headers || {}),
    },
  };
  const response = await fetch(apiUrl(url), mergedOptions);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    throw new Error(payload?.detail || response.statusText || "Request failed");
  }
  return payload;
}

function apiUrl(path) {
  return `${apiBaseUrl}${path}`;
}

function authHeaders() {
  return apiToken ? { Authorization: `Bearer ${apiToken}` } : {};
}

function jsonHeaders() {
  return {
    ...authHeaders(),
    "Content-Type": "application/json",
  };
}

async function runLimited(items, limit, worker) {
  const queue = [...items];
  const workers = Array.from({ length: Math.min(limit, queue.length) }, async () => {
    while (queue.length) {
      const item = queue.shift();
      await worker(item);
    }
  });
  await Promise.all(workers);
}

function relativePathFor(file) {
  return file.__relativePath || file.webkitRelativePath || file.name;
}

function stripBasePrefix(key) {
  if (state.basePrefix && key.startsWith(state.basePrefix)) {
    return key.slice(state.basePrefix.length);
  }
  return key;
}

function rowHtml(colspan, message) {
  return `<tr><td colspan="${colspan}" class="empty">${escapeHtml(message)}</td></tr>`;
}

function s3EmptyHtml(message) {
  return `<div class="s3-empty">${escapeHtml(message)}</div>`;
}

function statusClass(status) {
  if (status === "Uploaded") return "status-ok";
  if (status.startsWith("Failed")) return "status-error";
  if (status === "Excluded") return "status-waiting";
  if (status.startsWith("Skipped")) return "status-waiting";
  return "status-waiting";
}

function selectedLocalFiles() {
  return state.files.filter((file) => state.selectedPaths.has(relativePathFor(file)));
}

function toggleSelectAllFiles(event) {
  const checked = event.target.checked;
  state.selectedPaths.clear();
  if (checked) {
    for (const file of state.files) {
      state.selectedPaths.add(relativePathFor(file));
    }
  }
  renderLocalFiles();
}

function toggleLocalFile(path, checked) {
  if (checked) {
    state.selectedPaths.add(path);
  } else {
    state.selectedPaths.delete(path);
  }
  renderLocalFiles();
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || "");
      const comma = result.indexOf(",");
      if (comma < 0 || comma + 1 >= result.length) {
        reject(new Error(`Failed to encode ${file.name} as base64`));
        return;
      }
      resolve(result.slice(comma + 1));
    };
    reader.onerror = () => reject(new Error(`Failed to read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** index;
  return `${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatDate(value) {
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}

function iconSvg(name) {
  const icons = {
    chevronRight: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18l6-6-6-6"/></svg>',
    chevronDown: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>',
    folder:
      '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h6l2 2h10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M3 7v11"/></svg>',
    file:
      '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/></svg>',
    eye:
      '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
    download:
      '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/></svg>',
    trash:
      '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v5"/><path d="M14 11v5"/></svg>',
    refresh:
      '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 0 1-15 6.7"/><path d="M3 12a9 9 0 0 1 15-6.7"/><path d="M18 3v5h-5"/><path d="M6 21v-5h5"/></svg>',
    arrowUp:
      '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5"/><path d="M5 12l7-7 7 7"/></svg>',
    settings:
      '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z"/><path d="M19.4 15a1.8 1.8 0 0 0 .4 2l.1.1-2 3.4-.2-.1a1.8 1.8 0 0 0-2 .4l-.2.2a1.8 1.8 0 0 0-.5 1.3V22h-4v-.3a1.8 1.8 0 0 0-.5-1.3l-.2-.2a1.8 1.8 0 0 0-2-.4l-.2.1-2-3.4.1-.1a1.8 1.8 0 0 0 .4-2 1.8 1.8 0 0 0-1.6-1.1H4v-4h.3A1.8 1.8 0 0 0 6 8a1.8 1.8 0 0 0-.4-2l-.1-.1 2-3.4.2.1a1.8 1.8 0 0 0 2-.4l.2-.2A1.8 1.8 0 0 0 10.4.7V.5h4v.2a1.8 1.8 0 0 0 .5 1.3l.2.2a1.8 1.8 0 0 0 2 .4l.2-.1 2 3.4-.1.1a1.8 1.8 0 0 0-.4 2 1.8 1.8 0 0 0 1.6 1.1h.3v4h-.3a1.8 1.8 0 0 0-1.6 1.1z"/></svg>',
  };
  return icons[name] || "";
}

async function collectFilesFromHandle(directoryHandle, prefix) {
  const files = [];
  for await (const entry of directoryHandle.values()) {
    const nextPrefix = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.kind === "directory") {
      const nested = await collectFilesFromHandle(entry, nextPrefix);
      files.push(...nested);
      continue;
    }
    const file = await entry.getFile();
    Object.defineProperty(file, "__relativePath", {
      value: nextPrefix,
      enumerable: false,
      configurable: true,
    });
    files.push(file);
  }
  files.sort((a, b) => relativePathFor(a).localeCompare(relativePathFor(b)));
  return files;
}
