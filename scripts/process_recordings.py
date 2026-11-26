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


# 2026년 2학기 공식 시간표 강좌 목록 (고신대학교 작업치료학과 3학년)
OFFICIAL_COURSES = [
    "감각과인지재활",
    "명작의 탄생",
    "삼킴재활",
    "일상생활활동",
    "아동운동재활학",
    "장애아동진단 및 평가",
    "지역사회재활",
    "사례기반 정신사회작업치료학",
]

COURSE_ALIASES = {
    "adl": "일상생활활동",
    "일상생활": "일상생활활동",
    "일상생활활동": "일상생활활동",
    "감인": "감각과인지재활",
    "감각": "감각과인지재활",
    "인지": "감각과인지재활",
    "감각과인지": "감각과인지재활",
    "감각과인지재활": "감각과인지재활",
    "명작": "명작의 탄생",
    "명작의탄생": "명작의 탄생",
    "명작의 탄생": "명작의 탄생",
    "삼킴": "삼킴재활",
    "연하": "삼킴재활",
    "삼킴재활": "삼킴재활",
    "아동운동": "아동운동재활학",
    "아동운동재활": "아동운동재활학",
    "아동운동재활학": "아동운동재활학",
    "장애아동": "장애아동진단 및 평가",
    "아동진단": "장애아동진단 및 평가",
    "아진": "장애아동진단 및 평가",
    "진단및평가": "장애아동진단 및 평가",
    "장애아동진단": "장애아동진단 및 평가",
    "장애아동진단 및 평가": "장애아동진단 및 평가",
    "지역사회": "지역사회재활",
    "지사재": "지역사회재활",
    "지역사회재활": "지역사회재활",
    "사례기반": "사례기반 정신사회작업치료학",
    "정신사회": "사례기반 정신사회작업치료학",
    "정신": "사례기반 정신사회작업치료학",
    "사례기반 정신사회작업치료학": "사례기반 정신사회작업치료학",
}


