# POSTECH PLMS MCP

POSTECH PLMS를 자연어로 조회하는 **개인용 읽기 전용 MCP 서버**입니다. Python 3.11+ / stdio / OAuth Streamable HTTP를 지원합니다. POSTECH 공식 서비스가 아니며, 학생마다 자기 계정으로 설치·운영합니다.

## 할 수 있는 일

- 현재/지난 수강 과목, 주별 강의자료, 예정·제한된 활동 조회
- 과제 지시사항, 마감, 제출·채점 상태, 첨부파일 읽기
- 온라인 강의의 **정상 출석 마감 / 지각 인정 마감 / 강사 본문 지시** 구분
- 온라인 학습 진도와 오프라인 전자출결 조회 — 미래·공란·미처리는 출결로 단정하지 않음
- 공지사항 목록·본문·페이지 이동, 과목 내 검색
- 공식 실라버스·추가 계획서 후보, 평가비율·시험·출석·표절 규칙의 원문 근거 추출 및 비교
- 성적부 조회, 주간 할 일, 기한 지난 미제출 과제, ICS 일정 내보내기
- 명시적 조회 간 변경사항 비교(로컬 암호화 기준점)
- PDF, DOCX, PPTX, XLSX, HWP, HWPX, HTML, 텍스트 첨부파일과 페이지/슬라이드/셀 위치
- ZIP 첨부의 안전한 목록과 선택한 내부 문서·소스 파일 읽기

과제 제출, 퀴즈 응시, 쪽지 전송, 영상 재생/시청기록 조작, 출석 변경은 제공하지 않습니다. 스캔 이미지 OCR, 외부 사이트 자동 탐색, 음성/영상 전사는 포함하지 않습니다. 첨부파일의 수식·그림·표 해석이 텍스트만으로 불완전할 수 있으며 추출 경고를 반환합니다.

## 빠른 시작: 내 컴퓨터에 연결

Python 3.11 이상과 Git이 필요합니다. 다음은 macOS/Linux 셸 기준이며, 마지막 두 명령은 Codex CLI가 설치된 경우 사용합니다.

```bash
git clone https://github.com/junnnnnw00/postech-plms-mcp.git
cd postech-plms-mcp
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade 'pip>=26.2.1' 'setuptools>=83.0.0'
python -m pip install -e .
plms-mcp login --method sso
plms-mcp doctor
codex mcp add postech-plms -- "$PWD/.venv/bin/plms-mcp" serve --transport stdio
codex mcp list
```

새 Codex 대화에서 “PLMS 연결 상태를 확인하고 이번 학기 내 과목을 보여 줘”라고 질문합니다. CLI가 없으면 [앱 설정의 MCP 등록 절차](docs/clients.md)를 사용합니다. 다른 MCP 클라이언트에서는 설치된 `plms-mcp`의 절대 경로와 인자 `serve --transport stdio`를 등록합니다. Windows에서는 `py -3 -m venv .venv`로 환경을 만들고 `.\.venv\Scripts\Activate.ps1`로 활성화한 뒤 `plms-mcp.exe`의 절대 경로를 사용합니다.

아이디와 비밀번호는 실행 중 입력하며 비밀번호는 표시하지 않습니다. 기본은 세션 쿠키만 저장합니다. 자동 재인증이 필요하면 `plms-mcp login --method sso --save-password`로 로그인하여 비밀번호의 로컬 암호화 저장을 선택할 수 있습니다. 실패한 자동 로그인은 명시적 재로그인 전까지 중단됩니다. `PLMS_DATA_DIR`로 저장 위치를 지정할 수 있습니다. 키와 암호문을 함께 읽을 수 있는 컴퓨터 관리자는 복호화할 수 있으므로 상태 폴더를 공유하지 마세요.

SSO가 StonePASS나 개인정보 선택을 요구하면 브라우저에서 완료하고 `plms-mcp import-cookies /private/path/cookies.json`으로 **PLMS 도메인 쿠키만** 가져옵니다. 학교 인증 절차를 우회하지 않습니다. `plms-mcp logout`은 이 인스턴스의 로컬 연결 정보를 삭제합니다.

## 데스크톱 / 모바일

