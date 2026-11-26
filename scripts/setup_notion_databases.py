#!/usr/bin/env python3
"""
One-Click Notion Workspace Setup Script
Creates two databases inside NOTION_PARENT_PAGE_ID:
1. 🎓 강의 노트 아카이브 (Lecture Notes DB)
2. ✅ 강의 해야 할 일 & 과제 (Action Items DB)
"""

import os
import sys
import requests


def setup_notion() -> None:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    parent_page_id = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip()
    if not token or not parent_page_id:
        print("Usage: NOTION_TOKEN=secret_xxx NOTION_PARENT_PAGE_ID=xxx python scripts/setup_notion_databases.py")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    lecture_db_payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": "🎓"},
        "title": [{"type": "text", "text": {"content": "강의 노트 아카이브 (AI 자동 정리)"}}],
        "properties": {
            "Name": {"title": {}},
        },
    }
    r1 = requests.post("https://api.notion.com/v1/databases", headers=headers, json=lecture_db_payload, timeout=20)
    if r1.ok:
        print(f"[SUCCESS] Created Lecture DB! NOTION_LECTURE_DB_ID = {r1.json()['id']}")
    else:
        print(f"[ERROR] Failed to create Lecture DB: {r1.text}")

    task_db_payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": "✅"},
        "title": [{"type": "text", "text": {"content": "강의 해야 할 일 & 과제/시험 일정"}}],
        "properties": {
            "Name": {"title": {}},
        },
    }
    r2 = requests.post("https://api.notion.com/v1/databases", headers=headers, json=task_db_payload, timeout=20)
    if r2.ok:
        print(f"[SUCCESS] Created Task DB! NOTION_TASK_DB_ID = {r2.json()['id']}")
    else:
        print(f"[ERROR] Failed to create Task DB: {r2.text}")


if __name__ == "__main__":
    setup_notion()
