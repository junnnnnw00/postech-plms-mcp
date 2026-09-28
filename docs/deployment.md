# 배포와 학생별 계정 연결

공식 문서 확인일: 2026-09-28. 이 문서는 로컬 설치, 테스트용 원격 접근, 다중 사용자 운영을 구분한다. GitHub에 코드가 게시된 상태와 인터넷에서 MCP가 실행되는 상태, 사용자의 휴대전화에서 호출이 성공한 상태는 각각 별도로 검증한다.

## 권장 배포 형태

이 버전은 **개인 인스턴스**다. 각 학생이 자신의 컴퓨터 또는 서버에 설치하고 자신의 PLMS 계정으로 로그인한다. 원격 OAuth 승인은 그 인스턴스의 PLMS 세션에 대한 접근을 허용한다. OAuth 클라이언트를 여러 개 연결해도 PLMS 사용자가 분리되는 것은 아니다. 다른 학생에게 본인의 연결 암호를 공유하지 않는다.

운영자가 여러 학생의 계정을 받는 공용 서비스는 현재 배포 방식에 포함되지 않는다. 이를 만들려면 사용자 인증, 세션 격리, 토큰 폐기, 데이터 삭제 기능을 별도로 갖춰야 한다.

```text
개인 설치
Codex 또는 모바일 Remote → 개인 컴퓨터의 MCP → 본인의 PLMS 세션

향후 다중 사용자 서비스를 구현할 경우
ChatGPT/Codex → HTTPS + 학생별 OAuth → MCP 서버
                                      └→ 요청자에게 속한 PLMS 세션 → PLMS
```

개인용 원격 서버도 성적·출석·과제와 같은 계정 정보에 접근한다면 인증을 켠다. 예시 URL이나 비밀번호가 들어 있지 않은 코드만 공개한다.

## 개인 서버 실행

설치 후 서버 호스트에서 `plms-mcp login` 또는 `plms-mcp import-cookies PATH`로 본인 세션을 등록하고 `plms-mcp doctor`로 확인한다. 설정은 다음 의미를 가진다.

| 환경 변수 | 용도 |
| --- | --- |
| `PLMS_DATA_DIR` | 암호화된 개인 상태를 저장할 비공개 디렉터리 |
| `PLMS_ISSUER_URL` | OAuth에 사용하는 공개 HTTPS origin. 예: `https://YOUR-HOST.example` |
| `PLMS_CONNECT_PASSWORD` | 20자 이상의 무작위 MCP 연결 승인 암호. PLMS 비밀번호와 다른 값 사용 |
| `PLMS_USERNAME`, `PLMS_PASSWORD` | 선택적인 PLMS 로그인 입력. 저장소·로그에 포함하지 않음 |

```sh
plms-mcp serve --transport http --host 127.0.0.1 --port 8765
```

비밀 값은 개인 환경 파일 또는 호스팅 비밀 저장소로 주입한다. 명령어 예제에 실제 값을 넣거나 shell history에 남기지 않는다. `PLMS_DATA_DIR`와 암호화 키는 접근을 제한하고 Git 추적에서 제외한다. 서버를 다른 장치로 옮길 때 키와 상태의 일관성을 유지한다.

OAuth 상태는 개인 데이터 디렉터리의 `oauth.sqlite3`에 암호화되어 저장된다. 연결 암호는 이 상태의 암호화에도 사용되므로 암호 변경 시 OAuth 데이터베이스를 초기화하고 클라이언트를 다시 연결해야 한다. PLMS 로그인 상태는 별개다. HTTP access log에는 승인 화면의 일회용 ticket URL이 남을 수 있으므로 access log를 끈다. 이 구현의 요청 제한은 단일 프로세스 기준이므로 여러 worker로 확대하려면 공유 제한기를 먼저 추가한다.

## Python 서버를 실행할 호스트

Python MCP SDK/Starlette 애플리케이션은 프로젝트의 실행 명령을 지원하는 Python 호스트 또는 컨테이너 환경에 둔다. 서버에 필요한 항목은 다음과 같다.

- 고정된 HTTPS 주소와 `/mcp` 엔드포인트.
- PLMS에 나가는 HTTPS 요청을 허용하는 네트워크.
- 프로세스 재시작, 상태 점검, 충분한 요청 제한 시간.
- 세션을 재사용할 경우 사용자별 암호화 저장소와 지속 볼륨.
- 애플리케이션 설정·비밀을 주입하는 수단. 이미지를 만들 때 비밀을 포함하지 않는다.
- 로그에서 비밀번호, 쿠키, Authorization, 학생 자료 원문을 제거하는 설정.