- 로컬 데스크톱: MCP 클라이언트에 `.venv/bin/plms-mcp serve --transport stdio` 등록.
- 모바일 및 웹: HTTPS 도메인에 서버를 운영하고 OAuth MCP를 개인 plugin으로 등록합니다. 연결 비밀은 PLMS 비밀번호와 다른 임의의 20자 이상 값입니다.
- 모바일 Remote: 연결된 내 컴퓨터에서 로컬 MCP를 실행하는 경로입니다. 최초 기기 연결과 컴퓨터의 전원·네트워크 유지가 필요합니다.
- 다른 학생: 코드를 설치해 **자기 인스턴스·자기 계정**을 연결합니다. 운영자의 연결 비밀이나 승인된 토큰을 공유하면 운영자의 학업 자료에 접근하게 되므로 공유하지 않습니다.

```bash
export PLMS_ISSUER_URL=https://your-personal-host.example
# PLMS_CONNECT_PASSWORD는 비밀 관리자/보호된 환경 파일에 설정
plms-mcp serve --transport http --host 127.0.0.1 --port 8765
```

연결 주소는 `https://your-personal-host.example/mcp`. Docker Compose와 HTTPS 터널 예제는 [배포 문서](docs/deployment.md), ChatGPT/Codex/모바일 연결은 [클라이언트 문서](docs/clients.md)를 참고하세요. 공개 GitHub 저장소만으로 실행 서버나 모바일 연결이 생기지는 않습니다.

HTTP 서비스와 OAuth 동작은 [검증 기록](docs/verification.md)의 범위에서 확인했습니다. ChatGPT UI의 최종 승인과 휴대전화 실제 호출은 별도의 확인 단계입니다.

## 질문 예시

- “이번 주 미제출 과제와 안 들은 온라인 강의 마감을 알려줘.”
- “과제 PDF를 읽고 제출물과 제약조건을 정리해줘. 파일 어디에 나오는지도 알려줘.”
- “온라인 강의 정상 출석 마감과 지각 인정 마감이 달라?”
- “이 과목 출결을 실라버스 규칙과 비교해줘. 미처리 항목도 구분해줘.”
- “최근 공지에서 휴강이나 시험 변경이 있었어?”
- “실라버스 평가비율과 성적부의 현재 점수를 비교해줘.”

## 응답의 근거와 한계

성공한 조회 결과는 출처 URL, 결과 구성 시각, 시간대, 경고를 포함합니다. 페이지 캐시는 최대 90초 재사용할 수 있습니다. 한국시간 기준이며 날짜만 명시된 항목에 임의로 23:59를 붙이지 않습니다. 원문에서 확정할 수 없는 출석 환산, 예외, 최종 성적은 `unknown`으로 남깁니다. 검색은 범위가 정해져 있으므로 공지 본문·다음 페이지·첨부파일은 추가 조회가 필요합니다. 실라버스보다 나중에 나온 공지가 있을 수 있어 자동 비교를 공식 판정으로 사용하지 않습니다.

세션·저장 비밀번호·변경 기준점은 Fernet 암호문이며 파일은 0600, 상태 디렉터리는 0700입니다. HTTP는 OAuth 2.1 흐름(S256 PKCE, 짧은 단회 코드, 토큰 회전/폐기, resource 검증)을 적용합니다. 단일 사용자·단일 프로세스 운영을 전제로 하며 다중 학생 공용 서비스는 구현하지 않았습니다.

## 개발 및 검증

```bash
python -m pip install -e '.[dev]'
pytest -q
ruff check src tests
python -m build
pip-audit
```

테스트는 합성 데이터만 사용합니다. 실제 로그인 및 학교 자료는 Git에 넣지 않습니다. 라이브 검증 범위와 미완료 연결 항목은 [검증 기록](docs/verification.md)에 구분하여 기록합니다. 보안 모델과 운영상 주의점은 [SECURITY.md](SECURITY.md)를 참고하세요.

- [도구 19개와 입력·반환·예제](docs/tools.md)
- [모듈 구조와 데이터 흐름](docs/architecture.md)

## 라이선스

MIT. 학교 강의자료와 타인의 저작권은 이 코드 라이선스에 포함되지 않습니다.
