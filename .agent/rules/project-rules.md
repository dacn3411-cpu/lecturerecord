# lecturerecord 필수 규칙 (자세한 내용: 저장소 루트의 AGENTS.md)

- `main`은 GitHub Actions가 15분마다 그대로 실행한다. 버그가 들어가면 매 실행이 실패한다.
- 작업 시작 전 항상 `git pull --rebase origin main`.
- push 전 반드시 `python -m unittest discover -s tests -v` 실행. 실패하면 push 금지, 테스트를 지우거나 건너뛰기 금지.
- 정규식 문자 클래스 `[...]` 안의 `-`는 맨 끝에 둔다. `[./-\s]` ❌ → `[./\s-]` ✅
- 새 파싱/날짜/파일명 함수는 `tests/test_smoke.py`에 예시 입력 테스트를 같이 추가.
- 외부 데이터 처리(Drive·Notion·캘린더)는 파일 하나가 실패해도 `[WARN]` 로그만 남기고 계속 진행. 전체 실행이 죽으면 안 됨.
- 비밀값은 코드에 넣지 말고 GitHub Secrets만 사용.
