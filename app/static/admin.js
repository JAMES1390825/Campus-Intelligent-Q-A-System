const TOKEN_KEY = 'admin_token';
let overviewCache = null;
let uploadQueue = [];
let uploadQueueCleanupTimer = null;
let overviewPollTimer = null;
let overviewPollInterval = null;
const DEFAULT_ALLOWED_EXTS = ['.txt', '.md', '.pdf', '.docx', '.xlsx'];
const FAST_POLL_MS = 3500;
const SLOW_POLL_MS = 9000;

const STATUS_LABELS = {
  pending: '排队中',
  busy: '任务占用',
  uploading: '上传中',
  vectorizing: '向量化中',
  completed: '完成',
  failed: '失败',
  unknown: '未知状态',
};

const STATUS_BADGE_CLASS = {
  pending: 'badge-pending',
  busy: 'badge-busy',
  uploading: 'badge-uploading',
  vectorizing: 'badge-vectorizing',
  completed: 'badge-success',
  failed: 'badge-failed',
  unknown: 'badge-muted',
};

const MAX_STATUS_ITEMS = 12;
const APPROX_CHUNK_BYTES = 1800;
const STATUS_PROGRESS = {
  pending: 10,
  uploading: 40,
  vectorizing: 85,
  completed: 100,
  failed: 100,
  busy: 30,
};
const ACTIVE_UPLOAD_STATES = new Set(['pending', 'uploading', 'vectorizing', 'busy']);
const FILE_TYPE_LABELS = {
  txt: 'TXT 文本',
  md: 'Markdown',
  pdf: 'PDF',
  docx: 'Word',
  doc: 'Word',
  xlsx: 'Excel',
  csv: 'CSV',
};

function normalizeExt(ext) {
  if (!ext) return '';
  const value = String(ext).trim().toLowerCase();
  if (!value) return '';
  return value.startsWith('.') ? value : `.${value}`;
}

function normalizeUploadLimits(raw = null) {
  const allowedExts = Array.isArray(raw?.allowed_exts)
    ? raw.allowed_exts.map((ext) => normalizeExt(ext)).filter(Boolean)
    : [];
  const maxMb = typeof raw?.max_mb === 'number' && raw.max_mb > 0 ? raw.max_mb : null;
  return {
    maxMb,
    maxBytes: maxMb ? maxMb * 1024 * 1024 : null,
    allowedExts: allowedExts.length ? allowedExts : DEFAULT_ALLOWED_EXTS,
  };
}

function renderUploadLimits(rawLimits = null) {
  const normalized = rawLimits && rawLimits.__normalized ? rawLimits : normalizeUploadLimits(rawLimits);
  if (overviewCache) {
    overviewCache.upload_limits = Object.assign({ __normalized: true }, normalized);
  }
  const guardrailEl = document.getElementById('uploadGuardrails');
  const hintsEl = document.getElementById('uploadHints');
  if (guardrailEl) {
    const limitText = normalized.maxMb ? `单个文件 ≤ ${normalized.maxMb} MB` : '未设置单文件大小限制';
    guardrailEl.textContent = `限制：${limitText} · 支持：${normalized.allowedExts.join(' / ')}`;
  }
  if (hintsEl) {
    hintsEl.textContent = '系统会在上传前计算内容指纹并自动跳过重复文件。';
  }
}

async function computeFileHash(file) {
  if (!file || !window.crypto || !window.crypto.subtle) return null;
  const buffer = await file.arrayBuffer();
  const digest = await window.crypto.subtle.digest('SHA-256', buffer);
  const hashArray = Array.from(new Uint8Array(digest));
  return hashArray.map((b) => b.toString(16).padStart(2, '0')).join('');
}

function getDocsHashMap() {
  const docs = Array.isArray(overviewCache?.docs) ? overviewCache.docs : [];
  const map = new Map();
  docs.forEach((doc) => {
    if (doc && typeof doc.hash === 'string' && doc.hash) {
      map.set(doc.hash, doc);
    }
  });
  return map;
}

function getUploadLimits() {
  if (overviewCache && overviewCache.upload_limits && overviewCache.upload_limits.__normalized) {
    return overviewCache.upload_limits;
  }
  const normalized = normalizeUploadLimits(overviewCache?.upload_limits);
  if (overviewCache) {
    overviewCache.upload_limits = Object.assign({ __normalized: true }, normalized);
  }
  return normalized;
}

function removeQueuedDuplicatesAgainstExisting() {
  if (!uploadQueue.length) return;
  const statusEl = document.getElementById('uploadStatus');
  const hashMap = getDocsHashMap();
  if (!hashMap.size) return;
  const removed = [];
  uploadQueue = uploadQueue.filter((item) => {
    const state = normalizeStatus(item.status);
    const allowRemoval = state === 'pending' || state === 'unknown' || !state;
    if (allowRemoval && item.hash && hashMap.has(item.hash)) {
      removed.push({ file: item.file.name, dup: hashMap.get(item.hash)?.name || '已存在文件' });
      return false;
    }
    return true;
  });
  if (removed.length) {
    renderUploadQueue();
    const summary = removed
      .slice(0, 3)
      .map((entry) => `${entry.file} ↔ ${entry.dup}`)
      .join('、');
    const suffix = removed.length > 3 ? ` 等 ${removed.length} 个` : '';
    setStatus(statusEl, `已移除重复文件：${summary}${suffix}`);
  }
}

