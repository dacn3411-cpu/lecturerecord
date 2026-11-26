# 🎙️ LectureRecord AI — 갤럭시 강의 녹음 자동 분석 · 노션 정리 · 삼성 캘린더 알림 시스템

갤럭시(Android)의 **기존 강좌별 녹음 파일(.m4a, .mp3 등) 일괄 업로드**와 **앞으로 녹음하는 강의의 즉시 자동 업로드**를 지원하며, 업로드 즉시 **Gemini 2.5 Flash (무료 티어)** 가 한국어 음성을 인식하여 **노션(Notion)에 강의 요약 및 할 일을 정리**하고 **삼성 캘린더(Samsung Calendar)에 과제/시험 마감 알림**을 자동 등록합니다.

---

## 🚀 전체 작동 흐름

```
[갤럭시 핸드폰]
 ├─ 방법 1: 모바일 웹 앱(PWA)에서 기존 강좌별 녹음 다중 선택 업로드 / 실시간 녹음
 └─ 방법 2: 삼성 음성 녹음 앱 종료 즉시 MacroDroid가 백그라운드 자동 업로드
        │
        ▼ (recordings/<강좌명>/<날짜_파일명>.m4a 업로드 감지)
[GitHub Actions 자동 실행 (.github/workflows/analyze_lecture.yml)]
        │
        ├─ 🧠 Gemini 2.5 Flash 오디오 네이티브 분석
        │     ├─ 강좌명 자동 분류 & 강의 핵심 3줄 요약
        │     ├─ 섹션별 상세 필기 & 교수님 시험 강조("시험에 나온다") 포인트 추출
        │     └─ 해야 할 것(과제/시험/퀴즈/복습) + 마감일(YYYY-MM-DD HH:MM) 추출
        │
        ├─ 📝 노션(Notion) 자동 동기화
        │     └─ 강의 요약 노트 페이지 생성 + 할 일(To-do) 체크리스트 등록
        │
        ├─ 📅 삼성 캘린더(Samsung Calendar) 알림 연동
        │     ├─ docs/calendar.ics 구독 피드 자동 갱신 (마감 1일 전, 2시간 전 알람 포함)
        │     └─ (선택) 구글 캘린더 API 다이렉트 Push → 갤럭시 삼성 캘린더 즉시 알림
        │
        └─ 📂 notes/<강좌명>/<날짜_강의명>.md 마크다운 영구 보관
```

---

## 🛠️ 최초 1회 설정 가이드 (3분 완성)

### 1단계: GitHub Secrets 등록 (AI 분석 & 노션 연동)
저장소 상단 메뉴 **[Settings] → [Secrets and variables] → [Actions] → [New repository secret]** 에서 아래 키를 등록하세요.

| Secret 이름 | 필수 여부 | 설명 및 발급 방법 |
| --- | --- | --- |
| `GEMINI_API_KEY` | **필수 (무료)** | [Google AI Studio](https://aistudio.google.com/apikey)에서 무료 발급. 1~2시간짜리 `.m4a` 강의 녹음도 변환 없이 직접 정밀 분석합니다. |
| `NOTION_TOKEN` | **권장 (노션)** | [노션 내 통합(Integrations)](https://www.notion.so/my-integrations)에서 새 통합 생성 후 발급받은 `ntn_...` 또는 `secret_...` 토큰 |
| `NOTION_PARENT_PAGE_ID` | **권장 (노션)** | 강의 노트와 할 일을 자동으로 쌓을 노션 페이지의 32자리 ID (해당 노션 페이지 우측 상단 `•••` → `연결(Connections)`에서 생성한 통합을 반드시 추가해주세요) |
| `NOTION_LECTURE_DB_ID` | 선택 | 부모 페이지 대신 특정 노션 데이터베이스에 강의를 넣을 경우 지정 (`scripts/setup_notion_databases.py`로 자동 생성 가능) |
| `NOTION_TASK_DB_ID` | 선택 | 할 일만 모아두는 별도 노션 데이터베이스 ID |

---

### 2단계: 모바일 웹 업로더 & 대시보드 (GitHub Pages 활성화)
1. 저장소 **[Settings] → [Pages]** 이동
2. **Build and deployment** → Source를 `Deploy from a branch`로 선택
3. Branch를 `main` (또는 기본 브랜치), 폴더를 **`/docs`** 로 선택 후 **Save**
4. 약 1분 뒤 `https://dacn3411-cpu.github.io/lecturerecord/` 주소로 갤럭시 브라우저에서 접속하여 **[홈 화면에 추가]** 를 누르면 전용 앱처럼 사용할 수 있습니다.
   - **기존 강좌별 녹음 업로드**: 과목명을 입력(또는 칩 선택)하고 갤럭시 내 기존 녹음 파일들을 한 번에 다중 선택하여 업로드하세요.
   - **실시간 녹음**: 강의실에서 웹 앱의 `🔴 실시간 녹음 시작`을 누르고 수업이 끝날 때 종료를 누르면 그 자리에서 즉시 업로드 및 분석이 시작됩니다.

---

### 3단계: 삼성 캘린더(Samsung Calendar)에 과제/시험 알림 연동
1. 갤럭시 **삼성 캘린더 앱**은 핸드폰에 로그인된 **구글 계정 캘린더**와 기본 동기화됩니다.
2. [구글 캘린더 웹(calendar.google.com)](https://calendar.google.com) 좌측 메뉴 **다른 캘린더 `+` → URL로 추가** 클릭
3. 아래 주소를 입력하고 추가합니다:
   ```text
   https://dacn3411-cpu.github.io/lecturerecord/calendar.ics
   ```
4. 또는 모바일 웹 앱의 **[📱 갤럭시 무인 자동화 & 캘린더 설정]** 탭에서 `.ics` 파일을 터치하면 삼성 캘린더 앱에 일정과 알림(마감 1일 전 / 2시간 전)이 바로 추가됩니다.

---

## 📁 폴더 구조

- [`recordings/`](file:///home/antigravity/dacn3411-cpu/lecturerecord/recordings): 새로 업로드되는 녹음 파일이 들어오는 폴더 (강좌별 하위 폴더 `recordings/운영체제/강의1.m4a` 형태 지원, 처리 완료 후 `recordings/processed/`로 자동 이동)
- [`notes/`](file:///home/antigravity/dacn3411-cpu/lecturerecord/notes): AI가 분석한 강좌별 마크다운 강의 노트와 할 일 목록이 자동 저장되는 폴더
- [`docs/`](file:///home/antigravity/dacn3411-cpu/lecturerecord/docs): 갤럭시 모바일 웹 업로더(PWA), 대시보드, 그리고 삼성 캘린더 구독용 [`calendar.ics`](file:///home/antigravity/dacn3411-cpu/lecturerecord/docs/calendar.ics)
- [`scripts/process_recordings.py`](file:///home/antigravity/dacn3411-cpu/lecturerecord/scripts/process_recordings.py): Gemini 2.5 Flash 오디오 분석 + 노션 API 동기화 + 캘린더 피드 생성 핵심 스크립트
- [`.github/workflows/analyze_lecture.yml`](file:///home/antigravity/dacn3411-cpu/lecturerecord/.github/workflows/analyze_lecture.yml): 녹음 파일이 올라오는 즉시 실행되는 GitHub Actions 워크플로우
