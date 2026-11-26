/**
 * LectureRecord AI — Mobile Web App (PWA) Logic
 * Supports:
 * 1. Batch uploading existing Galaxy voice recordings (.m4a, .mp3, etc.) categorized by course
 * 2. Live in-browser lecture recording with immediate upload on stop
 * 3. Viewing extracted Action Items (assignments/exams) and AI lecture summaries
 */

const STORAGE_KEYS = {
  OWNER: 'lr_github_owner',
  REPO: 'lr_github_repo',
  TOKEN: 'lr_github_token',
  COURSES: 'lr_saved_courses',
};

const DEFAULT_COURSES = ['전공필수', '전공선택', '교양', '세미나'];

let uploadQueue = [];
let mediaRecorder = null;
let recordedChunks = [];
let recordStartTime = null;
let recordTimerInterval = null;

document.addEventListener('DOMContentLoaded', () => {
  initDateInput();
  loadSettingsIntoUI();
  renderCourseChips();
  setupDragAndDrop();
  loadLectureData();
});

function initDateInput() {
  const dateInput = document.getElementById('date-input');
  if (dateInput) {
    const today = new Date();
    const yyyy = today.getFullYear();
    const mm = String(today.getMonth() + 1).padStart(2, '0');
    const dd = String(today.getDate()).padStart(2, '0');
    dateInput.value = `${yyyy}-${mm}-${dd}`;
  }
}

function switchTab(tabName) {
  ['upload', 'dashboard', 'galaxy', 'settings'].forEach((name) => {
    const btn = document.getElementById(`tab-${name}-btn`);
    const panel = document.getElementById(`panel-${name}`);
    if (btn && panel) {
      const isActive = name === tabName;
      btn.classList.toggle('active', isActive);
      panel.classList.toggle('active', isActive);
    }
  });
}

function getSavedCourses() {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.COURSES);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length > 0) return parsed;
    }
  } catch (e) {
    console.warn(e);
  }
  return DEFAULT_COURSES;
}

function renderCourseChips() {
  const container = document.getElementById('course-chips-container');
  const courseInput = document.getElementById('course-input');
  if (!container) return;

  const courses = getSavedCourses();
  container.innerHTML = '';

  courses.forEach((course) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'course-chip';
    if (courseInput && courseInput.value === course) {
      chip.classList.add('selected');
    }
    chip.textContent = course;
    chip.onclick = () => {
      if (courseInput) {
        courseInput.value = course;
        renderCourseChips();
      }
    };
    container.appendChild(chip);
  });
}

function addSavedCourse() {
  const input = document.getElementById('new-course-add');
  const courseInput = document.getElementById('course-input');
  if (!input) return;

  const val = input.value.trim();
  if (!val) return;

  const courses = getSavedCourses();
  if (!courses.includes(val)) {
    courses.unshift(val);
    localStorage.setItem(STORAGE_KEYS.COURSES, JSON.stringify(courses));
  }
  input.value = '';
  if (courseInput) courseInput.value = val;
  renderCourseChips();
}

function setupDragAndDrop() {
  const dropzone = document.getElementById('file-dropzone');
  if (!dropzone) return;

  dropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropzone.classList.add('dragover');
  });

  dropzone.addEventListener('dragleave', () => {
    dropzone.classList.remove('dragover');
  });

  dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    if (e.dataTransfer && e.dataTransfer.files) {
      enqueueFiles(Array.from(e.dataTransfer.files));
    }
  });
}

function handleFileSelect(event) {
  const files = Array.from(event.target.files || []);
  enqueueFiles(files);
  event.target.value = '';
}