function ensureAdminToken() {
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) {
    window.location.href = '/admin';
    return null;
  }
  return token;
}

function authHeaders(token, extra = {}) {
  return Object.assign({ Authorization: 'Bearer ' + token }, extra);
}

function updateText(id, text) {
  const el = typeof id === 'string' ? document.getElementById(id) : id;
  if (el) {
    el.textContent = text;
  }
}

function formatNumber(value, digits = 0, fallback = '-') {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return fallback;
  }
  if (typeof value === 'number' && digits > 0) {
    return value.toFixed(digits);
  }
  return String(value);
}

function normalizeStatus(status) {
  if (!status) return 'unknown';
  return String(status).toLowerCase();
}

function buildStatusBadge(status) {
  const normalized = normalizeStatus(status);
  const label = STATUS_LABELS[normalized] || STATUS_LABELS.unknown;
  const className = STATUS_BADGE_CLASS[normalized] || STATUS_BADGE_CLASS.unknown;
  return `<span class="status-badge ${className}">${label}</span>`;
}

function escapeHtml(text) {
  if (text === null || text === undefined) return '';
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function setStatus(target, message) {
  if (!target) return;
  const el = typeof target === 'string' ? document.getElementById(target) : target;
  if (!el) return;
  el.textContent = message || '';
}

function updateSelectedFilesList(latestFile = null) {
  const el = document.getElementById('selectedFiles');
  if (!el) return;
  if (!uploadQueue.length) {
    el.textContent = '尚未选择文件';
    return;
  }
  const names = uploadQueue.slice(0, 3).map((item) => item.file.name).join('、');
  const suffix = uploadQueue.length > 3 ? ` 等 ${uploadQueue.length} 个文件` : '';
  const latest = latestFile ? `，最新加入 ${latestFile.name}` : '';
  el.textContent = `待上传 ${uploadQueue.length} 个文件：${names}${suffix}${latest}`;
}

function resetFileSelection() {
  uploadQueue = [];
  renderUploadQueue();
  const fileInput = document.getElementById('fileInput');
  if (fileInput) {
    fileInput.value = '';
  }
  updateSelectedFilesList(null);
  const statusEl = document.getElementById('uploadStatus');
  setStatus(statusEl, '已清空当前选择');
}

function stopOverviewPolling() {
  if (overviewPollTimer) {
    clearInterval(overviewPollTimer);
    overviewPollTimer = null;
    overviewPollInterval = null;
  }
}

function startOverviewPolling(intervalMs) {
  if (overviewPollTimer && overviewPollInterval === intervalMs) return;
  if (overviewPollTimer) {
    clearInterval(overviewPollTimer);
  }
  overviewPollInterval = intervalMs;
  overviewPollTimer = setInterval(() => {
    const token = localStorage.getItem(TOKEN_KEY);
    if (!token) {
      stopOverviewPolling();
      return;
    }
    loadOverview();
  }, intervalMs);
}

function updateOverviewPolling(isBusy) {
  const targetInterval = isBusy ? FAST_POLL_MS : SLOW_POLL_MS;
  startOverviewPolling(targetInterval);
}

function refreshUploadStatusMessage() {
  const statusEl = document.getElementById('uploadStatus');
  if (!uploadQueue.length) {
    if (statusEl) {
      setStatus(statusEl, '');
    }
    updateOverviewPolling(false);
    return;
  }
  if (!statusEl) {
    updateOverviewPolling(false);
    return;
  }
  const hasActive = uploadQueue.some((item) => ACTIVE_UPLOAD_STATES.has(normalizeStatus(item.status)));
  if (!hasActive) {
    setStatus(statusEl, '全部文件向量化完成');
  }
  updateOverviewPolling(hasActive);
}

function setupAdminMenu() {
  const trigger = document.getElementById('adminUserMenuButton');
  const dropdown = document.getElementById('adminUserMenuDropdown');
  if (!trigger || !dropdown) return;
  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    dropdown.classList.toggle('open');
  });
  document.addEventListener('click', (e) => {
    if (!dropdown.contains(e.target) && e.target !== trigger && !trigger.contains(e.target)) {
      dropdown.classList.remove('open');
    }
  });
  const changeBtn = document.getElementById('adminMenuChangePwd');
  const logoutBtn = document.getElementById('adminMenuLogout');
  if (changeBtn) {
    changeBtn.onclick = () => {
      dropdown.classList.remove('open');
      navigateChangePassword();
    };
  }
  if (logoutBtn) {
    logoutBtn.onclick = () => {
      dropdown.classList.remove('open');
      adminLogout();
    };
  }
}

function navigateChangePassword() {
  openPwdModal();
}

function adminLogout() {
  localStorage.removeItem(TOKEN_KEY);
  window.location.href = '/admin';
}

