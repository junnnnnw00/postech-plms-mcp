# 데스크톱과 모바일 연결

공식 문서 확인일: 2026-09-28. 메뉴명과 기능 제공 여부는 앱 버전, 계정, 워크스페이스 정책에 따라 달라질 수 있다. 아래는 연결 절차이며, 이 문서가 존재한다는 사실만으로 특정 계정이나 휴대전화의 연결이 완료된 것은 아니다.

## 연결 경로 선택

| 사용 환경 | 연결 방법 | 컴퓨터를 계속 켜 둬야 하나? |
| --- | --- | --- |
| 내 컴퓨터의 Codex | 로컬 STDIO MCP | 해당 컴퓨터에서 질문하는 동안 필요 |
| 휴대전화에서 내 컴퓨터의 Codex 사용 | ChatGPT 모바일의 Remote | 필요 |
| ChatGPT 웹·모바일에서 직접 질문 | HTTPS 원격 MCP를 연결한 개인/공개 plugin | 서버가 별도 호스팅되면 개인 컴퓨터는 불필요 |
| 다른 학생에게 소스 배포 | 각자 설치·로그인하는 로컬 MCP | 각자의 실행 환경에 따름 |
| 여러 학생이 각각 원격 사용 | 각 학생의 개인 HTTPS MCP 인스턴스 | 각자의 서버 필요 |

