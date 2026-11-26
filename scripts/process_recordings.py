#!/usr/bin/env python3
"""
Lecture Recording Automated Analyzer & Sync Engine
- Watches `recordings/` for new audio files (.m4a, .mp3, .wav, .aac, .ogg, .flac, .mp4, .webm)
- Uses Gemini 2.5 Flash API (free tier) to transcribe, summarize, and extract action items/deadlines
- Syncs structured Lecture Notes and To-Do items to Notion
- Updates `docs/calendar.ics` (with VALARM reminders for Samsung Calendar subscription)
  and optionally pushes directly to Google Calendar (which syncs with Samsung Calendar)
- Saves Markdown notes to `notes/<course>/<date>_<title>.md` and updates `docs/data/lectures.json`
"""

import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz
import requests
from google import genai
from google.genai import types
from icalendar import Alarm, Calendar, Event

KST = pytz.timezone("Asia/Seoul")
ROOT_DIR = Path(__file__).resolve().parent.parent
RECORDINGS_DIR = ROOT_DIR / "recordings"
PROCESSED_DIR = RECORDINGS_DIR / "processed"
NOTES_DIR = ROOT_DIR / "notes"
DOCS_DIR = ROOT_DIR / "docs"
DATA_DIR = DOCS_DIR / "data"
STATE_FILE = DATA_DIR / "lectures.json"
ICS_FILE = DOCS_DIR / "calendar.ics"

SUPPORTED_EXTENSIONS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".mp4", ".webm", ".3gp", ".amr"}

MIME_MAP = {
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".mp4": "audio/mp4",
    ".webm": "audio/webm",
    ".3gp": "audio/3gpp",
    ".amr": "audio/amr",
}


def load_state() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[WARN] Failed to load state file: {e}")
    return {"lectures": [], "tasks": [], "processed_hashes": {}}