function setupTabs() {
  const buttons = Array.from(document.querySelectorAll('.tab-button'));
  const panels = Array.from(document.querySelectorAll('.tab-panel'));
  if (!buttons.length || !panels.length) return;
  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const targetId = button.dataset.tabTarget;
      if (!targetId) return;
      buttons.forEach((btn) => {
        const isActive = btn === button;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
      });
      panels.forEach((panel) => {
        const isTarget = panel.id === targetId;
        panel.classList.toggle('active', isTarget);
        if (isTarget) {
          panel.removeAttribute('aria-hidden');
        } else {
          panel.setAttribute('aria-hidden', 'true');
        }
      });
    });
  });
}

function guessFileType(filename) {
  if (!filename) return '未知类型';
  const parts = filename.split('.');
  const ext = parts.length > 1 ? parts.pop().toLowerCase() : '';
  if (!ext) return '未知类型';
  return FILE_TYPE_LABELS[ext] || `${ext.toUpperCase()} 文件`;
}


function estimateChunkCount(file) {
  const size = file?.size || 0;
  if (!size) return 1;
  return Math.max(1, Math.ceil(size / APPROX_CHUNK_BYTES));
}

function updateUploadQueueSummary() {
  const summaryEl = document.getElementById('uploadQueueSummary');
  if (!summaryEl) return;
  if (!uploadQueue.length) {
    summaryEl.textContent = '';
    return;
  }
  const totalSize = uploadQueue.reduce((sum, item) => sum + (item.file.size || 0), 0);
  const totalChunks = uploadQueue.reduce((sum, item) => sum + estimateChunkCount(item.file), 0);
  summaryEl.textContent = `共 ${uploadQueue.length} 个文件 · ${formatSize(totalSize)} · 预估 ${totalChunks} 个切片`;
}

function moveUploadQueueItem(index, direction) {
  const targetIndex = index + direction;
  if (targetIndex < 0 || targetIndex >= uploadQueue.length) return;
  const temp = uploadQueue[index];
  uploadQueue[index] = uploadQueue[targetIndex];
  uploadQueue[targetIndex] = temp;
  renderUploadQueue();
}

function renderUploadQueue() {
  const list = document.getElementById('uploadQueueList');
  if (!list) return;
  if (!uploadQueue.length) {
    list.classList.add('muted');
    list.innerHTML = '暂无文件';
    updateUploadQueueSummary();
    refreshUploadStatusMessage();
    return;
  }
  list.classList.remove('muted');
  list.innerHTML = '';
  uploadQueue.forEach((item, index) => {
    const row = document.createElement('div');
    row.className = 'queue-item queue-item-rich';

    const info = document.createElement('div');
    info.className = 'queue-item-info';

    const title = document.createElement('div');
    title.className = 'queue-item-title';
    title.innerHTML = `${escapeHtml(item.file.name)} <span class="status-badge badge-muted">${guessFileType(item.file.name)}</span>`;

    const meta = document.createElement('div');
    meta.className = 'queue-item-meta';
    const modified = new Date(item.file.lastModified).toLocaleString();
    meta.textContent = `大小 ${formatSize(item.file.size)} · 预估 ${estimateChunkCount(item.file)} 个切片 · 修改于 ${modified}`;

    const currentStatus = normalizeStatus(item.status) || 'pending';
    const progressBar = document.createElement('div');
    progressBar.className = 'queue-progress';
    const progressInner = document.createElement('div');
    progressInner.className = 'queue-progress-inner';
    const progressValue = Math.min(100, Math.round(item.progress ?? STATUS_PROGRESS[currentStatus] ?? 0));
    progressInner.style.width = `${progressValue}%`;
    progressInner.textContent = `${progressValue}%`;
    progressBar.appendChild(progressInner);

    info.appendChild(title);
    info.appendChild(meta);
    info.appendChild(progressBar);

    if (item.hash) {
      const hashMeta = document.createElement('div');
      hashMeta.className = 'queue-item-note muted';
      hashMeta.style.fontFamily = 'monospace';
      hashMeta.textContent = `指纹 ${item.hash.slice(0, 16)}${item.hash.length > 16 ? '…' : ''}`;
      info.appendChild(hashMeta);
    }

    if (item.note) {
      const note = document.createElement('div');
      note.className = 'queue-item-note';
      if (currentStatus === 'failed') {
        note.classList.add('queue-item-note-error');
      }
      note.textContent = item.note;
      info.appendChild(note);
    }

    const actions = document.createElement('div');
    actions.className = 'queue-item-actions';

    const upBtn = document.createElement('button');
    upBtn.className = 'ghost';
    upBtn.textContent = '上移';
    upBtn.disabled = index === 0;
    upBtn.onclick = () => moveUploadQueueItem(index, -1);

    const downBtn = document.createElement('button');
    downBtn.className = 'ghost';
    downBtn.textContent = '下移';
    downBtn.disabled = index === uploadQueue.length - 1;
    downBtn.onclick = () => moveUploadQueueItem(index, 1);

    const removeBtn = document.createElement('button');
    removeBtn.className = 'ghost danger';
    removeBtn.textContent = '移除';
    removeBtn.onclick = () => {
      uploadQueue.splice(index, 1);
      renderUploadQueue();
    };

    actions.appendChild(upBtn);
    actions.appendChild(downBtn);
    actions.appendChild(removeBtn);

    row.appendChild(info);
    row.appendChild(actions);
    list.appendChild(row);
  });
  updateUploadQueueSummary();
  refreshUploadStatusMessage();
}