공식 OpenAI 문서는 계정에서 이용 가능한 plugins를 ChatGPT 웹·데스크톱·모바일의 Chat 또는 Work에서 쓸 수 있다고 안내한다. `Desktop only`인 plugin은 모바일에서 직접 실행할 수 없다. PLMS를 모바일에서 직접 쓰려면 원격 MCP 연결을 포함한 plugin을 설치해야 한다. [Plugins](https://learn.chatgpt.com/docs/plugins)

## Codex 데스크톱: 로컬 연결

프로젝트 README의 설치·로그인을 먼저 끝낸다. 학생 계정의 로그인 정보는 개인 로컬 설정에만 저장하고, GitHub 저장소나 MCP 도구 인자에 넣지 않는다.

```sh
plms-mcp login
plms-mcp doctor
codex mcp add postech-plms -- /ABSOLUTE/PATH/TO/plms-mcp serve --transport stdio
```

`/ABSOLUTE/PATH/TO/plms-mcp`에는 설치된 실행 파일의 절대 경로를 입력한다. 외부 브라우저에서 정상 로그인한 세션을 가져와야 하는 경우 프로젝트가 지원하는 쿠키 파일 형식으로 `plms-mcp import-cookies /PRIVATE/PATH/cookies.json`을 사용한다. 쿠키 파일은 비밀번호와 마찬가지로 개인 파일로 보관한다. `doctor`에서 로그인·조회까지 확인되어야 준비가 끝난다.

1. 데스크톱 앱의 **Settings → MCP servers → Add server**에서 이름을 `postech-plms`로 정한다.
2. **STDIO**를 고르고 실행 파일을 `plms-mcp`의 절대 경로, 인자를 `serve --transport stdio`로 입력한다. CLI로 이미 등록했다면 추가로 중복 등록하지 않는다.
3. 저장하고 서버를 **Restart**한다.
4. 새 대화에서 `/mcp`로 연결을 확인한 뒤 “PLMS에서 이번 학기 내 수강 과목을 조회해 줘”라고 질문한다.

Codex의 데스크톱 앱·CLI·IDE 확장은 같은 호스트의 MCP 설정을 공유한다. 로컬 `config.toml`은 ChatGPT 웹에서 읽지 않는다. 따라서 로컬 설정 추가가 휴대전화의 일반 Chat/Work에 원격 plugin을 설치하는 효과를 내지는 않는다. [MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)

## Codex 데스크톱: 원격 연결

운영자가 OAuth를 지원하는 HTTPS 서버를 배포한 경우:

```sh
codex mcp add postech-plms-remote --url https://YOUR-PLMS-HOST.example/mcp
codex mcp login postech-plms-remote
codex mcp list
```

`YOUR-PLMS-HOST.example`은 예시이며 실제 서버 주소로 바꾼다. 이 버전은 **한 사람의 PLMS 세션을 보관한 개인 서버**다. OAuth 승인 화면에는 서버 운영 시 정한 `PLMS_CONNECT_PASSWORD`를 입력한다. 이 연결 암호는 PLMS 비밀번호와 별개이며, 서버에 저장된 본인 PLMS 세션의 조회 권한을 부여한다. URL 쿼리에 비밀번호·토큰을 붙이지 않는다. 도구 목록을 못 읽으면 로그인만 반복하지 말고 [배포 문서](deployment.md)의 인증 및 전송 계층 점검을 따른다.

## ChatGPT 웹과 모바일: 원격 plugin 연결

개인 테스트 연결은 웹에서 등록하고 모바일에서 사용 여부를 확인하는 순서를 권장한다.

1. ChatGPT **Settings → Security and login → Developer mode**를 켠다. 항목이 없다면 계정·워크스페이스에서 제공되는지 확인한다.
2. **Plugins → +**에서 이름을 `PLMS`, 주소를 운영 중인 `https://…/mcp`로 입력한다.
3. 인증 방식은 서버가 제공하는 **OAuth**를 선택하고 연결한다. 본인 개인 서버의 승인 화면에서 `PLMS_CONNECT_PASSWORD`로 조회 연결을 승인한다. PLMS 비밀번호를 이 화면에 입력하지 않는다.
4. 발견된 도구 목록을 확인한다. 개인 Plugins 목록에서 생성된 plugin을 설치한다.
5. 웹의 새 Chat/Work에서 `@PLMS`를 선택하고 수강 과목 조회를 시험한다.
6. 휴대전화의 최신 ChatGPT 앱에서 **같은 계정·워크스페이스**로 로그인한다. 새 Chat/Work에서 `@PLMS`를 선택하고 같은 질문을 한다.

공식 연결 절차는 HTTPS `/mcp`, 개발자 모드 및 도구 메타데이터 새로고침을 설명한다. 도구가 바뀌면 Plugins의 해당 연결에서 **Refresh** 후 새 대화로 다시 시험한다. [Connect and test your plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt), [Plugin quickstart](https://developers.openai.com/plugins/quickstart)

모바일에서 plugin이 보이지 않으면 같은 계정·워크스페이스인지, 설치됐는지, `Desktop only`인지, 해당 계정의 제공 범위와 관리 정책이 허용하는지 확인한다. 모바일에서의 실제 도구 호출까지 성공하기 전에는 “폰 연결 완료”로 판단하지 않는다.

## 모바일 Remote: 로컬 MCP를 그대로 사용

원격 서버를 배포하기 전에도 사용할 수 있는 경로다. 휴대전화가 연결된 컴퓨터의 도구와 설정으로 작업한다.

1. 컴퓨터의 최신 데스크톱 앱에서 **Settings → Connections → Control this Mac or PC → Set up/Add**를 연다.
2. 표시된 QR 코드를 휴대전화로 스캔한다.
3. 같은 ChatGPT 계정·워크스페이스로 로그인하고 기기 연결을 승인한다.
4. 모바일 앱의 **Remote**에서 컴퓨터를 고른 뒤 PLMS MCP가 설정된 작업을 연다.
5. 컴퓨터가 켜져 있고 인터넷에 연결된 상태로 질문한다.

QR 스캔, 계정 인증 및 기기 승인은 사용자의 휴대전화에서 필요한 단계다. 연결 상태를 실제 확인하지 않은 채 이를 자동 완료했다고 표기하지 않는다. Remote 가용성은 계정 정책과 배포 상황에 영향을 받으며, CLI만으로 모바일 기기 설정을 끝낼 수는 없다. [Codex Remote](https://learn.chatgpt.com/docs/remote), [Remote connections](https://learn.chatgpt.com/docs/remote-connections)

## 연결 후 시험할 질문

- “이번 주까지 제출해야 하는 과제를 과목별로, 정확한 마감 날짜와 시각까지 알려 줘.”
- “시청 기한이 남은 온라인 강의를 알려 주고 출석 인정 기간과 구분해 줘.”
- “이 과목의 과제 첨부파일을 읽고 제출 형식, 파일명, 필수 구현 항목을 정리해 줘.”
- “이번 주 새 공지 중 강의실·시험·마감 변경을 찾아서 원문 링크와 함께 알려 줘.”
- “내 출석 현황을 강의계획서 출결 규정과 비교하고 확인되지 않은 부분은 표시해 줘.”
- “공지의 변경된 마감과 실라버스의 원래 일정이 다르면 둘 다 보여 줘.”
- “내가 아직 제출하지 않은 항목만 보여 주고, 제출 여부가 확인 안 되는 항목은 따로 알려 줘.”

응답에는 가능하면 과목명, 자료 제목, 원문 링크, 조회 시각, 한국 시간 기준 마감, 확인되지 않은 정보가 포함되어야 한다. 도구에서 접근할 수 없는 자료를 읽었다고 주장하거나 강의를 자동 재생해 출석을 생성해서는 안 된다.

## 연결 해제와 비밀번호 변경

PLMS 비밀번호를 바꾼 뒤 로그인 오류가 나면 서버 호스트에서 `plms-mcp login` 또는 쿠키 가져오기를 다시 실행한다. OAuth만 재승인한다고 PLMS 세션이 갱신되는 것은 아니다. `PLMS_CONNECT_PASSWORD`를 바꾸려면 OAuth 데이터베이스를 초기화하고 클라이언트를 다시 연결한다. 실제 서버의 토큰 폐기·데이터 삭제 절차를 따른다. 로컬 MCP 제거는 설정만 없앨 수 있다.

MCP 서버는 OpenAI API 호출 없이 PLMS 자료를 제공한다. 사용하는 ChatGPT/Codex 계정의 이용 조건과 서버 호스팅 비용은 별개다.