def save_state(state: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_course_and_date(file_path: Path, override_course: str = "") -> Dict[str, str]:
    """
    Infers course name and recording date from directory structure or filename.
    Supported patterns:
    - recordings/< 강좌명 >/< 파일명 >.m4a
    - recordings/[강좌명]_20261125_강의제목.m4a
    - Samsung Voice Recorder: "음성 001.m4a", "음성 261125_143000.m4a", "자료구조_2026-11-25.m4a"
    """
    rel_parts = file_path.relative_to(RECORDINGS_DIR).parts
    course_from_dir = rel_parts[0] if len(rel_parts) > 1 and rel_parts[0] != "processed" else ""

    stem = file_path.stem
    course = override_course.strip() or course_from_dir

    # Try prefix pattern: [Course] Title or Course_Date
    if not course:
        bracket_match = re.match(r"^\[([^\]]+)\]\s*(.*)$", stem)
        if bracket_match:
            course = bracket_match.group(1).strip()
            stem = bracket_match.group(2).strip() or stem
        elif "_" in stem:
            first_part = stem.split("_")[0].strip()
            if not re.match(r"^(음성|녹음|Voice|Record|\d+)$", first_part, re.IGNORECASE):
                course = first_part

    # Try extracting date from filename (YYYY-MM-DD, YYYYMMDD, YYMMDD)
    now_kst = datetime.now(KST)
    recorded_date = now_kst.strftime("%Y-%m-%d")

    date_match_4 = re.search(r"(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])", stem)
    if date_match_4:
        recorded_date = f"{date_match_4.group(1)}-{date_match_4.group(2)}-{date_match_4.group(3)}"
    else:
        date_match_2 = re.search(r"(?<!\d)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)", stem)
        if date_match_2:
            recorded_date = f"20{date_match_2.group(1)}-{date_match_2.group(2)}-{date_match_2.group(3)}"

    return {
        "course": course or "미분류 강좌",
        "date": recorded_date,
        "raw_title": stem,
    }


def analyze_audio_with_gemini(file_path: Path, meta: Dict[str, str]) -> Dict[str, Any]:
    """
    Uploads the audio file to Gemini File API and requests structured JSON analysis:
    - Inferred course name & lecture title
    - Executive summary
    - Detailed section-by-section lecture notes
    - Key concepts & exam tips emphasized by the professor
    - Action items / To-dos (assignments, quizzes, exams, readings) with due dates
    - Full/highlighted transcript
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY 환경변수(GitHub Secret)가 설정되어 있지 않습니다. "
            "Google AI Studio(https://aistudio.google.com/apikey)에서 무료 키를 발급받아 등록해주세요."
        )

    client = genai.Client(api_key=api_key)
    mime_type = MIME_MAP.get(file_path.suffix.lower()) or mimetypes.guess_type(str(file_path))[0] or "audio/mp4"

    print(f"[INFO] Uploading {file_path.name} ({mime_type}) to Gemini File API...")
    uploaded_file = client.files.upload(
        file=str(file_path),
        config=types.UploadFileConfig(mime_type=mime_type, display_name=file_path.name),
    )

    # Wait until file state is ACTIVE
    while uploaded_file.state.name == "PROCESSING":
        print("[INFO] Waiting for Gemini audio processing...")
        time.sleep(3)
        uploaded_file = client.files.get(name=uploaded_file.name)

    if uploaded_file.state.name != "ACTIVE":
        raise RuntimeError(f"Gemini file upload failed with state: {uploaded_file.state.name}")

    prompt = f"""
당신은 대학 강의 및 전문 강좌 녹음을 완벽하게 분석하여 노트와 해야 할 일(Action Items)을 정리하는 AI 조교입니다.

[기본 메타데이터]
- 파일명: {file_path.name}
- 폴더/파일 기반 추정 강좌명: {meta['course']}
- 녹음 기준 날짜: {meta['date']} (상대적 날짜 계산 시 이 날짜를 기준으로 YYYY-MM-DD를 계산하세요. 예: '다음 주 수요일까지', '내일까지')

오디오 전체를 주의 깊게 듣고 다음 JSON 스키마에 맞춰 한국어로 상세하고 정확하게 응답하세요:
1. `course_name`: 추정 강좌명 설정이 '미분류 강좌'라면 강의 내용에서 과목명/강좌명을 추론하고, 이미 지정되어 있다면 그대로 유지하세요.
2. `lecture_title`: 오늘 강의의 핵심 주제를 담은 명확한 제목
3. `summary_3lines`: 강의 전체 핵심 요약 (3~5문장)
4. `detailed_notes`: 강의 흐름에 따른 섹션별 상세 필기 노트 (배열: 각 항목은 `section_title`, `content`, `timestamp_hint`)
5. `exam_and_emphasis`: 교수님/강사가 "시험에 나온다", "중요하다", "꼭 기억해라"라고 강조한 포인트 목록 (배열)
6. `key_terms`: 핵심 용어 및 개념 정의 (배열: 각 항목은 `term`, `definition`)
7. `action_items`: 학생이 해야 할 일, 과제, 퀴즈, 시험 일정, 복습할 범위, 준비물 등 (배열).
   - 각 항목은:
     - `title`: 해야 할 일 제목 (예: "[자료구조] 3장 연습문제 과제 제출")
     - `category`: "과제" | "시험" | "퀴즈" | "복습/예습" | "기타" 중 택1
     - `priority`: "높음" | "보통" | "낮음"
     - `due_date`: 마감일/일정 날짜 (반드시 `YYYY-MM-DD` 형식. 강의에서 언급이 없으면 녹음일로부터 3일 뒤 날짜로 기본 지정하되 `is_exact_date`를 false로 설정)
     - `due_time`: 마감 시간 (`HH:MM` 24시간 형식, 언급 없으면 `"23:59"` 또는 시험인 경우 `"09:00"`)
     - `is_exact_date`: 강의 중 명시적인 마감일/시험일이 언급되었으면 `true`, 추천 복습 일정이면 `false`
     - `details`: 세부 요구사항 (제출 방식, 분량, 주의사항 등)
8. `transcript_excerpt`: 강의 주요 발언 및 핵심 내용 녹취록 (시간 흐름순 정리)
"""

    response_schema = {
        "type": "OBJECT",
        "properties": {
            "course_name": {"type": "STRING"},
            "lecture_title": {"type": "STRING"},
            "summary_3lines": {"type": "STRING"},
            "detailed_notes": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "section_title": {"type": "STRING"},
                        "timestamp_hint": {"type": "STRING"},
                        "content": {"type": "STRING"},
                    },
                    "required": ["section_title", "content"],
                },
            },
            "exam_and_emphasis": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
            },
            "key_terms": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "term": {"type": "STRING"},
                        "definition": {"type": "STRING"},
                    },
                    "required": ["term", "definition"],
                },
            },
            "action_items": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {"type": "STRING"},
                        "category": {"type": "STRING"},
                        "priority": {"type": "STRING"},
                        "due_date": {"type": "STRING"},
                        "due_time": {"type": "STRING"},
                        "is_exact_date": {"type": "BOOLEAN"},
                        "details": {"type": "STRING"},
                    },
                    "required": ["title", "category", "priority", "due_date", "due_time", "is_exact_date", "details"],
                },
            },
            "transcript_excerpt": {"type": "STRING"},
        },
        "required": [
            "course_name",
            "lecture_title",
            "summary_3lines",
            "detailed_notes",
            "exam_and_emphasis",
            "key_terms",
            "action_items",
            "transcript_excerpt",
        ],
    }

    print("[INFO] Running Gemini 2.5 Flash audio analysis...")
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[uploaded_file, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=0.2,
            ),
        )
        result = json.loads(response.text)
    finally:
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception:
            pass

    if meta["course"] != "미분류 강좌":
        result["course_name"] = meta["course"]
    result["recorded_date"] = meta["date"]
    result["source_file"] = str(file_path.relative_to(ROOT_DIR))
    return result


def write_markdown_note(analysis: Dict[str, Any]) -> Path:
    course = re.sub(r'[\\/*?:"<>|]', "_", analysis["course_name"]).strip() or "미분류"
    date_str = analysis["recorded_date"]
    safe_title = re.sub(r'[\\/*?:"<>|]', "_", analysis["lecture_title"]).strip()[:50]

    course_dir = NOTES_DIR / course
    course_dir.mkdir(parents=True, exist_ok=True)
    md_path = course_dir / f"{date_str}_{safe_title}.md"

    lines = [
        f"# [{analysis['course_name']}] {analysis['lecture_title']}",
        "",
        f"- **녹음 일자**: {date_str}",
        f"- **원본 파일**: `{analysis['source_file']}`",
        "",
        "## 📌 3줄 핵심 요약",
        analysis["summary_3lines"],
        "",
        "## ✅ 해야 할 일 (Action Items & 과제/시험)",
    ]

    if analysis.get("action_items"):
        for item in analysis["action_items"]:
            exact_badge = "📅 확정 일정" if item.get("is_exact_date") else "💡 권장 복습/할일"
            lines.append(
                f"- [ ] **[{item['category']}] {item['title']}** "
                f"(마감: `{item['due_date']} {item.get('due_time', '23:59')}` | 중요도: {item['priority']} | {exact_badge})"
            )
            if item.get("details"):
                lines.append(f"  - {item['details']}")
    else:
        lines.append("- 추출된 과제나 특별 할 일이 없습니다.")

    lines.extend(["", "## ⭐ 교수님 강조 & 시험 출제 포인트"])
    if analysis.get("exam_and_emphasis"):
        for emp in analysis["exam_and_emphasis"]:
            lines.append(f"- 🔥 {emp}")
    else:
        lines.append("- 별도 시험 강조 사항 없음")

    lines.extend(["", "## 📖 섹션별 상세 강의 노트"])
    for idx, sec in enumerate(analysis.get("detailed_notes", []), 1):
        ts = f" ({sec['timestamp_hint']})" if sec.get("timestamp_hint") else ""
        lines.append(f"### {idx}. {sec['section_title']}{ts}")
        lines.append(sec["content"])
        lines.append("")

    lines.extend(["## 🔑 핵심 용어 정리", "| 용어 | 설명 |", "| --- | --- |"])
    for kt in analysis.get("key_terms", []):
        term = kt["term"].replace("|", "\\|")
        defn = kt["definition"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| **{term}** | {defn} |")

    lines.extend(["", "## 🎙️ 주요 녹취록 요약", analysis.get("transcript_excerpt", "")])

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[INFO] Saved Markdown lecture note: {md_path.relative_to(ROOT_DIR)}")
    return md_path


def sync_to_notion(analysis: Dict[str, Any]) -> Optional[str]:
    """
    Syncs the analyzed lecture note and action items to Notion.
    Supports two modes:
    1. Database Mode: If NOTION_LECTURE_DB_ID (and optionally NOTION_TASK_DB_ID) are provided.
    2. Parent Page Mode: If NOTION_PARENT_PAGE_ID is provided, automatically creates child pages
       with structured blocks and to-do checkboxes.
    """
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        print("[INFO] NOTION_TOKEN not set; skipping Notion sync.")
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    lecture_db_id = os.environ.get("NOTION_LECTURE_DB_ID", "").strip()
    task_db_id = os.environ.get("NOTION_TASK_DB_ID", "").strip()
    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip() or "424c5c90104580a8a342d929114d2402"
    if "notion." in parent_page_id:
        m = re.search(r"([0-9a-fA-F]{32})", parent_page_id.replace("-", ""))
        if m:
            parent_page_id = m.group(1)

    # Build rich children blocks for the lecture page
    children: List[Dict[str, Any]] = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "🎓"},
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": f"[{analysis['course_name']}] {analysis['recorded_date']} 강의 핵심 요약\n\n{analysis['summary_3lines'][:1800]}"
                        },
                    }
                ],
            },
        },
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "✅ 해야 할 것 (과제 / 시험 / 복습)"}}]},
        },
    ]

    for item in analysis.get("action_items", []):
        todo_text = f"[{item['category']}] {item['title']} (마감: {item['due_date']} {item.get('due_time', '23:59')}) — {item.get('details', '')}"
        children.append(
            {
                "object": "block",
                "type": "to_do",
                "to_do": {
                    "rich_text": [{"type": "text", "text": {"content": todo_text[:1900]}}],
                    "checked": False,
                },
            }
        )

    if analysis.get("exam_and_emphasis"):
        children.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": "⭐ 교수님 강조 & 시험 포인트"}}]},
            }
        )
        for emp in analysis["exam_and_emphasis"]:
            children.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"type": "text", "text": {"content": emp[:1900]}}]
                    },
                }
            )

    children.append(
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "📖 상세 강의 노트"}}]},
        }
    )

    for sec in analysis.get("detailed_notes", [])[:25]:
        ts = f" ({sec['timestamp_hint']})" if sec.get("timestamp_hint") else ""
        children.append(
            {
                "object": "block",
                "type": "heading_3",
                "heading_3": {
                    "rich_text": [{"type": "text", "text": {"content": f"{sec['section_title']}{ts}"[:1900]}}]
                },
            }
        )
        content_text = sec["content"]
        for i in range(0, len(content_text), 1800):
            children.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": content_text[i : i + 1800]}}]
                    },
                }
            )

    page_title = f"[{analysis['course_name']}] {analysis['lecture_title']} ({analysis['recorded_date']})"
    created_url = None

    # Create lecture page in Database or Parent Page
    if lecture_db_id:
        payload = {
            "parent": {"database_id": lecture_db_id},
            "icon": {"type": "emoji", "emoji": "📝"},
            "properties": {
                "Name": {"title": [{"text": {"content": page_title}}]},
            },
            "children": children[:95],
        }
        resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=30)
        if resp.ok:
            created_url = resp.json().get("url")
            print(f"[INFO] Created Notion Lecture DB page: {created_url}")
        else:
            print(f"[WARN] Notion Lecture DB insert failed ({resp.status_code}): {resp.text}")

    elif parent_page_id:
        payload = {
            "parent": {"page_id": parent_page_id},
            "icon": {"type": "emoji", "emoji": "📝"},
            "properties": {
                "title": {"title": [{"text": {"content": page_title}}]},
            },
            "children": children[:95],
        }
        resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=30)
        if resp.ok:
            created_url = resp.json().get("url")
            print(f"[INFO] Created Notion child page: {created_url}")
        else:
            print(f"[WARN] Notion parent page insert failed ({resp.status_code}): {resp.text}")

    # Optional: Also insert individual tasks into a separate Notion Task DB if configured
    if task_db_id and analysis.get("action_items"):
        for item in analysis["action_items"]:
            task_payload = {
                "parent": {"database_id": task_db_id},
                "icon": {"type": "emoji", "emoji": "✅"},
                "properties": {
                    "Name": {
                        "title": [
                            {
                                "text": {
                                    "content": f"[{analysis['course_name']}] {item['title']} (마감: {item['due_date']})"
                                }
                            }
                        ]
                    }
                },
                "children": [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {
                                        "content": f"분류: {item['category']} | 중요도: {item['priority']}\n마감일: {item['due_date']} {item.get('due_time', '23:59')}\n세부내용: {item.get('details', '')}"
                                    },
                                }
                            ]
                        },
                    }
                ],
            }
            t_resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=task_payload, timeout=20)
            if not t_resp.ok:
                print(f"[WARN] Notion Task DB insert failed: {t_resp.text}")

    return created_url


def update_ics_calendar(state: Dict[str, Any]) -> None:
    """
    Generates an iCalendar (.ics) feed at `docs/calendar.ics` containing all extracted action items
    with VALARM notifications (1 day before & 2 hours before).
    Samsung Calendar (via Google Account or URL subscription) automatically syncs this feed and triggers native alarms.
    """
    cal = Calendar()
    cal.add("prodid", "-//LectureRecord AI//Samsung Calendar Feed//KO")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "🎓 강의 과제 & 시험 일정 (AI 자동정리)")
    cal.add("x-wr-timezone", "Asia/Seoul")

    for task in state.get("tasks", []):
        event = Event()
        uid = task.get("id") or hashlib.md5(f"{task['course']}_{task['title']}_{task['due_date']}".encode()).hexdigest()
        event.add("uid", f"{uid}@lecturerecord.ai")

        summary = f"[{task['course']}] {task['title']}"
        event.add("summary", summary)

        due_date_str = task.get("due_date", datetime.now(KST).strftime("%Y-%m-%d"))
        due_time_str = task.get("due_time", "23:59")
        try:
            dt_naive = datetime.strptime(f"{due_date_str} {due_time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            dt_naive = datetime.now() + timedelta(days=3)

        dt_start = KST.localize(dt_naive - timedelta(hours=1))
        dt_end = KST.localize(dt_naive)

        event.add("dtstart", dt_start)
        event.add("dtend", dt_end)
        event.add("dtstamp", datetime.now(pytz.utc))

        desc_lines = [
            f"강좌: {task['course']}",
            f"강의명: {task.get('lecture_title', '')}",
            f"분류: {task.get('category', '과제')} | 중요도: {task.get('priority', '보통')}",
            f"세부사항: {task.get('details', '')}",
        ]
        if task.get("notion_url"):
            desc_lines.append(f"노션 노트: {task['notion_url']}")
        event.add("description", "\n".join(desc_lines))

        # Alarm 1: 1 day before
        alarm_1d = Alarm()
        alarm_1d.add("action", "DISPLAY")
        alarm_1d.add("description", f"⏰ 내일 마감: {summary}")
        alarm_1d.add("trigger", timedelta(days=-1))
        event.add_component(alarm_1d)

        # Alarm 2: 2 hours before
        alarm_2h = Alarm()
        alarm_2h.add("action", "DISPLAY")
        alarm_2h.add("description", f"🚨 2시간 전: {summary}")
        alarm_2h.add("trigger", timedelta(hours=-2))
        event.add_component(alarm_2h)

        cal.add_component(event)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    ICS_FILE.write_bytes(cal.to_ical())
    print(f"[INFO] Updated iCalendar feed: {ICS_FILE.relative_to(ROOT_DIR)} ({len(state.get('tasks', []))} events)")


def push_to_google_calendar(analysis: Dict[str, Any], notion_url: Optional[str]) -> None:
    """
    Optional direct push to Google Calendar API if GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_CALENDAR_ID are configured.
    Since Galaxy Samsung Calendar natively syncs with the user's Google Calendar, events appear immediately on the phone with reminders.
    """
    sa_json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    calendar_id = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
    if not sa_json_str or not calendar_id:
        return

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        sa_info = json.loads(sa_json_str)
        creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        service = build("calendar", "v3", credentials=creds)

        for item in analysis.get("action_items", []):
            due_date_str = item.get("due_date")
            due_time_str = item.get("due_time", "23:59")
            try:
                dt_naive = datetime.strptime(f"{due_date_str} {due_time_str}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue

            dt_end = KST.localize(dt_naive)
            dt_start = dt_end - timedelta(hours=1)

            event_body = {
                "summary": f"[{analysis['course_name']}] {item['title']}",
                "description": (
                    f"분류: {item['category']} | 중요도: {item['priority']}\n"
                    f"세부 내용: {item.get('details', '')}\n"
                    f"강의: {analysis['lecture_title']} ({analysis['recorded_date']})\n"
                    + (f"노션 링크: {notion_url}" if notion_url else "")
                ),
                "start": {"dateTime": dt_start.isoformat(), "timeZone": "Asia/Seoul"},
                "end": {"dateTime": dt_end.isoformat(), "timeZone": "Asia/Seoul"},
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": 24 * 60},
                        {"method": "popup", "minutes": 120},
                    ],
                },
            }
            created = service.events().insert(calendarId=calendar_id, body=event_body).execute()
            print(f"[INFO] Pushed event to Google/Samsung Calendar: {created.get('htmlLink')}")
    except Exception as e:
        print(f"[WARN] Google Calendar direct push failed: {e}")


DEFAULT_GOOGLE_DRIVE_FOLDER_ID = "13fRGpU8zvRwdcdPt7SAk2hsLYiRrh-rE"


def pull_from_google_drive(state: Dict[str, Any]) -> None:
    """
    Pulls new audio recordings uploaded via Galaxy FolderSync from Google Drive into `recordings/`.
    Supports two modes:
    1. Public/Shared Link Mode (Zero-config): Scrapes embeddedfolderview and downloads files directly using file IDs.
    2. Service Account Mode (if GOOGLE_SERVICE_ACCOUNT_JSON is provided): Uses Google Drive API v3.
    """
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip() or DEFAULT_GOOGLE_DRIVE_FOLDER_ID
    if "drive.google.com" in folder_id:
        m = re.search(r"folders/([a-zA-Z0-9_-]+)", folder_id)
        if m:
            folder_id = m.group(1)

    sa_json_str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    downloaded_drive_ids = state.setdefault("processed_drive_ids", {})

    # Mode 1: Service Account API (if configured)
    if sa_json_str:
        try:
            import io
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaIoBaseDownload

            sa_info = json.loads(sa_json_str)
            creds = service_account.Credentials.from_service_account_info(
                sa_info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
            )
            drive_service = build("drive", "v3", credentials=creds)

            def scan_drive_folder_api(parent_id: str, course_prefix: str = "") -> None:
                query = f"'{parent_id}' in parents and trashed = false"
                results = (
                    drive_service.files()
                    .list(q=query, fields="files(id, name, mimeType)", pageSize=100)
                    .execute()
                )
                for item in results.get("files", []):
                    f_id = item["id"]
                    f_name = item["name"]
                    f_mime = item.get("mimeType", "")

                    if f_mime == "application/vnd.google-apps.folder":
                        scan_drive_folder_api(f_id, f_name)
                    else:
                        ext = Path(f_name).suffix.lower()
                        if ext in SUPPORTED_EXTENSIONS and f_id not in downloaded_drive_ids:
                            target_dir = RECORDINGS_DIR / course_prefix if course_prefix else RECORDINGS_DIR
                            target_dir.mkdir(parents=True, exist_ok=True)
                            local_path = target_dir / f_name

                            print(f"[INFO] Downloading new recording from Google Drive API: {f_name}")
                            request = drive_service.files().get_media(fileId=f_id)
                            with io.FileIO(str(local_path), "wb") as fh:
                                downloader = MediaIoBaseDownload(fh, request)
                                done = False
                                while not done:
                                    _, done = downloader.next_chunk()

                            downloaded_drive_ids[f_id] = {
                                "name": f_name,
                                "downloaded_at": datetime.now(KST).isoformat(),
                            }

            scan_drive_folder_api(folder_id)
            return
        except Exception as e:
            print(f"[WARN] Service Account Google Drive sync failed, falling back to public link mode: {e}")

    # Mode 2: Zero-auth Public Shared Folder Mode
    def scan_public_folder(current_folder_id: str, course_prefix: str = "") -> None:
        embed_url = f"https://drive.google.com/embeddedfolderview?id={current_folder_id}#list"
        try:
            resp = requests.get(embed_url, timeout=20)
            if not resp.ok:
                print(f"[WARN] Could not access public Drive folder {current_folder_id}: HTTP {resp.status_code}")
                return
            html = resp.text
        except Exception as e:
            print(f"[WARN] Failed to fetch public Drive folder {current_folder_id}: {e}")
            return

        # Match each flip-entry block
        entry_blocks = re.findall(
            r'<div class="flip-entry" id="entry-([a-zA-Z0-9_-]+)"[^>]*>(.*?)</div></div></div>',
            html,
            re.S,
        )
        if not entry_blocks:
            # Fallback broader pattern
            entry_blocks = re.findall(
                r'id="entry-([a-zA-Z0-9_-]+)".*?<div class="flip-entry-title">([^<]+)</div>',
                html,
                re.S,
            )
            for f_id, title in entry_blocks:
                process_public_entry(f_id, title.strip(), "", course_prefix)
            return

        for f_id, block_html in entry_blocks:
            title_m = re.search(r'<div class="flip-entry-title">([^<]+)</div>', block_html)
            if not title_m:
                continue
            title = title_m.group(1).strip()
            process_public_entry(f_id, title, block_html, course_prefix)

    def process_public_entry(f_id: str, f_name: str, block_html: str, course_prefix: str) -> None:
        if "drive.google.com/drive/folders/" in block_html or (not Path(f_name).suffix and "." not in f_name):
            # It's a subfolder (e.g., course folder)
            scan_public_folder(f_id, f_name)
            return

        ext = Path(f_name).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS or f_id in downloaded_drive_ids:
            return

        target_dir = RECORDINGS_DIR / course_prefix if course_prefix else RECORDINGS_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        local_path = target_dir / f_name

        print(f"[INFO] Downloading new recording from shared Google Drive folder: {f_name} (ID: {f_id})")
        dl_url = f"https://drive.usercontent.google.com/download?id={f_id}&export=download&confirm=t"
        try:
            with requests.get(dl_url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
            downloaded_drive_ids[f_id] = {
                "name": f_name,
                "course": course_prefix,
                "downloaded_at": datetime.now(KST).isoformat(),
            }
            print(f"[INFO] Successfully downloaded {f_name} ({local_path.stat().st_size} bytes)")
        except Exception as e:
            print(f"[WARN] Failed to download file {f_name} ({f_id}): {e}")

    print(f"[INFO] Checking shared Google Drive folder ({folder_id}) for new recordings...")
    scan_public_folder(folder_id)


def find_unprocessed_files(state: Dict[str, Any], target_file: str = "") -> List[Path]:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    if target_file:
        candidate = ROOT_DIR / target_file
        if candidate.exists() and candidate.is_file():
            return [candidate]

    files: List[Path] = []
    for p in sorted(RECORDINGS_DIR.rglob("*")):
        if not p.is_file():
            continue
        if "processed" in p.relative_to(RECORDINGS_DIR).parts:
            continue
        if p.suffix.lower() in SUPPORTED_EXTENSIONS:
            file_hash = compute_sha256(p)
            if file_hash not in state.get("processed_hashes", {}):
                files.append(p)
    return files


def main() -> None:
    state = load_state()
    target_file = os.environ.get("TARGET_FILE_PATH", "").strip()
    override_course = os.environ.get("OVERRIDE_COURSE_NAME", "").strip()

    pull_from_google_drive(state)
    files = find_unprocessed_files(state, target_file)
    if not files:
        print("[INFO] No new unprocessed audio files found in recordings/.")
        update_ics_calendar(state)
        save_state(state)
        return

    print(f"[INFO] Found {len(files)} new recording(s) to analyze.")

    for file_path in files:
        print(f"\n==================== Processing: {file_path.name} ====================")
        file_hash = compute_sha256(file_path)
        meta = parse_course_and_date(file_path, override_course)
        analysis = analyze_audio_with_gemini(file_path, meta)

        md_path = write_markdown_note(analysis)
        notion_url = sync_to_notion(analysis)
        push_to_google_calendar(analysis, notion_url)

        lecture_id = file_hash[:12]
        lecture_entry = {
            "id": lecture_id,
            "course_name": analysis["course_name"],
            "lecture_title": analysis["lecture_title"],
            "recorded_date": analysis["recorded_date"],
            "summary_3lines": analysis["summary_3lines"],
            "exam_and_emphasis": analysis.get("exam_and_emphasis", []),
            "key_terms": analysis.get("key_terms", []),
            "detailed_notes": analysis.get("detailed_notes", []),
            "note_path": str(md_path.relative_to(ROOT_DIR)),
            "notion_url": notion_url,
            "processed_at": datetime.now(KST).isoformat(),
        }
        state["lectures"].insert(0, lecture_entry)

        for idx, item in enumerate(analysis.get("action_items", [])):
            task_entry = {
                "id": f"{lecture_id}_task_{idx}",
                "lecture_id": lecture_id,
                "course": analysis["course_name"],
                "lecture_title": analysis["lecture_title"],
                "title": item["title"],
                "category": item["category"],
                "priority": item["priority"],
                "due_date": item["due_date"],
                "due_time": item.get("due_time", "23:59"),
                "is_exact_date": item.get("is_exact_date", True),
                "details": item.get("details", ""),
                "notion_url": notion_url,
                "completed": False,
            }
            state["tasks"].insert(0, task_entry)

        state["processed_hashes"][file_hash] = {
            "file": str(file_path.relative_to(ROOT_DIR)),
            "processed_at": datetime.now(KST).isoformat(),
            "lecture_id": lecture_id,
        }

        # Move processed recording to recordings/processed/<course>/ to keep folder clean
        dest_dir = PROCESSED_DIR / re.sub(r'[\\/*?:"<>|]', "_", analysis["course_name"]).strip()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / file_path.name
        shutil.move(str(file_path), str(dest_path))
        print(f"[INFO] Moved processed audio to {dest_path.relative_to(ROOT_DIR)}")

    update_ics_calendar(state)
    save_state(state)
    print("\n[SUCCESS] All recordings processed and synced!")


if __name__ == "__main__":
    main()