function syncQueueStatusesFromBackend(statusList = [], docs = []) {
  if (!uploadQueue.length) return;
  const statusMap = new Map();
  if (Array.isArray(statusList)) {
    statusList.forEach((item) => {
      const key = item.filename || item.doc;
      if (key) {
        statusMap.set(key, item);
      }
    });
  }
  if (Array.isArray(docs)) {
    docs.forEach((doc) => {
      if (doc?.name) {
        statusMap.set(doc.name, { status: doc.status, note: doc.status_meta?.note });
      }
    });
  }
  let changed = false;
  let completedUpdated = false;
  uploadQueue.forEach((entry) => {
    const info = statusMap.get(entry.file.name);
    if (!info) return;
    const normalized = normalizeStatus(info.status);
    if (normalized && normalized !== entry.status) {
      entry.status = normalized;
      entry.progress = STATUS_PROGRESS[normalized] ?? entry.progress;
      if (normalized === 'completed') {
        entry.note = '向量化完成';
        completedUpdated = true;
      }
      changed = true;
    }
    if (info.note && info.note !== entry.note) {
      entry.note = info.note;
      changed = true;
    }
    if (info.error && info.error !== entry.note) {
      entry.note = info.error;
      changed = true;
    }
  });
  if (changed) {
    renderUploadQueue();
  }
  if (completedUpdated) {
    scheduleQueueCleanup(2000);
  }
  refreshUploadStatusMessage();
}

function scheduleQueueCleanup(delay = 5000) {
  if (uploadQueueCleanupTimer) {
    clearTimeout(uploadQueueCleanupTimer);
  }
  uploadQueueCleanupTimer = setTimeout(() => {
    const before = uploadQueue.length;
    uploadQueue = uploadQueue.filter((item) => item.status !== 'completed');
    if (uploadQueue.length !== before) {
      renderUploadQueue();
    }
    uploadQueueCleanupTimer = null;
  }, delay);
}

let hashUnsupportedNotified = false;

async function enqueueFile(file) {
  const statusEl = document.getElementById('uploadStatus');
  if (!file) return;
  const limits = getUploadLimits();
  const ext = normalizeExt(file.name.split('.').pop());
  if (limits.allowedExts.length && (!ext || !limits.allowedExts.includes(ext))) {
    setStatus(statusEl, `文件类型 ${ext || '未知'} 不在允许范围 ${limits.allowedExts.join(', ')}`);
    return;
  }
  if (limits.maxBytes && file.size > limits.maxBytes) {
    setStatus(statusEl, `文件 ${file.name} 超过大小限制 ${limits.maxMb} MB`);
    return;
  }

  const duplicateCandidate = uploadQueue.some(
    (item) =>
      item.file.name === file.name &&
      item.file.size === file.size &&
      item.file.lastModified === file.lastModified,
  );
  if (duplicateCandidate) {
    setStatus(statusEl, '该文件已在待上传列表中');
    return;
  }

  let fileHash = null;
  try {
    fileHash = await computeFileHash(file);
  } catch (error) {
    console.warn('computeFileHash failed', error);
  }

  if (!fileHash && !hashUnsupportedNotified) {
    setStatus(statusEl, '当前浏览器不支持内容指纹计算，将按常规方式加入');
    hashUnsupportedNotified = true;
  }

  if (fileHash) {
    const queueDup = uploadQueue.some((item) => item.hash && item.hash === fileHash);
    if (queueDup) {
      setStatus(statusEl, '该文件内容与待上传列表中的其他文件相同');
      return;
    }
    const existingDoc = getDocsHashMap().get(fileHash);
    if (existingDoc) {
      setStatus(statusEl, `内容与现有文档 ${existingDoc.name || '已存在文件'} 重复，未加入`);
      return;
    }
  }

  uploadQueue.push({ file, status: 'pending', progress: STATUS_PROGRESS.pending, note: '', hash: fileHash });
  renderUploadQueue();
  const hashHint = fileHash ? `（指纹 ${fileHash.slice(0, 8)}…）` : '';
  setStatus(statusEl, `已加入：${file.name} ${hashHint}`.trim());
  updateSelectedFilesList(file);
}

function renderStats(data = {}) {
  const metrics = data.metrics || {};
  const vector = data.vectorstore || {};
  const redisInfo = data.redis || {};
  const docs = Array.isArray(data.docs) ? data.docs : [];

  updateText('statChunks', formatNumber(vector.vectors_indexed, 0, '-'));
  updateText('statDocsCount', `文档数 ${formatNumber(data.docs_count ?? docs.length, 0, '-')}`);
  updateText('statUploads', formatNumber(metrics.total_doc_uploads, 0, '0'));
  updateText('statQueries', formatNumber(metrics.total_queries, 0, '0'));
  updateText('statLatency', metrics.avg_latency_ms ? `avg latency ${formatNumber(metrics.avg_latency_ms, 1)} ms` : 'avg latency -');
  updateText('statQueue', formatNumber(redisInfo.queue_length, 0, '-'));
  updateText('statRedis', redisInfo.enabled ? '协调已启用' : '协调未启用');
}