def normalize_course_name(raw_course: str) -> str:
    if not raw_course:
        return "미분류 강좌"
    cleaned = raw_course.strip()
    key = re.sub(r"\s+", "", cleaned).lower()
    # Check exact or longest alias match first
    for alias_key, official in sorted(COURSE_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        alias_norm = re.sub(r"\s+", "", alias_key).lower()
        if key == alias_norm or key.startswith(alias_norm) or alias_norm in key:
            return official
    return cleaned


def parse_course_and_date(file_path: Path, override_course: str = "") -> Dict[str, str]:
    """
    Infers course name and recording date from directory structure, filename prefix (e.g. ADL -> 일상생활활동, 아진/아동 진단 -> 장애아동진단 및 평가),
    or timetable schedule.
    """
    rel_parts = file_path.relative_to(RECORDINGS_DIR).parts
    course_from_dir = rel_parts[0] if len(rel_parts) > 1 and rel_parts[0] != "processed" else ""

    stem = file_path.stem
    course = override_course.strip() or course_from_dir

    # Try extracting date from filename (YYYY-MM-DD, YYYYMMDD, YYMMDD) first
    now_kst = datetime.now(KST)
    recorded_date = now_kst.strftime("%Y-%m-%d")

    date_match_4 = re.search(r"(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])", stem)
    if date_match_4:
        recorded_date = f"{date_match_4.group(1)}-{date_match_4.group(2)}-{date_match_4.group(3)}"
        # Remove leading date prefix if present (e.g., "2026-11-16_ADL 10")
        stem_without_date = re.sub(r"^20\d{2}[-_.]?\d{2}[-_.]?\d{2}[-_\s]*", "", stem).strip()
        if stem_without_date:
            stem = stem_without_date
    else:
        date_match_2 = re.search(r"(?<!\d)(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)", stem)
        if date_match_2:
            recorded_date = f"20{date_match_2.group(1)}-{date_match_2.group(2)}-{date_match_2.group(3)}"

    # Try prefix pattern: [Course] Title, Course_Date, or "ADL 10", "아동 진단 5", "삼킴1" (Course Name + number)
    if not course:
        bracket_match = re.match(r"^\[([^\]]+)\]\s*(.*)$", stem)
        if bracket_match:
            course = bracket_match.group(1).strip()
            stem = bracket_match.group(2).strip() or stem
        else:
            prefix_num_match = re.match(r"^([A-Za-z가-힣\s]+?)\s*[-_.]?\s*(\d+.*)$", stem)
            if prefix_num_match:
                first_word = prefix_num_match.group(1).strip()
                if not re.match(r"^(음성|녹음|Voice|Record|강의)$", first_word, re.IGNORECASE):
                    course = first_word
            elif "_" in stem:
                first_part = stem.split("_")[0].strip()
                if not re.match(r"^(음성|녹음|Voice|Record|\d+)$", first_part, re.IGNORECASE):
                    course = first_part

    course = normalize_course_name(course)

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

    courses_list_str = ", ".join(OFFICIAL_COURSES)
    prompt = f"""
당신은 고신대학교 작업치료학과 3학년 강의를 완벽하게 복기하고 전공 서적·아티클 수준으로 깊이 있게 정리하는 AI 튜터입니다.

[2026년 2학기 수강 과목 목록 (반드시 아래 과목명 중 하나로 분류하세요)]
{courses_list_str}
(참고: ADL = 일상생활활동, 연하/삼킴 = 삼킴재활, 지사재 = 지역사회재활, 정신사회 = 사례기반 정신사회작업치료학)

[기본 메타데이터]
- 파일명: {file_path.name}
- 폴더/파일 기반 추정 강좌명: {meta['course']}
- 녹음 기준 날짜: {meta['date']} (상대적 날짜 계산 시 이 날짜를 기준으로 YYYY-MM-DD를 계산하세요. 예: '다음 주 수요일까지', '내일까지')

[작성 원칙 — 매우 중요]
- **지나치게 짧게 요약하지 마세요!** 학생이 나중에 시간 내서 천천히 읽으며 수업 전체 흐름과 교수님의 설명, 임상 예시를 생생하게 리마인드(복기)할 수 있도록 **충분히 풍부한 분량과 서술형 문장**으로 자세히 풀어쓰세요.
- 강의에서 다룬 주요 개념들은 단순 단어 뜻풀이에 그치지 말고, 노트 맨 아래에 **별도의 심화 학습 아티클(블로그 포스트/전공 칼럼처럼 술술 읽히는 긴 글 형태)** 로 깊이 있게 작성해주세요.

오디오 전체를 주의 깊게 듣고 다음 JSON 스키마에 맞춰 한국어로 상세하고 풍부하게 응답하세요:
1. `course_name`: 위 [2026년 2학기 수강 과목 목록] 중 가장 일치하는 공식 과목명을 정확히 선택하세요.
2. `lecture_title`: 오늘 강의의 핵심 주제를 담은 명확한 제목 (파일명에 주차나 번호가 있다면 포함, 예: "10주차 - ...")
3. `summary_3lines`: 오늘 수업의 전체 맥락과 핵심 흐름을 1~2문단으로 자연스럽게 정리한 도입 개요
4. `detailed_notes`: 강의 시간 흐름에 따른 섹션별 상세 강의록 (배열: 각 항목은 `section_title`, `timestamp_hint`, `content`).
   - `content`는 개조식 단답형이 아니라, **교수님이 수업 중에 설명하신 원리, 배경, 임상 사례, 환자 적용 예시, 비유 등을 빠짐없이 담아서 읽을거리로서 충분한 분량(섹션당 최소 2~4문단 이상)으로 상세히 서술**하세요.
5. `exam_and_emphasis`: 교수님/강사가 "시험에 나온다", "중요하다", "국가시험(국시) 빈출이다", "꼭 기억해라"라고 강조한 포인트와 그 이유/맥락 (배열)
6. `key_terms`: 핵심 전공 용어 및 상세 정의 (배열: 각 항목은 `term`, `definition`)
7. `concept_articles`: **강의에서 나온 꼭 공부해야 할 핵심 개념들을 선정하여 맨 밑에 읽을 수 있도록 작성한 심화 아티클 시리즈** (배열, 최소 4~6개 핵심 주제).
   - **분량 요구사항**: 아티클 전체 합산 분량이 **최소 A4 용지 2~3페이지 이상(공백 포함 4,000~7,000자 이상)** 이 되도록 절대 축약하지 말고 전공 교과서·임상 리뷰 논문 수준으로 깊이 있게 풀어서 작성하세요.
   - 각 항목은:
     - `article_title`: 아티클 제목 (예: "📖 [심화 아티클 1] 작업치료에서 이동(Transfer) 훈련의 생체역학적 원리와 임상 중재 프로토콜")
     - `why_important`: 이 개념이 임상 실무 및 작업치료사 국가고시/전공 시험에서 왜 중요한지 핵심 포인트
     - `article_body`: 비전공자나 복습하는 학생이 이 글만 읽어도 개념을 100% 완벽히 이해할 수 있도록 ① 개념의 이론적 배경과 정의 → ② 세부 생리학적/해부학적/임상적 기전 및 단계별 분류 → ③ 실제 환자 사례(Case)에서의 평가 도구 및 작업치료 중재 전략 → ④ 임상 주의사항(금기증) 및 국가고시 시험 대비 핵심 암기 팁까지 **각 아티클당 최소 4~7개의 긴 문단으로 구성된 완결성 있는 장문의 아티클(Article)** 로 작성하세요.
8. `action_items`: 학생이 해야 할 일, 과제, 실습 준비, 퀴즈, 시험 일정, 복습할 범위 등 (배열).
   - 각 항목은:
     - `title`: 해야 할 일 제목 (예: "[일상생활활동] 10주차 과제 제출")
     - `category`: "과제" | "시험" | "퀴즈" | "복습/예습" | "기타" 중 택1
     - `priority`: "높음" | "보통" | "낮음"
     - `due_date`: 마감일/일정 날짜 (반드시 `YYYY-MM-DD` 형식. 강의에서 언급이 없으면 녹음일로부터 3일 뒤 날짜로 기본 지정하되 `is_exact_date`를 false로 설정)
     - `due_time`: 마감 시간 (`HH:MM` 24시간 형식, 언급 없으면 `"23:59"` 또는 시험인 경우 `"09:00"`)
     - `is_exact_date`: 강의 중 명시적인 마감일/시험일이 언급되었으면 `true`, 추천 복습 일정이면 `false`
     - `details`: 세부 요구사항 (제출 방식, 분량, 주의사항 등)
9. `transcript_excerpt`: 강의 주요 발언 및 핵심 흐름 녹취록 (시간 흐름순으로 상세히 정리)
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
            "concept_articles": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "article_title": {"type": "STRING"},
                        "why_important": {"type": "STRING"},
                        "article_body": {"type": "STRING"},
                    },
                    "required": ["article_title", "why_important", "article_body"],
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
            "concept_articles",
            "action_items",
            "transcript_excerpt",
        ],
    }

    candidate_models = [
        "gemini-3.8-flash",
        "gemini-3.8-flash-lite",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.8-pro",
        "gemini-3.5-pro",
    ]
    try:
        for m in client.models.list():
            m_name = getattr(m, "name", "")
            if m_name.startswith("models/"):
                m_name = m_name[len("models/") :]
            if "gemini-3" in m_name and ("flash" in m_name or "pro" in m_name) and "tts" not in m_name and "image" not in m_name:
                if m_name not in candidate_models:
                    candidate_models.append(m_name)
        print(f"[INFO] Available Gemini 3 candidate models: {candidate_models}")
    except Exception as e:
        print(f"[WARN] Could not list models dynamically: {e}")

    result = None
    last_err = None
    try:
        for attempt in range(1, 4):
            for model_name in candidate_models:
                try:
                    print(f"[INFO] Running {model_name} audio analysis (attempt {attempt})...")
                    response = client.models.generate_content(
                        model=model_name,
                        contents=[uploaded_file, prompt],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=response_schema,
                            temperature=0.3,
                        ),
                    )
                    result = json.loads(response.text)
                    break
                except Exception as err:
                    last_err = err
                    err_str = str(err)
                    print(f"[WARN] Model {model_name} failed: {err_str[:220]}")
                    if "404" in err_str or "NOT_FOUND" in err_str:
                        continue
                    time.sleep(4)
            if result is not None:
                break
            print(f"[INFO] Waiting 10s before retry round {attempt + 1}...")
            time.sleep(10)

        if result is None:
            raise RuntimeError(f"All Gemini models failed. Last error: {last_err}")
    finally:
        try:
            client.files.delete(name=uploaded_file.name)
        except Exception:
            pass

    if meta["course"] != "미분류 강좌":
        result["course_name"] = meta["course"]
    result["course_name"] = normalize_course_name(result.get("course_name", ""))
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
        f"# 📅 {date_str} | [{analysis['course_name']}] {analysis['lecture_title']}",
        "",
        f"- **녹음 일자**: {date_str}",
        f"- **원본 파일**: `{analysis['source_file']}`",
        "",
        "## 📌 강의 전체 맥락 & 핵심 개요",
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

    lines.extend(["", "## ⭐ 교수님 강조 & 국가고시/시험 출제 포인트"])
    if analysis.get("exam_and_emphasis"):
        for emp in analysis["exam_and_emphasis"]:
            lines.append(f"- 🔥 {emp}")
    else:
        lines.append("- 별도 시험 강조 사항 없음")

    lines.extend(["", "## 📖 리마인드를 위한 섹션별 상세 강의록"])
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

    if analysis.get("concept_articles"):
        lines.extend(["", "---", "", "## 📚 공부해야 할 핵심 개념 심화 아티클 (Study Articles)"])
        for art in analysis["concept_articles"]:
            lines.append(f"### {art['article_title']}")
            lines.append(f"> 💡 **핵심 중요도**: {art['why_important']}")
            lines.append("")
            lines.append(art["article_body"])
            lines.append("")

    lines.extend(["", "## 🎙️ 주요 녹취록 흐름", analysis.get("transcript_excerpt", "")])

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[INFO] Saved Markdown lecture note: {md_path.relative_to(ROOT_DIR)}")
    return md_path


def ensure_notion_course_folders() -> Dict[str, str]:
    """
    Ensures that all OFFICIAL_COURSES have a dedicated folder sub-page inside NOTION_PARENT_PAGE_ID.
    Returns a dictionary mapping course_name -> notion_page_id.
    """
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        return {}

    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip() or "424c5c90104580a8a342d929114d2402"
    if "notion." in parent_page_id:
        m = re.search(r"([0-9a-fA-F]{32})", parent_page_id.replace("-", ""))
        if m:
            parent_page_id = m.group(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    course_map: Dict[str, str] = {}
    try:
        blocks_resp = requests.get(
            f"https://api.notion.com/v1/blocks/{parent_page_id}/children?page_size=100",
            headers=headers,
            timeout=20,
        )
        existing_subpages: Dict[str, str] = {}
        if blocks_resp.ok:
            for blk in blocks_resp.json().get("results", []):
                if blk.get("type") == "child_page":
                    cp_title = blk.get("child_page", {}).get("title", "")
                    existing_subpages[cp_title] = blk["id"]

        for course in OFFICIAL_COURSES:
            matched_id = None
            for title, pid in existing_subpages.items():
                if course.lower() in title.lower():
                    matched_id = pid
                    break
            if matched_id:
                course_map[course] = matched_id
            else:
                folder_title = f"[{course}]"
                folder_payload = {
                    "parent": {"page_id": parent_page_id},
                    "icon": {"type": "emoji", "emoji": "📂"},
                    "properties": {
                        "title": {"title": [{"text": {"content": folder_title}}]},
                    },
                }
                f_resp = requests.post(
                    "https://api.notion.com/v1/pages", headers=headers, json=folder_payload, timeout=20
                )
                if f_resp.ok:
                    course_map[course] = f_resp.json()["id"]
                    print(f"[INFO] Pre-created Notion course folder: {folder_title}")
    except Exception as e:
        print(f"[WARN] ensure_notion_course_folders error: {e}")

    return course_map


def append_paragraph_chunks(blocks_list: List[Dict[str, Any]], text: str) -> None:
    """Splits long multi-paragraph text into Notion paragraph blocks respecting the 2000-char limit."""
    for para in text.split("\n\n"):
        cleaned = para.strip()
        if not cleaned:
            continue
        for i in range(0, len(cleaned), 1800):
            blocks_list.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": cleaned[i : i + 1800]}}]
                    },
                }
            )


def sync_to_notion(analysis: Dict[str, Any]) -> Optional[str]:
    """
    Syncs the analyzed lecture note, detailed reading notes, and bottom concept study articles to Notion.
    Supports multi-batch block appending when total blocks exceed Notion's 100-block per request limit.
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

    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    rec_date_str = analysis["recorded_date"]
    try:
        dt_obj = datetime.strptime(rec_date_str, "%Y-%m-%d")
        date_with_day = f"{rec_date_str} ({weekday_names[dt_obj.weekday()]}요일)"
    except ValueError:
        date_with_day = rec_date_str

    # Build rich children blocks for the lecture page (with prominent Date header at top)
    children: List[Dict[str, Any]] = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "📅"},
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": (
                                f"📅 강의 녹음 일자: {date_with_day}\n"
                                f"🎓 강좌명: {analysis['course_name']} | 원본 파일: {Path(analysis.get('source_file', '')).name}\n\n"
                                f"📌 강의 전체 개요:\n{analysis['summary_3lines'][:1600]}"
                            )
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
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": "⭐ 교수님 강조 & 국가고시/시험 포인트"}}]},
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
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "📖 리마인드를 위한 섹션별 상세 강의록"}}]},
        }
    )

    for sec in analysis.get("detailed_notes", []):
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
        append_paragraph_chunks(children, sec["content"])

    if analysis.get("key_terms"):
        children.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": "🔑 주요 전공 용어 체크"}}]},
            }
        )
        for kt in analysis["key_terms"]:
            children.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [
                            {"type": "text", "text": {"content": f"{kt['term']}: {kt['definition']}"[:1900]}}
                        ]
                    },
                }
            )

    # Format title concisely for mobile screens: "MM/DD N주차 제목" (e.g., "10/26 1주차 뇌졸중 환자의 ADL")
    try:
        dt_mmdd = datetime.strptime(rec_date_str, "%Y-%m-%d").strftime("%m/%d")
    except ValueError:
        dt_mmdd = rec_date_str[-5:].replace("-", "/")

    raw_file_stem = Path(analysis.get("source_file", "")).stem
    raw_file_stem = re.sub(r"^20\d{2}[-_.]?\d{2}[-_.]?\d{2}[-_\s]*", "", raw_file_stem).strip()
    num_m = re.search(r"(\d+)", raw_file_stem)
    file_num = num_m.group(1) if num_m else ""

    clean_title = analysis["lecture_title"].strip()
    # Strip leading [과목명] or date if Gemini included it
    clean_title = re.sub(r"^\[[^\]]+\]\s*", "", clean_title)
    clean_title = re.sub(r"^\d{2,4}년\s*\d{1,2}월\s*\d{1,2}일\s*", "", clean_title)

    # Extract or normalize "N주차" at the start of clean_title
    week_m = re.match(r"^(\d+)\s*(?:주차|강)\s*[-—:]?\s*(.*)$", clean_title)
    if week_m:
        week_tag = f"{week_m.group(1)}주차"
        subject_body = week_m.group(2).strip()
    elif file_num:
        week_tag = f"{file_num}주차"
        subject_body = clean_title
    else:
        week_tag = ""
        subject_body = clean_title

    if week_tag:
        page_title = f"{dt_mmdd} {week_tag} {subject_body}".strip()
    else:
        page_title = f"{dt_mmdd} {subject_body}".strip()

    created_url = None
    created_page_id = None

    # Create lecture page in Database or Parent Page with first 95 blocks
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
            created_page_id = resp.json().get("id")
            print(f"[INFO] Created Notion Lecture DB page: {created_url}")
        else:
            print(f"[WARN] Notion Lecture DB insert failed ({resp.status_code}): {resp.text}")

    elif parent_page_id:
        course_folder_page_id = parent_page_id
        course_folder_title = f"[{analysis['course_name']}]"
        try:
            blocks_resp = requests.get(
                f"https://api.notion.com/v1/blocks/{parent_page_id}/children?page_size=100",
                headers=headers,
                timeout=20,
            )
            if blocks_resp.ok:
                found_subpage = None
                for blk in blocks_resp.json().get("results", []):
                    if blk.get("type") == "child_page":
                        cp_title = blk.get("child_page", {}).get("title", "")
                        if analysis["course_name"].lower() in cp_title.lower():
                            found_subpage = blk["id"]
                            break
                if found_subpage:
                    course_folder_page_id = found_subpage
                else:
                    folder_payload = {
                        "parent": {"page_id": parent_page_id},
                        "icon": {"type": "emoji", "emoji": "📂"},
                        "properties": {
                            "title": {"title": [{"text": {"content": course_folder_title}}]},
                        },
                    }
                    f_resp = requests.post(
                        "https://api.notion.com/v1/pages", headers=headers, json=folder_payload, timeout=20
                    )
                    if f_resp.ok:
                        course_folder_page_id = f_resp.json()["id"]
                        print(f"[INFO] Created Notion course folder page: {course_folder_title}")
        except Exception as e:
            print(f"[WARN] Could not resolve course subfolder in Notion, using root page: {e}")

        payload = {
            "parent": {"page_id": course_folder_page_id},
            "icon": {"type": "emoji", "emoji": "📝"},
            "properties": {
                "title": {"title": [{"text": {"content": page_title}}]},
            },
            "children": children[:95],
        }
        resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=30)
        if resp.ok:
            created_url = resp.json().get("url")
            created_page_id = resp.json().get("id")
            print(f"[INFO] Created Notion child page inside course folder: {created_url}")
        else:
            print(f"[WARN] Notion parent page insert failed ({resp.status_code}): {resp.text}")

    # Append remaining blocks in batches of 90 if the long-form lecture notes exceed 95 blocks
    if created_page_id and len(children) > 95:
        for offset in range(95, len(children), 90):
            batch = children[offset : offset + 90]
            app_resp = requests.patch(
                f"https://api.notion.com/v1/blocks/{created_page_id}/children",
                headers=headers,
                json={"children": batch},
                timeout=30,
            )
            if not app_resp.ok:
                print(f"[WARN] Notion block append failed at offset {offset}: {app_resp.text}")

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

        # Split by each flip-entry block
        parts = html.split('<div class="flip-entry"')
        for part in parts[1:]:
            id_m = re.search(r'id="entry-([a-zA-Z0-9_-]+)"', part)
            title_m = re.search(r'<div class="flip-entry-title">([^<]+)</div>', part)
            if id_m and title_m:
                process_public_entry(id_m.group(1), title_m.group(1).strip(), part, course_prefix)

    def parse_drive_mod_date(block_html: str) -> str:
        mod_m = re.search(r'<div class="flip-entry-last-modified"><div>([^<]+)</div>', block_html)
        if not mod_m:
            return datetime.now(KST).strftime("%Y-%m-%d")
        raw_mod = mod_m.group(1).strip()
        now_kst = datetime.now(KST)
        # Format like "Nov 16", "Oct 26"
        for fmt in ["%b %d", "%B %d"]:
            try:
                parsed = datetime.strptime(raw_mod, fmt)
                return f"{now_kst.year}-{parsed.month:02d}-{parsed.day:02d}"
            except ValueError:
                pass
        # Format like "2026. 11. 16." or "11/16/26"
        m_ymd = re.search(r"(\d{2,4})[./\s-]+(\d{1,2})[./\s-]+(\d{1,2})", raw_mod)
        if m_ymd:
            g1, g2, g3 = m_ymd.group(1), int(m_ymd.group(2)), int(m_ymd.group(3))
            if len(g1) == 4:
                y, mo, d = int(g1), g2, g3  # 2026. 11. 16.
            else:
                mo, d, y = int(g1), g2, g3  # 11/16/26 (M/D/YY)
            if y < 100:
                y += 2000
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
        return now_kst.strftime("%Y-%m-%d")

    def process_public_entry(f_id: str, f_name: str, block_html: str, course_prefix: str) -> None:
        if "drive.google.com/drive/folders/" in block_html or (not Path(f_name).suffix and "." not in f_name):
            # It's a subfolder (e.g., course folder)
            scan_public_folder(f_id, f_name)
            return

        ext = Path(f_name).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS or f_id in downloaded_drive_ids:
            return

        try:
            mod_date_str = parse_drive_mod_date(block_html)
        except Exception as e:
            print(f"[WARN] Could not parse modified date for {f_name}, using today: {e}")
            mod_date_str = datetime.now(KST).strftime("%Y-%m-%d")
        local_filename = f_name
        if not re.search(r"20\d{2}[-_.]?\d{2}[-_.]?\d{2}", f_name):
            local_filename = f"{mod_date_str}_{f_name}"

        target_dir = RECORDINGS_DIR / course_prefix if course_prefix else RECORDINGS_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        local_path = target_dir / local_filename

        print(f"[INFO] Downloading new recording from shared Google Drive folder: {f_name} (Date: {mod_date_str}, ID: {f_id})")
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
                "recorded_date": mod_date_str,
                "course": course_prefix,
                "downloaded_at": datetime.now(KST).isoformat(),
            }
            print(f"[INFO] Successfully downloaded {local_filename} ({local_path.stat().st_size} bytes)")
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