TLS는 앞단 프록시에서 종료할 수 있으나 앱이 인식하는 공개 URL과 OAuth `resource`·redirect URI가 일치해야 한다. 프록시가 `/mcp` 경로, 인증 헤더 또는 스트리밍 응답을 변경하지 않도록 설정한다. 애플리케이션은 프록시가 누구인지 명시적으로 신뢰해야 한다.

GitHub Pages는 Python 프로세스 호스팅 대안이 아니다. 저장소 배포, 문서 호스팅 및 MCP 런타임 운영을 별개로 계획한다.

## Docker Compose로 개인 서버 실행

저장소의 `Dockerfile`과 `compose.yaml`을 사용한다. `.env.example`을 `.env`로 복사한 뒤, 자신의 공개 HTTPS origin과 별도 연결 암호를 채운다. 예시 도메인은 실제 서비스가 아니다. `.env`는 Git에 넣지 않는다.

```sh
cp .env.example .env
# 로컬 편집기로 .env의 두 필수 값을 설정한 다음 진행
docker compose build
docker compose run --rm plms login --method sso
docker compose run --rm plms doctor
docker compose up -d
docker compose ps
```

`login`은 터미널에서 계정 정보를 입력받아 named volume에 암호화 상태를 저장한다. Compose는 같은 볼륨을 실행 중인 서버에 연결하고, 호스트의 `127.0.0.1:8765`에만 포트를 연다. 인증된 HTTPS 프록시·터널을 앞에 연결한다. 서버 프로세스는 일반 사용자로 실행하고 루트 파일시스템은 읽기 전용이며, 개인 상태와 임시 공간은 별도로 둔다.

Compose가 읽는 `.env`는 일반 Python CLI를 직접 실행할 때 자동으로 로드되지 않는다. 로컬 실행에서는 환경 변수를 프로세스에 전달해야 한다. `docker compose down` 뒤에도 named volume은 유지되며 `down -v`는 로그인·OAuth·비교 상태를 함께 없애므로 재연결이 필요하다. 공개 origin이 바뀌면 설정을 바꾸고 서버와 MCP 클라이언트 연결을 갱신한다.

## 로컬 HTTPS 테스트

앱을 로컬에서 실행하고 MCP Inspector로 먼저 연결한다. 그 뒤 ngrok 같은 HTTPS 터널을 같은 로컬 포트에 연결할 수 있다.

```sh
ngrok http 8765
```

발급된 HTTPS origin을 `PLMS_ISSUER_URL`로 설정하여 서버를 재시작하고, 해당 주소의 `/mcp`를 ChatGPT의 개인 plugin 연결 주소로 쓴다. 터널 URL이 바뀌면 MCP 공개 URL, OAuth 메타데이터 및 클라이언트 연결도 일치하도록 갱신한다. 서버와 터널이 꺼지거나 컴퓨터가 잠들면 요청에 응답할 수 없다. 고정 URL과 재시작 정책이 없는 개발 터널은 장기 운영이 확인된 서버로 취급하지 않는다.