function renderDocsTable(docs = []) {
  const body = document.getElementById('docsTableBody');
  if (!body) return;
  if (!Array.isArray(docs) || docs.length === 0) {
    body.innerHTML = '<tr><td colspan="6" class="sources">暂无文档</td></tr>';
    return;
  }

  body.innerHTML = '';
  docs.forEach((doc) => {
    const tr = document.createElement('tr');

    const nameTd = document.createElement('td');
    const link = document.createElement('button');
    link.className = 'link-button';
    link.textContent = doc.name;
    link.onclick = () => downloadDocAdmin(doc.name);
    nameTd.appendChild(link);

    const sizeTd = document.createElement('td');
    sizeTd.textContent = formatSize(doc.size);

    const updatedTd = document.createElement('td');
    updatedTd.textContent = formatTimestamp(doc.mtime || doc.updated_at);

    const statusTd = document.createElement('td');
    const currentStatus = normalizeStatus(doc.status);
    statusTd.innerHTML = buildStatusBadge(currentStatus);

    const hashTd = document.createElement('td');
    if (doc.hash) {
      const shortHash = String(doc.hash).slice(0, 12);
      hashTd.textContent = shortHash + (doc.hash.length > 12 ? '…' : '');
      hashTd.title = doc.hash;
      hashTd.style.fontFamily = 'monospace';
    } else {
      hashTd.textContent = '-';
    }

    const actionsTd = document.createElement('td');
    const downloadBtn = document.createElement('button');
    downloadBtn.className = 'ghost';
    downloadBtn.textContent = '下载';
    downloadBtn.onclick = () => downloadDocAdmin(doc.name);

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'ghost danger';
    deleteBtn.textContent = '删除';
    deleteBtn.onclick = () => deleteDoc(doc.name);

    actionsTd.appendChild(downloadBtn);
    actionsTd.appendChild(deleteBtn);

    tr.appendChild(nameTd);
    tr.appendChild(sizeTd);
    tr.appendChild(updatedTd);
    tr.appendChild(statusTd);
    tr.appendChild(hashTd);
    tr.appendChild(actionsTd);

    body.appendChild(tr);
  });
}

function renderStatusList(items = []) {
  const container = document.getElementById('statusList');
  if (!container) return;
  if (!Array.isArray(items) || items.length === 0) {
    container.innerHTML = '<div class="sources">暂无状态</div>';
    return;
  }
  container.innerHTML = '';
  items.slice(0, MAX_STATUS_ITEMS).forEach((item) => {
    const card = document.createElement('div');
    card.className = 'status-card';
    const statusLine = document.createElement('div');
    statusLine.className = 'status-card-title';
    statusLine.innerHTML = `${escapeHtml(item.doc || item.filename || '未知文档')} ${buildStatusBadge(item.status)}`;
    const metaLine = document.createElement('div');
    metaLine.className = 'status-card-meta';
    const tsValue = item.ts ? Number(item.ts) : null;
    const ts = tsValue ? new Date(tsValue * 1000).toLocaleTimeString() : '';
    const details = [];
    if (item.job_id) {
      const shortId = String(item.job_id).slice(0, 8);
      details.push(`任务 ${shortId}`);
    }
    if (item.admin) {
      details.push(`操作 ${escapeHtml(item.admin)}`);
    }
    if (item.note) {
      details.push(escapeHtml(item.note));
    }
    if (item.error) {
      details.push(escapeHtml(item.error));
    }
    metaLine.innerHTML = `${ts}${details.length ? ' · ' + details.join(' · ') : ''}`;
    card.appendChild(statusLine);
    card.appendChild(metaLine);
    container.appendChild(card);
  });
}

function renderQueuePreview(redisInfo = {}) {
  const list = document.getElementById('queueList');
  if (!list) return;
  const preview = Array.isArray(redisInfo.queue_preview) ? redisInfo.queue_preview : [];
  if (!preview.length) {
    list.innerHTML = '<div class="sources">队列为空</div>';
    return;
  }
  list.innerHTML = '';
  preview.forEach((item) => {
    const row = document.createElement('div');
    row.className = 'queue-item';
    const title = escapeHtml(item.filename || item.doc || '未知任务');
    const size = item.size ? ` · ${formatSize(item.size)}` : '';
    const tsValue = item.ts ? Number(item.ts) : null;
    const time = tsValue ? ` · ${new Date(tsValue * 1000).toLocaleTimeString()}` : '';
    row.innerHTML = `<span class="queue-item-title">${title}</span><span class="queue-item-meta">${size}${time}</span>`;
    list.appendChild(row);
  });
}

function renderTesterResult(resp) {
  const container = document.getElementById('testerResult');
  const answerEl = document.getElementById('testerAnswer');
  const sourcesEl = document.getElementById('testerSources');
  if (!container || !answerEl || !sourcesEl) return;
  answerEl.innerHTML = resp.answer ? escapeHtml(resp.answer).replace(/\n/g, '<br/>') : '无回答';
  const sources = Array.isArray(resp.sources) ? resp.sources : [];
  if (!sources.length) {
    sourcesEl.innerHTML = '<div class="sources">无引用</div>';
  } else {
    sourcesEl.innerHTML = sources
      .map((s) => `<div class="source-item"><strong>${escapeHtml(s.source || '未知来源')}</strong><p>${escapeHtml(s.snippet || '').slice(0, 160)}</p></div>`)
      .join('');
  }
  container.classList.remove('hidden');
}