def ensure_local_course_folders() -> None:
    for course in OFFICIAL_COURSES:
        safe_c = re.sub(r'[\\/*?:"<>|]', "_", course).strip()
        c_rec_dir = RECORDINGS_DIR / safe_c
        c_note_dir = NOTES_DIR / safe_c
        c_rec_dir.mkdir(parents=True, exist_ok=True)
        c_note_dir.mkdir(parents=True, exist_ok=True)
        gitkeep_rec = c_rec_dir / ".gitkeep"
        gitkeep_note = c_note_dir / ".gitkeep"
        if not gitkeep_rec.exists():
            gitkeep_rec.write_text(f"# {course} 강의 녹음 폴더\n", encoding="utf-8")
        if not gitkeep_note.exists():
            gitkeep_note.write_text(f"# {course} AI 강의 노트 폴더\n", encoding="utf-8")


def commit_and_push_progress(msg: str) -> None:
    """Commits and pushes state/notes incrementally inside GitHub Actions so progress is saved immediately."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        import subprocess

        subprocess.run(["git", "config", "user.name", "Lecture AI Bot"], check=False)
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=False)
        subprocess.run(["git", "add", "notes/", "docs/", "recordings/"], check=False)
        diff_res = subprocess.run(["git", "diff", "--staged", "--quiet"], check=False)
        if diff_res.returncode != 0:
            subprocess.run(["git", "commit", "-m", f"{msg} [skip ci]"], check=False)
            subprocess.run(["git", "pull", "--rebase", "-X", "ours", "origin", "main"], check=False)
            subprocess.run(["git", "push"], check=False)
            print(f"[INFO] Incrementally committed & pushed progress: {msg}")
    except Exception as e:
        print(f"[WARN] Incremental git push skipped: {e}")


def main() -> None:
    state = load_state()
    target_file = os.environ.get("TARGET_FILE_PATH", "").strip()
    override_course = os.environ.get("OVERRIDE_COURSE_NAME", "").strip()

    ensure_local_course_folders()
    ensure_notion_course_folders()
    cleanup_and_rename_existing_notion_pages()
    pull_from_google_drive(state)
    files = find_unprocessed_files(state, target_file)
    if not files:
        print("[INFO] No new unprocessed audio files found in recordings/.")
        cleanup_and_rename_existing_notion_pages()
        update_ics_calendar(state)
        save_state(state)
        return

    print(f"[INFO] Found {len(files)} new recording(s) to analyze.")
    newly_processed_dates = set()

    for idx_file, file_path in enumerate(files, 1):
        print(f"\n==================== Processing [{idx_file}/{len(files)}]: {file_path.name} ====================")
        try:
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
                "source_file": analysis.get("source_file", file_path.name),
                "summary_3lines": analysis["summary_3lines"],
                "exam_and_emphasis": analysis.get("exam_and_emphasis", []),
                "key_terms": analysis.get("key_terms", []),
                "detailed_notes": analysis.get("detailed_notes", []),
                "concept_articles": analysis.get("concept_articles", []),
                "note_path": str(md_path.relative_to(ROOT_DIR)),
                "notion_url": notion_url,
                "processed_at": datetime.now(KST).isoformat(),
            }
            state["lectures"].insert(0, lecture_entry)
            newly_processed_dates.add((analysis["course_name"], analysis["recorded_date"]))

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
            cleanup_and_rename_existing_notion_pages()
            if idx_file % 3 == 0:
                commit_and_push_progress(f"🤖 Auto-save progress ({idx_file}/{len(files)} lectures)")
        except Exception as e:
            print(f"[ERROR] Failed to process {file_path.name}: {e}")

    # Create or update the separate standalone Daily Core Study Article page for each date+course
    for c_name, r_date in sorted(newly_processed_dates):
        same_day_lectures = [
            lec
            for lec in state.get("lectures", [])
            if lec.get("course_name") == c_name and lec.get("recorded_date") == r_date
        ]
        if same_day_lectures:
            sync_daily_consolidated_article(c_name, r_date, same_day_lectures)

    cleanup_and_rename_existing_notion_pages()
    update_ics_calendar(state)
    save_state(state)
    commit_and_push_progress("🤖 Auto-analyze lecture recordings & update calendar/notes")
    print("\n[SUCCESS] Batch processing finished!")


def sync_daily_consolidated_article(course_name: str, recorded_date: str, lectures: List[Dict[str, Any]]) -> None:
    """
    Creates a separate, standalone Notion note specifically for that day's in-depth Concept Study Articles:
    e.g. `📚 26년 11월 16일 핵심 심화 아티클 (강의10, 11, 12 종합)`
    alongside the individual lecture notes (`📝 26년 11월 16일 강의10 — ...`).
    """
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        return

    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip() or "424c5c90104580a8a342d929114d2402"
    if "notion." in parent_page_id:
        m = re.search(r"([0-9a-fA-F]{32})", parent_page_id.replace("-", ""))
        if m:
            parent_page_id = m.group(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    try:
        dt_obj = datetime.strptime(recorded_date, "%Y-%m-%d")
        date_with_day = f"{recorded_date} ({weekday_names[dt_obj.weekday()]}요일)"
        dt_short = dt_obj.strftime("%y년 %m월 %d일")
    except ValueError:
        date_with_day = recorded_date
        dt_short = recorded_date

    # Locate course folder page
    course_folder_page_id = parent_page_id
    try:
        blocks_resp = requests.get(
            f"https://api.notion.com/v1/blocks/{parent_page_id}/children?page_size=100",
            headers=headers,
            timeout=20,
        )
        if blocks_resp.ok:
            for blk in blocks_resp.json().get("results", []):
                if blk.get("type") == "child_page":
                    cp_title = blk.get("child_page", {}).get("title", "")
                    if course_name.lower() in cp_title.lower():
                        course_folder_page_id = blk["id"]
                        break
    except Exception:
        pass

    ordered_lectures = list(reversed(lectures))
    lec_nums = []
    for idx, lec in enumerate(ordered_lectures, 1):
        stem = Path(lec.get("source_file", "")).stem
        stem = re.sub(r"^20\d{2}[-_.]?\d{2}[-_.]?\d{2}[-_\s]*", "", stem).strip()
        m_num = re.search(r"(\d+)", stem)
        lec_nums.append(m_num.group(1) if m_num else str(idx))
    lec_nums_str = ", ".join(lec_nums)
    titles_joined = " / ".join(lec["lecture_title"] for lec in ordered_lectures)

    children: List[Dict[str, Any]] = [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "📚"},
                "rich_text": [
                    {
                        "type": "text",
                        "text": {
                            "content": (
                                f"📅 강의 일자: {date_with_day} ({dt_short} 강의 {lec_nums_str} 핵심 개념 총정리 아티클)\n"
                                f"🎓 강좌명: {course_name}\n"
                                f"📖 취합된 강의 범위: {titles_joined[:1400]}\n\n"
                                f"이 노트는 오늘 진행된 모든 강의에서 반드시 공부하고 이해해야 할 핵심 전공 개념들을 "
                                f"A4 2~4페이지 이상의 심화 아티클 형태로 따로 분리하여 깊이 있게 정리한 학습 전용 노트입니다."
                            )
                        },
                    }
                ],
            },
        },
    ]

    art_count = 0
    for idx, lec in enumerate(ordered_lectures, 1):
        children.append({"object": "block", "type": "divider", "divider": {}})
        children.append(
            {
                "object": "block",
                "type": "heading_1",
                "heading_1": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {"content": f"📘 [{lec['lecture_title']}] 핵심 개념 심화 아티클"[:1900]},
                        }
                    ]
                },
            }
        )
        for art in lec.get("concept_articles", []):
            art_count += 1
            children.append(
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": f"Article {art_count}. {art['article_title']}"[:1900]},
                            }
                        ]
                    },
                }
            )
            children.append(
                {
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "icon": {"type": "emoji", "emoji": "💡"},
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": f"핵심 학습 & 국가고시 포인트: {art['why_important']}"[:1800]},
                            }
                        ],
                    },
                }
            )
            append_paragraph_chunks(children, art.get("article_body", ""))

    try:
        dt_mmdd = datetime.strptime(recorded_date, "%Y-%m-%d").strftime("%m/%d")
    except ValueError:
        dt_mmdd = recorded_date[-5:].replace("-", "/")

    page_title = f"{dt_mmdd} 핵심 아티클 ({lec_nums_str}주차)"
    payload = {
        "parent": {"page_id": course_folder_page_id},
        "icon": {"type": "emoji", "emoji": "📚"},
        "properties": {
            "title": {"title": [{"text": {"content": page_title}}]},
        },
        "children": children[:95],
    }
    resp = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload, timeout=30)
    if resp.ok:
        created_page_id = resp.json().get("id")
        print(f"[INFO] Created Separate Daily Core Study Article in Notion: {resp.json().get('url')}")
        if created_page_id and len(children) > 95:
            for offset in range(95, len(children), 90):
                batch = children[offset : offset + 90]
                requests.patch(
                    f"https://api.notion.com/v1/blocks/{created_page_id}/children",
                    headers=headers,
                    json={"children": batch},
                    timeout=30,
                )


def format_concise_notion_title(old_title: str) -> Optional[str]:
    """
    Converts verbose legacy titles like:
    - `📝 26년 10월 26일 강의1 — 뇌졸중 환자의 일상생활활동` -> `10/26 1주차 뇌졸중 환자의 일상생활활동`
    - `📝 26년 10월 26일 강의2 — 10주차 - 상하지 ...` -> `10/26 2주차 상하지 ...`
    - `[일상생활활동] 11강 - 류마티스 관절염(RA)과 ...` -> `11/16 11주차 류마티스 관절염(RA)과 ...`
    - `📚 26년 11월 16일 핵심 심화 아티클 (강의 10, 11, 12 통합)` -> `11/16 핵심 아티클 (10, 11, 12주차)`
    """
    t = old_title.strip()

    # Case 1: Daily Core Article
    m_art = re.match(r"^[📚📰\s]*(\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일\s*핵심.*?\((?:강의\s*)?([^)]+?)(?:\s*통합|\s*종합)?\)$", t)
    if m_art:
        mm = int(m_art.group(2))
        dd = int(m_art.group(3))
        nums = m_art.group(4).strip()
        return f"{mm:02d}/{dd:02d} 핵심 아티클 ({nums}주차)"

    # Case 2: "📝 26년 10월 26일 강의2 — 10주차 - 제목"
    m_new = re.match(r"^[📝📅\s]*\d{2,4}년\s*(\d{1,2})월\s*(\d{1,2})일\s*강의(\d+)?\s*[-—|:]\s*(.*)$", t)
    if m_new:
        mm = int(m_new.group(1))
        dd = int(m_new.group(2))
        lec_num = m_new.group(3) or ""
        rest = m_new.group(4).strip()
        # Strip duplicate "10주차 -" if we already have lec_num, or use it if lec_num is missing
        m_wk = re.match(r"^(\d+)\s*(?:주차|강)\s*[-—:]?\s*(.*)$", rest)
        if m_wk:
            if not lec_num:
                lec_num = m_wk.group(1)
            rest = m_wk.group(2).strip()
        week_str = f"{lec_num}주차 " if lec_num else ""
        return f"{mm:02d}/{dd:02d} {week_str}{rest}".strip()

    # Case 3: "📅 2026-11-16 | [일상생활활동] 10주차 - 제목"
    m_iso = re.match(r"^[📝📅\s]*20\d{2}-(\d{2})-(\d{2})\s*\|\s*\[[^\]]+\]\s*(.*)$", t)
    if m_iso:
        mm, dd, rest = m_iso.group(1), m_iso.group(2), m_iso.group(3).strip()
        m_wk = re.match(r"^(\d+)\s*(?:주차|강)\s*[-—:]?\s*(.*)$", rest)
        if m_wk:
            return f"{mm}/{dd} {m_wk.group(1)}주차 {m_wk.group(2).strip()}"
        return f"{mm}/{dd} {rest}"

    return None


def cleanup_and_rename_existing_notion_pages() -> None:
    """
    Scans all course folder subpages in Notion and:
    1. Archives old duplicate first-pass pages (starting with `[과목명] ...`)
    2. Renames any verbose `📝 26년 00월 00일 강의N — ...` pages into the compact `00/00 0주차 제목` format!
    3. Renames course folder titles from `📂 [일상생활활동] 강의 노트 폴더` to `[일상생활활동]` so they don't wrap on mobile.
    """
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        return

    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip() or "424c5c90104580a8a342d929114d2402"
    if "notion." in parent_page_id:
        m = re.search(r"([0-9a-fA-F]{32})", parent_page_id.replace("-", ""))
        if m:
            parent_page_id = m.group(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    try:
        blocks_resp = requests.get(
            f"https://api.notion.com/v1/blocks/{parent_page_id}/children?page_size=100",
            headers=headers,
            timeout=20,
        )
        if not blocks_resp.ok:
            return

        for folder_blk in blocks_resp.json().get("results", []):
            if folder_blk.get("type") != "child_page":
                continue
            folder_id = folder_blk["id"]
            folder_title = folder_blk.get("child_page", {}).get("title", "")

            # Shorten folder title if it has "강의 노트 폴더"
            if "강의 노트 폴더" in folder_title:
                clean_f_title = re.sub(r"^[📂📁\s]*", "", folder_title)
                clean_f_title = clean_f_title.replace("강의 노트 폴더", "").strip()
                requests.patch(
                    f"https://api.notion.com/v1/pages/{folder_id}",
                    headers=headers,
                    json={"properties": {"title": {"title": [{"text": {"content": clean_f_title}}]}}},
                    timeout=15,
                )
                print(f"[INFO] Renamed Notion course folder: {folder_title} -> {clean_f_title}")

            # Inspect child lecture pages inside the course folder
            sub_resp = requests.get(
                f"https://api.notion.com/v1/blocks/{folder_id}/children?page_size=100",
                headers=headers,
                timeout=20,
            )
            if not sub_resp.ok:
                continue

            sub_pages = [b for b in sub_resp.json().get("results", []) if b.get("type") == "child_page"]
            seen_titles = set()

            # Process newest first so we keep newer pages and archive older duplicates
            for blk in reversed(sub_pages):
                p_id = blk["id"]
                cp_title = blk.get("child_page", {}).get("title", "").strip()

                # Archive old first-pass `[일상생활활동] ...` pages
                if cp_title.startswith("["):
                    requests.patch(
                        f"https://api.notion.com/v1/pages/{p_id}",
                        headers=headers,
                        json={"archived": True},
                        timeout=15,
                    )
                    print(f"[INFO] Archived legacy Notion page: {cp_title}")
                    continue

                new_concise_title = format_concise_notion_title(cp_title)
                final_title = new_concise_title or cp_title

                if final_title in seen_titles:
                    requests.patch(
                        f"https://api.notion.com/v1/pages/{p_id}",
                        headers=headers,
                        json={"archived": True},
                        timeout=15,
                    )
                    print(f"[INFO] Archived duplicate Notion page: {cp_title}")
                    continue

                seen_titles.add(final_title)
                if new_concise_title and new_concise_title != cp_title:
                    requests.patch(
                        f"https://api.notion.com/v1/pages/{p_id}",
                        headers=headers,
                        json={"properties": {"title": {"title": [{"text": {"content": new_concise_title}}]}}},
                        timeout=15,
                    )
                    print(f"[INFO] Renamed Notion page: {cp_title} -> {new_concise_title}")
    except Exception as e:
        print(f"[WARN] cleanup_and_rename_existing_notion_pages error: {e}")


if __name__ == "__main__":
    main()