공식 OpenAI 연결 문서는 개발 시 HTTPS 또는 Secure MCP Tunnel을 허용한다. 다만 **Secure MCP Tunnel은 실행용 API 키가 필요**하므로 이 프로젝트의 “OpenAI API 키 없이 사용” 경로에 포함하지 않는다. [Connect and test](https://developers.openai.com/plugins/deploy/connect-chatgpt), [Secure MCP Tunnel prerequisites](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

## 원격 OAuth 계약

ChatGPT/Codex에 노출할 계정별 MCP는 MCP authorization 규격을 따른다. 공개 resource metadata, authorization-server discovery, authorization code + PKCE `S256`, `resource` 바인딩을 제공한다. 클라이언트 등록은 지원하는 방식(CIMD, DCR 또는 사전 등록)을 사용한다. 정확한 redirect URI는 연결 관리 화면의 값을 등록한다. URL을 추측하거나 임의의 redirect를 허용하지 않는다. 요청마다 토큰의 발급자·대상·만료·scope를 검증하며 무효 토큰에는 적절한 `401` challenge를 반환한다. [OpenAI MCP authentication](https://developers.openai.com/plugins/build/auth)

개인 인스턴스에서는 연결 암호를 아는 승인된 클라이언트들이 같은 PLMS 세션에 접근한다. 아래 표는 **향후 다중 사용자 운영으로 확장할 때 필요한 조건**이며, 현재 버전의 사용자 격리 완료를 의미하지 않는다.

| 항목 | 요구 동작 |
| --- | --- |
| MCP 권한 | PLMS 조회 범위만 요청 |
| 사용자 매핑 | OAuth 주체에서 서버가 소유 계정을 결정. 모델이 학생 ID를 바꿔 다른 계정을 선택할 수 없음 |
| PLMS 로그인 | 본인의 계정으로 로그인. MCP OAuth 토큰과 PLMS 세션을 구분 |
| 캐시 | 사용자 ID와 과목 ID를 포함한 키, 사용자별 저장 범위 |
| 첨부파일 | 자료 ID의 소유권과 강의 접근권한을 확인한 뒤 다운로드 |
| 재로그인 | 세션 만료를 “자료 없음”으로 바꾸지 않고 재연결 안내 |
| 연결 해제 | 접근·갱신 토큰 폐기와 PLMS 세션 삭제 |
| 도구 반환값 | 쿠키·비밀번호·로그인 토큰 제외 |
| 로그 | 도구명·소요 시간·실패 분류 중심. 원문 자료와 인증 값 제외 |

운영 환경에서 다중 사용자 검증이 되지 않았다면 개인 설치 또는 한 사람만 접근 가능한 서버로 제한한다. 공개 서비스의 가입·운영 정책은 실제 구현 및 학교와의 관계를 정확히 설명해야 하며, 학교가 승인한 공식 서비스라고 표현하지 않는다.

## 서버를 공개하기 전 검증

1. 인증 없는 호출이 개인 자료를 반환하지 않는지 확인한다.
2. 개인 배포는 사용자별 데이터 디렉터리·암호화 키·연결 암호·인스턴스를 분리한다. 향후 공용 서비스라면 학생 A가 학생 B의 과목·첨부파일 ID를 넣어도 자료를 받지 못하는지 별도 검증한다.
3. 로그아웃·토큰 만료·PLMS 비밀번호 변경 후 이전 연결의 동작을 시험한다.
4. 파일 크기·다운로드 시간·압축 해제 크기·문서 추출 분량 제한을 시험한다.
5. LMS 공지·첨부파일 안의 지시문이 서버 권한이나 도구 동작을 바꾸지 못하는지 확인한다.
6. 과제 제출, 시험 시작, 게시글 작성, 출석 생성 등 변경 요청이 노출되지 않았는지 도구 목록을 검사한다.
7. 빈 결과, 권한 거절, 네트워크 장애, 부분 수집 실패가 구분되는지 확인한다.
8. 마감·출석 기간·실라버스 규정에 원문 근거와 한국 시간대가 붙는지 확인한다.

자동 테스트 통과는 실제 PLMS 페이지와 모바일 클라이언트 연결 확인을 대체하지 않는다. 배포 기록에는 테스트 날짜, 버전, 검증한 클라이언트, 성공한 기능, 확인하지 못한 기능을 분리해서 남긴다.

## GitHub 배포

공개 저장소에는 설치법, 기능 범위, 테스트, 라이선스, 개인정보 처리 설명, 이슈 신고 안내를 포함한다. 실제 계정 정보, 쿠키 저장소, 강의자료, 제출물, 학생 이름이 담긴 테스트 결과·스크린샷은 제외한다. 공개 전 작업 트리뿐 아니라 커밋 이력도 검사한다.

학생들은 같은 소스를 각자 실행할 수 있다. GitHub 소스를 공개하는 것만으로 ChatGPT plugin 디렉터리에 등록되거나 모든 사용자의 계정이 연결되지는 않는다.

## 공용 plugin 디렉터리 배포

공식 배포는 안정적인 공개 HTTPS MCP를 준비하고 plugin 제출 포털에 제출하는 별도 단계다. 검증된 게시자 정보, 설명, 지원·개인정보·이용약관 URL, 기능을 시험할 수 있는 자료가 필요하다. 제출 후 심사와 게시 절차가 남으며 즉시 공개되는 것은 아니다. 심사용으로 개발자의 실제 학생 계정을 공유하지 않고 적절한 데모 방식을 마련한다. [Submit plugins](https://developers.openai.com/plugins/deploy/submission)

개인 테스트 연결을 먼저 사용하고, 실제 계정 격리와 안정성을 확인한 뒤 공개 디렉터리 제출 여부를 결정한다. 모바일에서 최종 사용자가 설치·로그인·도구 호출을 끝낸 증거가 있을 때만 해당 연결을 완료로 기록한다.