async function loadOverview() {
  const token = ensureAdminToken();
  if (!token) return;
  const statusEl = document.getElementById('overviewStatus');
  setStatus(statusEl, '加载中...');
  try {
    const resp = await fetch('/api/admin/overview', { headers: authHeaders(token) });
    if (resp.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      window.location.href = '/admin';
      return;
    }
    if (!resp.ok) {
      const text = await resp.text();
      setStatus(statusEl, '加载失败: ' + text);
      return;
    }
    const data = await resp.json();
    overviewCache = data;
    renderUploadLimits(data.upload_limits);
    renderDocsTable(data.docs || []);
    renderStats(data);
    renderStatusList(data.recent_statuses || []);
    renderQueuePreview(data.redis || {});
    syncQueueStatusesFromBackend(data.recent_statuses || [], data.docs || []);
    removeQueuedDuplicatesAgainstExisting();
    const docsStatus = document.getElementById('docsStatus');
    if (docsStatus) {
      const count = data.docs_count ?? (Array.isArray(data.docs) ? data.docs.length : 0);
      docsStatus.textContent = `共 ${count} 个文件`;
    }
    const hasActiveDocs = (data.docs || []).some((doc) => ACTIVE_UPLOAD_STATES.has(normalizeStatus(doc.status)));
    const hasActiveStatuses = (data.recent_statuses || []).some((item) => ACTIVE_UPLOAD_STATES.has(normalizeStatus(item.status)));
    const hasActiveQueue = uploadQueue.some((item) => ACTIVE_UPLOAD_STATES.has(normalizeStatus(item.status)));
    updateOverviewPolling(hasActiveDocs || hasActiveStatuses || hasActiveQueue);
    setStatus(statusEl, `更新于 ${new Date().toLocaleTimeString()}`);
  } catch (error) {
    setStatus(statusEl, '加载失败: ' + error);
  }
}

async function login() {
  const username = document.getElementById('adminUsername').value.trim();
  const pwd = document.getElementById('adminPassword').value;
  const statusEl = document.getElementById('loginStatus');
  if (!username || !pwd) { alert('请输入账号和密码'); return; }
  try {
    const params = new URLSearchParams();
    params.append('username', username);
    params.append('password', pwd);
    const resp = await fetch('/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: params
    });
    if (!resp.ok) {
      const err = await resp.json();
      setStatus(statusEl, '登录失败: ' + (err.detail || resp.statusText));
      return;
    }
    const data = await resp.json();
    localStorage.setItem(TOKEN_KEY, data.token);
    setStatus(statusEl, '登录成功');
  } catch (e) {
    setStatus(statusEl, '异常: ' + e);
  }
}

function openPwdModal() {
  const modal = document.getElementById('pwdModal');
  if (modal) modal.classList.remove('hidden');
}

function closePwdModal() {
  const modal = document.getElementById('pwdModal');
  const statusEl = document.getElementById('pwdStatus');
  const newInput = document.getElementById('newPassword');
  const confirmInput = document.getElementById('confirmPassword');
  if (statusEl) statusEl.textContent = '';
  if (newInput) newInput.value = '';
  if (confirmInput) confirmInput.value = '';
  if (modal) modal.classList.add('hidden');
}

async function changePassword() {
  const token = localStorage.getItem(TOKEN_KEY);
  const newPwd = document.getElementById('newPassword').value;
  const confirmPwd = document.getElementById('confirmPassword').value;
  const statusEl = document.getElementById('pwdStatus');
  if (!token) { setStatus(statusEl, '请先登录'); return; }
  if (!newPwd || newPwd.length < 6) { setStatus(statusEl, '新密码至少6位'); return; }
  if (newPwd !== confirmPwd) { setStatus(statusEl, '两次输入不一致'); return; }
  try {
    const params = new URLSearchParams();
    params.append('new_password', newPwd);
    const resp = await fetch('/api/auth/change_password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'Authorization': 'Bearer ' + token },
      body: params
    });
    if (!resp.ok) {
      let errText = resp.statusText;
      try { const err = await resp.json(); errText = err.detail || errText; } catch (_) {}
      setStatus(statusEl, '修改失败: ' + errText);
      return;
    }
    setStatus(statusEl, '修改成功');
    closePwdModal();
  } catch (e) {
    setStatus(statusEl, '异常: ' + e);
  }
}