function enqueueFiles(files) {
  const course = (document.getElementById('course-input')?.value || '').trim() || '자동분류';
  const dateStr = document.getElementById('date-input')?.value || new Date().toISOString().slice(0, 10);

  files.forEach((file) => {
    uploadQueue.push({
      id: `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      file,
      course,
      date: dateStr,
      status: 'pending',
      statusText: '대기 중',
    });
  });

  renderQueue();
}

function clearQueue() {
  uploadQueue = [];
  renderQueue();
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderQueue() {
  const section = document.getElementById('upload-queue-section');
  const list = document.getElementById('queue-list');
  const countSpan = document.getElementById('queue-count');

  if (!section || !list || !countSpan) return;

  if (uploadQueue.length === 0) {
    section.style.display = 'none';
    return;
  }

  section.style.display = 'block';
  countSpan.textContent = String(uploadQueue.length);
  list.innerHTML = '';

  uploadQueue.forEach((item) => {
    const row = document.createElement('div');
    row.className = 'queue-item';

    const targetPath = item.course && item.course !== '자동분류'
      ? `recordings/${item.course}/${item.date}_${item.file.name}`
      : `recordings/${item.date}_${item.file.name}`;

    row.innerHTML = `
      <div class="queue-item-info">
        <div class="queue-item-name">🎵 ${escapeHtml(item.file.name)} (${formatBytes(item.file.size)})</div>
        <div class="queue-item-meta">저장 경로: <code>${escapeHtml(targetPath)}</code></div>
      </div>
      <span class="queue-badge ${item.status}">${escapeHtml(item.statusText)}</span>
    `;
    list.appendChild(row);
  });
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = String(reader.result || '');
      const base64 = result.includes(',') ? result.split(',')[1] : result;
      resolve(base64);
    };
    reader.onerror = (err) => reject(err);
    reader.readAsDataURL(file);
  });
}

async function uploadSingleItem(item, settings) {
  item.status = 'uploading';
  item.statusText = 'GitHub 업로드 중...';
  renderQueue();

  const safeCourse = item.course && item.course !== '자동분류'
    ? item.course.replace(/[\\/*?:"<>|]/g, '_').trim()
    : '';

  const safeFileName = item.file.name.replace(/[\\/*?:"<>|]/g, '_');
  const relativePath = safeCourse
    ? `recordings/${safeCourse}/${item.date}_${safeFileName}`
    : `recordings/${item.date}_${safeFileName}`;

  const apiUrl = `https://api.github.com/repos/${encodeURIComponent(settings.owner)}/${encodeURIComponent(settings.repo)}/contents/${relativePath.split('/').map(encodeURIComponent).join('/')}`;

  const base64Content = await fileToBase64(item.file);

  // Check if file already exists to get sha
  let existingSha = undefined;
  try {
    const checkResp = await fetch(apiUrl, {
      headers: {
        Authorization: `Bearer ${settings.token}`,
        Accept: 'application/vnd.github+json',
      },
    });
    if (checkResp.ok) {
      const existingData = await checkResp.json();
      existingSha = existingData.sha;
    }
  } catch (e) {
    // ignore
  }

  const body = {
    message: `🎙️ Upload lecture recording: [${item.course}] ${item.file.name}`,
    content: base64Content,
  };
  if (existingSha) {
    body.sha = existingSha;
  }

  const putResp = await fetch(apiUrl, {
    method: 'PUT',
    headers: {
      Authorization: `Bearer ${settings.token}`,
      Accept: 'application/vnd.github+json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });

  if (!putResp.ok) {
    const errText = await putResp.text();
    throw new Error(`HTTP ${putResp.status}: ${errText}`);
  }

  item.status = 'done';
  item.statusText = '✅ 업로드 완료 (AI 분석 자동 시작됨)';
  renderQueue();
}

async function uploadAllQueued() {
  const settings = getGitHubSettings();
  if (!settings.token) {
    alert('먼저 [🔑 GitHub 연결 설정] 탭에서 GitHub Personal Access Token을 입력하고 저장해주세요.');
    switchTab('settings');
    return;
  }

  const pendingItems = uploadQueue.filter((i) => i.status !== 'done');
  if (pendingItems.length === 0) {
    alert('업로드할 대기 파일이 없습니다.');
    return;
  }

  const uploadBtn = document.getElementById('btn-start-upload');
  if (uploadBtn) uploadBtn.disabled = true;

  for (const item of pendingItems) {
    try {
      await uploadSingleItem(item, settings);
    } catch (err) {
      console.error(err);
      item.status = 'error';
      item.statusText = `❌ 실패: ${err.message.slice(0, 60)}`;
      renderQueue();
    }
  }

  if (uploadBtn) uploadBtn.disabled = false;
}

/* Live In-Browser Audio Recorder */
async function toggleRecording() {
  const btn = document.getElementById('btn-start-record');
  const statusEl = document.getElementById('record-status');

  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    clearInterval(recordTimerInterval);
    if (btn) {
      btn.textContent = '🔴 실시간 녹음 시작';
      btn.className = 'btn btn-primary';
    }
    if (statusEl) statusEl.textContent = '녹음 완료! 대기열에 추가되었습니다.';
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recordedChunks = [];
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
      ? 'audio/webm;codecs=opus'
      : 'audio/mp4';

    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) {
        recordedChunks.push(e.data);
      }
    };

    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const ext = mimeType.includes('webm') ? 'webm' : 'm4a';
      const blob = new Blob(recordedChunks, { type: mimeType });
      const now = new Date();
      const timeTag = `${String(now.getHours()).padStart(2, '0')}${String(now.getMinutes()).padStart(2, '0')}`;
      const course = (document.getElementById('course-input')?.value || '').trim() || '실시간강의';
      const fileName = `${course}_강의녹음_${timeTag}.${ext}`;
      const file = new File([blob], fileName, { type: mimeType });

      enqueueFiles([file]);

      // Auto-upload immediately if GitHub token is configured
      const settings = getGitHubSettings();
      if (settings.token) {
        await uploadAllQueued();
      }
    };

    mediaRecorder.start(1000);
    recordStartTime = Date.now();
    updateRecordTimer();
    recordTimerInterval = setInterval(updateRecordTimer, 1000);

    if (btn) {
      btn.textContent = '⏹️ 녹음 종료 & 즉시 업로드';
      btn.className = 'btn btn-danger';
    }
    if (statusEl) statusEl.textContent = '🎙️ 강의 녹음 중... 종료 시 자동 업로드됩니다.';
  } catch (err) {
    alert(`마이크 권한을 허용해주세요: ${err.message}`);
  }
}

function updateRecordTimer() {
  const timerEl = document.getElementById('record-timer');
  if (!timerEl || !recordStartTime) return;
  const elapsedSec = Math.floor((Date.now() - recordStartTime) / 1000);
  const hrs = String(Math.floor(elapsedSec / 3600)).padStart(2, '0');
  const mins = String(Math.floor((elapsedSec % 3600) / 60)).padStart(2, '0');
  const secs = String(elapsedSec % 60).padStart(2, '0');
  timerEl.textContent = `${hrs}:${mins}:${secs}`;
}

/* Dashboard Data Loading */
async function loadLectureData() {
  const tasksContainer = document.getElementById('tasks-container');
  const lecturesContainer = document.getElementById('lectures-container');
  if (!tasksContainer || !lecturesContainer) return;

  try {
    const resp = await fetch(`data/lectures.json?t=${Date.now()}`);
    if (!resp.ok) throw new Error('Not found');
    const data = await resp.json();
    renderDashboard(data);
  } catch (e) {
    renderDashboard({ lectures: [], tasks: [] });
  }
}

function renderDashboard(data) {
  const tasksContainer = document.getElementById('tasks-container');
  const lecturesContainer = document.getElementById('lectures-container');

  const tasks = data.tasks || [];
  const lectures = data.lectures || [];

  if (tasks.length === 0) {
    tasksContainer.innerHTML = `
      <div class="task-item">
        <div class="task-main">
          <div style="font-weight: 600;">아직 분석된 과제/할 일이 없습니다.</div>
          <div style="font-size: 0.84rem; color: var(--text-secondary);">
            [녹음 업로드] 탭에서 기존 강의 녹음 파일(.m4a, .mp3)을 업로드하면 약 1분 뒤 AI가 과제와 마감일을 이곳과 노션, 삼성 캘린더에 자동 등록합니다.
          </div>
        </div>
      </div>
    `;
  } else {
    tasksContainer.innerHTML = tasks
      .map(
        (t) => `
      <div class="task-item">
        <div class="task-main">
          <div class="task-badges">
            <span class="badge course">${escapeHtml(t.course)}</span>
            <span class="badge">${escapeHtml(t.category)}</span>
            <span class="badge ${t.priority === '높음' ? 'urgent' : ''}">중요도: ${escapeHtml(t.priority)}</span>
          </div>
          <div style="font-weight: 700; font-size: 0.98rem;">${escapeHtml(t.title)}</div>
          <div style="font-size: 0.84rem; color: var(--text-secondary);">${escapeHtml(t.details || '')}</div>
        </div>
        <div style="text-align: right; white-space: nowrap;">
          <div style="font-family: var(--font-mono); font-size: 0.85rem; color: var(--accent-primary); font-weight: 700;">
            📅 ${escapeHtml(t.due_date)}
          </div>
          <div style="font-size: 0.76rem; color: var(--text-muted);">${escapeHtml(t.due_time || '23:59')} 마감</div>
        </div>
      </div>
    `
      )
      .join('');
  }

  if (lectures.length === 0) {
    lecturesContainer.innerHTML = `
      <div class="lecture-card">
        <div style="font-weight: 600;">아직 분석된 강의 노트가 없습니다.</div>
        <p style="font-size: 0.85rem; color: var(--text-secondary); margin-top: 4px;">
          녹음 파일을 업로드하면 강좌별 3줄 핵심 요약, 시험 출제 강조 포인트, 상세 필기가 이곳과 노션에 자동 정리됩니다.
        </p>
      </div>
    `;
  } else {
    lecturesContainer.innerHTML = lectures
      .map(
        (lec) => `
      <article class="lecture-card">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
          <span class="badge course">${escapeHtml(lec.course_name)}</span>
          <span style="font-size: 0.8rem; color: var(--text-muted);">${escapeHtml(lec.recorded_date)}</span>
        </div>
        <h3>${escapeHtml(lec.lecture_title)}</h3>
        <div class="lecture-summary">${escapeHtml(lec.summary_3lines)}</div>
        ${
          lec.exam_and_emphasis && lec.exam_and_emphasis.length > 0
            ? `<div style="background: rgba(245, 158, 11, 0.1); border-left: 3px solid var(--accent-warning); padding: 10px 12px; border-radius: 6px; font-size: 0.84rem; margin-bottom: 10px;">
                <strong>⭐ 교수님 강조 포인트:</strong>
                <ul style="padding-left: 18px; margin-top: 4px;">
                  ${lec.exam_and_emphasis.map((e) => `<li>${escapeHtml(e)}</li>`).join('')}
                </ul>
              </div>`
            : ''
        }
        <div style="display: flex; gap: 10px; flex-wrap: wrap;">
          ${
            lec.notion_url
              ? `<a href="${escapeHtml(lec.notion_url)}" target="_blank" rel="noopener" class="btn btn-secondary" style="padding: 6px 12px; font-size: 0.8rem;">📝 노션에서 전체 노트 열기</a>`
              : ''
          }
          <a href="https://github.com/dacn3411-cpu/lecturerecord/blob/main/${encodeURI(lec.note_path || '')}" target="_blank" rel="noopener" class="btn btn-secondary" style="padding: 6px 12px; font-size: 0.8rem;">📄 GitHub 마크다운 원본 보기</a>
        </div>
      </article>
    `
      )
      .join('');
  }
}

/* GitHub Settings Management */
function getGitHubSettings() {
  return {
    owner: localStorage.getItem(STORAGE_KEYS.OWNER) || 'dacn3411-cpu',
    repo: localStorage.getItem(STORAGE_KEYS.REPO) || 'lecturerecord',
    token: localStorage.getItem(STORAGE_KEYS.TOKEN) || '',
  };
}

function loadSettingsIntoUI() {
  const s = getGitHubSettings();
  const ownerEl = document.getElementById('setting-owner');
  const repoEl = document.getElementById('setting-repo');
  const tokenEl = document.getElementById('setting-token');

  if (ownerEl) ownerEl.value = s.owner;
  if (repoEl) repoEl.value = s.repo;
  if (tokenEl) tokenEl.value = s.token;
}

function saveSettings() {
  const owner = document.getElementById('setting-owner')?.value.trim() || 'dacn3411-cpu';
  const repo = document.getElementById('setting-repo')?.value.trim() || 'lecturerecord';
  const token = document.getElementById('setting-token')?.value.trim() || '';

  localStorage.setItem(STORAGE_KEYS.OWNER, owner);
  localStorage.setItem(STORAGE_KEYS.REPO, repo);
  localStorage.setItem(STORAGE_KEYS.TOKEN, token);

  const fb = document.getElementById('settings-feedback');
  if (fb) {
    fb.style.color = 'var(--accent-secondary)';
    fb.textContent = '✅ 설정이 현재 기기 브라우저에 안전하게 저장되었습니다.';
  }
}

async function testGitHubConnection() {
  saveSettings();
  const s = getGitHubSettings();
  const fb = document.getElementById('settings-feedback');
  if (!s.token) {
    if (fb) {
      fb.style.color = 'var(--accent-danger)';
      fb.textContent = '❌ GitHub Personal Access Token을 입력해주세요.';
    }
    return;
  }

  try {
    const resp = await fetch(`https://api.github.com/repos/${encodeURIComponent(s.owner)}/${encodeURIComponent(s.repo)}`, {
      headers: {
        Authorization: `Bearer ${s.token}`,
        Accept: 'application/vnd.github+json',
      },
    });
    if (resp.ok) {
      const data = await resp.json();
      if (fb) {
        fb.style.color = 'var(--accent-secondary)';
        fb.textContent = `✅ 연결 성공! 저장소: ${data.full_name} (쓰기 권한: ${data.permissions?.push ? '있음' : '확인 필요'})`;
      }
    } else {
      throw new Error(`HTTP ${resp.status}`);
    }
  } catch (err) {
    if (fb) {
      fb.style.color = 'var(--accent-danger)';
      fb.textContent = `❌ 연결 실패 (${err.message}). 토큰과 저장소 이름을 확인해주세요.`;
    }
  }
}

function copyIcsUrl() {
  const url = document.getElementById('ics-url-display')?.textContent || '';
  navigator.clipboard.writeText(url).then(() => {
    alert('캘린더 구독 URL이 복사되었습니다!');
  });
}

function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