async function uploadDoc() {
  const token = ensureAdminToken();
  const statusEl = document.getElementById('uploadStatus');
  const uploadBtn = document.getElementById('uploadSubmitButton');
  if (!token) return;
  const pendingItems = uploadQueue.filter((item) => !item.status || item.status === 'pending' || item.status === 'failed');
  if (!pendingItems.length) {
    setStatus(statusEl, '请选择需要上传的文件');
    return;
  }
  const form = new FormData();
  pendingItems.forEach((item) => {
    form.append('files', item.file);
    item.status = 'uploading';
    item.progress = STATUS_PROGRESS.uploading;
    item.note = '';
  });
  renderUploadQueue();
  updateOverviewPolling(true);
  setStatus(statusEl, `上传中，共 ${pendingItems.length} 个文件，完成后会自动向量化`);
  let originalButtonLabel = '';
  if (uploadBtn) {
    originalButtonLabel = uploadBtn.textContent;
    uploadBtn.disabled = true;
    uploadBtn.textContent = '上传中...';
  }
  try {
    const resp = await fetch('/api/docs/upload', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + token },
      body: form,
    });
    if (!resp.ok) {
      let errText = resp.statusText;
      if (resp.status === 401) {
        pendingItems.forEach((item) => {
          item.status = 'failed';
          item.progress = STATUS_PROGRESS.failed;
          item.note = '登录已失效';
        });
        renderUploadQueue();
        localStorage.removeItem(TOKEN_KEY);
        setStatus(statusEl, '登录已失效，请重新登录');
        return;
      }
      try {
        const err = await resp.json();
        errText = err.detail || errText;
      } catch (_) {}
      pendingItems.forEach((item) => {
        item.status = 'failed';
        item.progress = STATUS_PROGRESS.failed;
        item.note = errText;
      });
      renderUploadQueue();
      setStatus(statusEl, '错误: ' + errText);
      return;
    }
    const data = await resp.json();
    const processed = Array.isArray(data.processed) ? data.processed : [];
    const failed = Array.isArray(data.failed) ? data.failed : [];
    const vectorization = data.vectorization || {};
    const autoVectorize = vectorization.auto_vectorize !== undefined ? !!vectorization.auto_vectorize : true;
    const processedSet = new Set(processed);
    pendingItems.forEach((item) => {
      if (!processedSet.has(item.file.name)) return;
      if (autoVectorize && vectorization.scheduled) {
        item.status = 'vectorizing';
        item.progress = STATUS_PROGRESS.vectorizing;
        item.note = '等待向量化完成...';
      } else {
        item.status = 'completed';
        item.progress = STATUS_PROGRESS.completed;
        item.note = '上传完成';
      }
    });
    failed.forEach((fail) => {
      pendingItems.forEach((item) => {
        if (item.file.name === fail.filename) {
          item.status = 'failed';
          item.progress = STATUS_PROGRESS.failed;
          item.note = fail.reason || '处理失败';
        }
      });
    });
    renderUploadQueue();
    let message;
    if (autoVectorize && vectorization.scheduled) {
      const pendingList = Array.isArray(vectorization.pending) ? vectorization.pending : processed;
      const names = pendingList.slice(0, 3).join('、');
      const suffix = pendingList.length > 3 ? ` 等 ${pendingList.length} 个` : '';
      message = `已提交 ${names || pendingList.length} 个文件${suffix}，后台向量化中`;
    } else {
      message = `已处理 ${processed.length} 个文件`;
      if (typeof data.docs_count === 'number') {
        message += `，当前切片数 ${data.docs_count}`;
      }
    }
    if (failed.length) {
      const failedInfo = failed
        .map((item) => `${item.filename || '未知文件'}(${item.reason || '失败'})`)
        .join('、');
      message += `；失败 ${failed.length} 个：${failedInfo}`;
    }
    if (data.status === 'failed' && !processed.length) {
      message = '上传失败，请检查文件后重试';
    }
    setStatus(statusEl, message);
    await loadOverview();
    renderUploadQueue();
    scheduleQueueCleanup();
  } catch (e) {
    pendingItems.forEach((item) => {
      item.status = 'failed';
      item.progress = STATUS_PROGRESS.failed;
      item.note = String(e);
    });
    renderUploadQueue();
    setStatus(statusEl, '异常: ' + e);
  } finally {
    if (uploadBtn) {
      uploadBtn.disabled = false;
      uploadBtn.textContent = originalButtonLabel || '上传';
    }
  }
}

function resetRegisterForm(keepStatus = false) {
  const idInput = document.getElementById('studentIdInput');
  const passwordInput = document.getElementById('studentPasswordInput');
  const statusEl = document.getElementById('registerStatus');
  if (idInput) idInput.value = '';
  if (passwordInput) passwordInput.value = '';
  if (!keepStatus && statusEl) statusEl.textContent = '';
}

async function registerStudent() {
  const token = ensureAdminToken();
  const statusEl = document.getElementById('registerStatus');
  const idInput = document.getElementById('studentIdInput');
  const passwordInput = document.getElementById('studentPasswordInput');
  if (!token || !idInput) return;
  const studentId = idInput.value.trim();
  if (!studentId) {
    alert('请输入学号');
    return;
  }
  const payload = { student_id: studentId };
  const customPassword = passwordInput ? passwordInput.value.trim() : '';
  if (customPassword) {
    payload.password = customPassword;
  }
  if (statusEl) statusEl.textContent = '创建中...';
  try {
    const resp = await fetch('/api/admin/users/register', {
      method: 'POST',
      headers: authHeaders(token, { 'Content-Type': 'application/json' }),
      body: JSON.stringify(payload),
    });
    if (resp.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      window.location.href = '/admin';
      return;
    }
    if (!resp.ok) {
      let errText = resp.statusText;
      try {
        const err = await resp.json();
        errText = err.detail || errText;
      } catch (_) {}
      if (statusEl) statusEl.textContent = '创建失败: ' + errText;
      return;
    }
    const data = await resp.json();
    if (statusEl) {
      statusEl.textContent = `账号 ${data.username || studentId} 创建成功，初始密码 ${data.initial_password}`;
    }
    resetRegisterForm(true);
  } catch (error) {
    if (statusEl) statusEl.textContent = '异常: ' + error;
  }
}

window.addEventListener('DOMContentLoaded', () => {
  setupAdminMenu();
  setupTabs();
  renderUploadQueue();
  renderUploadLimits();
  const fileInput = document.getElementById('fileInput');
  if (fileInput) {
    updateSelectedFilesList(null);
    fileInput.addEventListener('change', async (event) => {
      const target = event.target;
      if (target && target.files && target.files.length) {
        const files = Array.from(target.files);
        for (const file of files) {
          // eslint-disable-next-line no-await-in-loop
          await enqueueFile(file);
        }
        target.value = '';
      }
    });
  }
  const token = ensureAdminToken();
  if (!token) return;
  loadOverview();
  startOverviewPolling(SLOW_POLL_MS);
});

window.addEventListener('beforeunload', () => {
  stopOverviewPolling();
});

async function deleteDoc(name) {
  const token = ensureAdminToken();
  const statusEl = document.getElementById('docsStatus');
  if (!token) return;
  if (!confirm(`确认删除 ${name} ?`)) return;
  if (statusEl) statusEl.textContent = '删除中...';
  try {
    const resp = await fetch(`/api/admin/docs/${encodeURIComponent(name)}`, {
      method: 'DELETE',
      headers: { 'Authorization': 'Bearer ' + token }
    });
    if (!resp.ok) {
      if (resp.status === 401) {
        localStorage.removeItem(TOKEN_KEY);
        window.location.href = '/admin';
        return;
      }
      let errText = resp.statusText;
      try {
        const err = await resp.json();
        errText = err.detail || errText;
      } catch (_) {}
      if (statusEl) statusEl.textContent = '删除失败: ' + errText;
      return;
    }
    const data = await resp.json();
    await loadOverview();
    if (statusEl) statusEl.textContent = `已删除 ${data.name}，剩余 ${data.docs_count} 个文档`;
  } catch (e) {
    if (statusEl) statusEl.textContent = '异常: ' + e;
  }
}

async function downloadDocAdmin(name) {
  const token = ensureAdminToken();
  const statusEl = document.getElementById('docsStatus');
  if (!token) return;
  if (statusEl) statusEl.textContent = `准备下载 ${name} ...`;
  try {
    const resp = await fetch(`/api/admin/docs/${encodeURIComponent(name)}/download`, {
      headers: authHeaders(token),
    });
    if (!resp.ok) {
      if (resp.status === 401) {
        localStorage.removeItem(TOKEN_KEY);
        window.location.href = '/admin';
        return;
      }
      const errText = await resp.text();
      if (statusEl) statusEl.textContent = '下载失败: ' + errText;
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    if (statusEl) statusEl.textContent = `已下载 ${name}`;
  } catch (e) {
    if (statusEl) statusEl.textContent = '下载异常: ' + e;
  }
}

function formatSize(bytes) {
  if (!bytes && bytes !== 0) return '未知大小';
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  const mb = kb / 1024;
  return `${mb.toFixed(2)} MB`;
}

function formatTimestamp(ts) {
  if (!ts) return '未知时间';
  const date = new Date(ts * 1000);
  if (Number.isNaN(date.getTime())) return '未知时间';
  return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')} ${String(date.getHours()).padStart(2,'0')}:${String(date.getMinutes()).padStart(2,'0')}`;
}


async function runTestQuery() {
  const token = ensureAdminToken();
  if (!token) return;
  const questionEl = document.getElementById('testerQuestion');
  const testerStatus = document.getElementById('testerStatus');
  const resultEl = document.getElementById('testerResult');
  if (!questionEl) return;
  const query = questionEl.value.trim();
  if (!query) {
    setStatus(testerStatus, '请输入测试问题');
    return;
  }
  if (resultEl) resultEl.classList.add('hidden');
  setStatus(testerStatus, '调试中...');
  const payload = {
    query,
    streaming: false,
  };
  try {
    const resp = await fetch('/api/admin/test_query', {
      method: 'POST',
      headers: authHeaders(token, { 'Content-Type': 'application/json' }),
      body: JSON.stringify(payload),
    });
    if (resp.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      window.location.href = '/admin';
      return;
    }
    if (!resp.ok) {
      let errText = resp.statusText;
      try {
        const err = await resp.json();
        errText = err.detail || errText;
      } catch (_) {}
      setStatus(testerStatus, '调用失败: ' + errText);
      return;
    }
    const data = await resp.json();
    renderTesterResult(data);
    setStatus(testerStatus, '调试完成');
  } catch (error) {
    setStatus(testerStatus, '异常: ' + error);
  }
}
