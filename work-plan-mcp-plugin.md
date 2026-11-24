# 작업: text2sql MCP·플러그인 정리

> 2026-11-24 개정. 세 저장소의 최신 `main`(mcp `3c406e5`, plugin `ec4ae60`, semantic-definitions `075a232`)과 대조해 고쳤다.
> 줄 번호는 이 커밋 기준이다. 코드가 바뀌었으면 줄 번호가 아니라 내용으로 찾는다.

## 작업 뒤 바뀐 결정 (2026-11-24, 1~4단계 이후)

아래 내용은 이 문서의 해당 항목보다 우선한다. 지금 코드는 아래대로 동작한다.

- **SQL 은 막지 않고 알린다.** 2-1·2-2 의 조인 검사는 거절이 아니라 `unapproved_join` 경고다. 정의 밖 테이블·컬럼(`outside_definition`), `t2s run` 의 집계(`undefined_calculation`), 승인 전 지표·연결(`draft`)도 실행하고 경고한다. 막는 것은 안전 장치(쓰기·여러 문장·위험 함수·행 수·시간·느린 쿼리)와 DB 에 없는 테이블·컬럼뿐이다.
- **민감 필드(2-3) 기능은 지웠다.** 스터디 범위가 아니라서 게시·스냅샷·t2s 의 `sensitivity` 처리를 모두 뺐다.
- **`ask` 의 기본 지표 경로는 `t2s sql` 이다.** 에이전트가 SQL 을 쓰고 지표는 `AGG(별칭.지표)` 로 넣는다. `t2s compile` 은 AGG 로 쓸 수 없는 모델 수준 지표에만 쓴다. 골든셋 지표 문항은 `tests/test_agg_golden.py` 로 이 경로를 확인한다.
- **정의 검색은 BM25 전문 검색이다.** 게시기가 `definition_text` 인덱스(cjk 분석기)를 만들고, `graph_search_definitions` 는 참조명·이름·동의어 정확 일치 → BM25 점수 순으로 정렬한다. 인덱스가 없으면 부분 일치로 돌아간다. Aura 에는 인덱스를 만들어 두었다.
- **Olist 정의에 동의어를 더했다(정의 번호 11).** 다시 게시해야 그래프에 들어간다.
- **`graph_find_join_path`** 는 승인된 경로가 없으면 `unapproved_paths` 로 승인 전 연결 경로를 함께 준다.

## 배경

상위 저장소 `text2sql`은 세 저장소를 Git Submodule로 묶고 있다. 각 서브모듈은 독립 저장소이므로 커밋은 해당 서브모듈 안에서 한다.

- `mcp/` (text2sql-mcp): Neo4j에 게시된 시맨틱 정의와 계보를 읽기 전용 MCP 도구로 제공한다. Railway에서 Streamable HTTP로 운영한다.
- `plugin/` (text2sql-plugin): Claude Code·Codex 플러그인이다. 스킬이 `t2s` CLI를 호출해 SQL을 만들고, 가드로 검사한 뒤, 원본 DB에서 읽기 전용으로 실행한다. `t2s`는 서버에서 활성 모델 스냅샷을 받아 메모리에서 컴파일한다.
- `semantic-definitions/`: Ossie 정의 파일과 Neo4j 게시 코드(`graphdb/plan.py`, `graphdb/neo4j_writer.py`)가 있다.

GraphDB를 도입한 목적은 LLM에 들어가는 메타데이터 컨텍스트를 줄이는 것이다. 그런데 현재는 `t2s context`가 전체 catalog(Olist 기준 15,727자)를 LLM에 그대로 넘기고, 전체 스냅샷 도구(`graph_get_model_snapshot`)도 LLM이 호출할 수 있게 노출되어 있다. 이번 작업의 핵심은 이 구조를 바꾸는 것이다. 다만 `t2s` 프로세스가 메모리에 전체 모델을 들고 컴파일·검증하는 것은 유지한다. 조인 경로 탐색과 가드 검증에는 전체 모델이 필요하다.

## 이번 개정에서 확정한 결정

1. **스냅샷 도구는 MCP에서 제거한다.** deprecated로 남기지 않는다. 플러그인도 같이 바꾼다.
   - MCP 리소스로도 내지 않는다. Claude Code는 리소스를 목록으로 보고 읽는 도구를 LLM에 자동으로 주기 때문에, 리소스로 옮겨도 LLM이 읽을 수 있다.
   - 대신 MCP 밖의 일반 HTTP 경로(`GET /snapshot`)로 준다. 같은 Bearer 토큰으로 보호하고 `t2s`만 부른다. (1-1, 1-4)
2. **스냅샷 로컬 캐시는 이번 작업에서 뺀다.** `version_id`는 정의 파일 내용의 해시(`plan.py:121`)라서, 게시 코드나 서버 출력이 바뀌어도 값이 같다. 캐시 키로 쓰면 낡은 사본이 남는다. 스냅샷은 약 62KB라 매번 받아도 된다.
3. **`is_time` 전달은 이번 작업에서 뺀다.** 2-3은 `sensitivity`만 다룬다.
4. **소스 판별 규칙은 세 저장소를 맞춘다.** MCP만이 아니라 `plan.py`도 같은 규칙으로 고친다. (2-4)
5. **`t2s context`의 `tables`는 남긴다.** 테이블 이름 목록이라 작고(Olist 기준 10개), 빼면 스킬이 `t2s schema`를 한 번 더 불러야 한다. 출력 계약을 깨는 변경을 `catalog` 하나로 줄인다. (1-3)
6. **`/readyz`는 만들지 않는다.** 인증 없는 경로가 DB에 붙게 되고, 같은 정보를 인증된 `/snapshot`의 503 응답으로 알 수 있다. Railway 점검 경로는 `/healthz` 그대로 둔다. (3-3)
7. **옛 플러그인과의 호환은 포기한다.** 옛 도구를 한 버전 더 남기지 않는다. 배포 순서와 짝이 되는 버전을 문서에 적는 것으로 대신한다. (규칙 6)

## 반드시 지킬 것

1. 실제 DB, Neo4j(Aura), Railway에 접속하지 않는다. `DATABASE_URL`, `NEO4J_*`, `MCP_API_TOKEN`, `TEXT2SQL_MCP_TOKEN` 값을 읽거나 출력하지 않는다. 검증은 각 저장소의 테스트 픽스처(임시 SQLite, FakeTx 등)와 목(mock)으로만 한다. 통합 테스트는 건너뛴 상태로 둔다. 테스트를 돌릴 때 셸에 이 환경변수가 설정돼 있으면 빼고 돌린다.
2. 수정 전에 해당 파일을 먼저 읽는다. 이 문서의 설명과 실제 코드가 다르면 코드를 기준으로 판단하고, 차이를 보고서에 적는다.
3. `t2s`의 출력 계약을 유지한다. 모든 출력은 stdout에 JSON 하나이고, 오류는 `{"error": {"kind", "message", "retryable"}}` 형식이다. 이번에 의도적으로 바꾸는 것은 `t2s context`의 `catalog` 제거(1-3)뿐이다.
4. Claude Code(`.mcp.json`, `.claude-plugin/plugin.json`)와 Codex(`mcp.json`, `plugin.json`)를 모두 유지한다. 한쪽만 고치지 않는다.
5. `sqlglot==30.25.0` 고정은 바꾸지 않는다.
6. **하위 호환을 지키지 않는 깨지는 변경이 있다. 숨기지 않고 문서에 적는다.**
   - MCP 0.3.0은 `graph_get_model_snapshot` 도구가 없다. plugin 0.5.1 이하는 MCP 0.3.0에서 `graph_unavailable`로 실패한다.
   - plugin 0.6.0은 `/snapshot` 경로가 필요하다. MCP 0.2.0에서는 404로 실패한다. 옛 도구로 대체 호출하지 않는다.
   - 배포 순서는 MCP 먼저, 플러그인 다음이다. 두 저장소 README에 "변경 사항" 절을 만들어 짝이 되는 버전과 순서를 적는다. 두 저장소 모두 CHANGELOG 파일은 없다.
7. 공식 문서로 확인할 수 없는 값은 추측해서 코드에 넣지 않는다. 확인하지 못한 것은 코드 변경 없이 보고서에 "확인 필요"로 남긴다.
8. **커밋하지 않는다.** 코드만 고치고 변경은 작업 폴더에 둔다. `git commit`, `git push`, `git stash`를 쓰지 않는다. 커밋은 내가 직접 한다. 여러 저장소에 걸친 항목(2-3, 2-4, 2-5)의 작업 순서는 definitions → mcp → plugin이다. 상위 저장소(`text2sql`)의 서브모듈 포인터는 건드리지 않는다.
9. 단계 하나를 끝낼 때마다 테스트를 돌리고, 아래 보고 형식으로 보고한 뒤 멈춘다. 다음 단계는 내가 확인한 뒤 진행한다.

## 테스트 명령과 기준값

| 저장소 | 명령 | 변경 전 결과 |
| --- | --- | --- |
| plugin | `cd plugin && uv run pytest -q` | 115 passed, 4 skipped |
| mcp | `cd mcp && uv run python -m unittest discover -s tests -v` | 8 tests OK (skipped 2) |
| semantic-definitions | `cd semantic-definitions && uv run --with pytest python -m pytest -q` | 9 passed |

- semantic-definitions는 `uv run pytest -q`로는 돌지 않는다. `pytest`가 의존성에 없고 `graphdb`를 찾지 못한다. 위 명령을 쓴다.
- semantic-definitions에서 `uv run`을 쓰면 `uv.lock`이 새로 생긴다. 커밋에 넣지 않는다.

---

## 1단계: 컨텍스트 축소

### 1-1. [mcp] 스냅샷을 MCP 밖 HTTP 경로로 옮기고 도구를 제거

- `http_app.py`에 `GET /snapshot`(기본 모델, `GRAPH_DEFAULT_MODEL`)과 `GET /snapshot/{model}`을 추가한다.
  - `/mcp`와 같은 Bearer 인증을 거친다. 인증 없이 열리는 경로는 `/healthz`뿐이다.
  - 구현은 `BearerAuth.__call__`에서 인증을 통과한 뒤 경로가 `/snapshot`이면 직접 응답하는 방식으로 한다. 라우터를 새로 만들어 MCP 앱을 그 아래에 끼우지 않는다. MCP 앱의 시작·종료 처리(lifespan)를 건드리지 않기 위해서다.
  - `GET`만 받는다. 다른 메서드는 405다.
  - 응답 본문은 기존 `get_model_snapshot` 결과(`tenant`, `model`, `version_id`, `definition`)를 JSON으로 만든 것이다. `Content-Type: application/json`.
  - 오류는 `{"error": {"code", "message"}}`와 HTTP 상태 코드로 낸다. `NOT_FOUND`·`NO_ACTIVE_VERSION`은 404, `DB_UNAVAILABLE`은 503, 그 밖의 `GraphError`는 500이다. 응답에 내부 정보(쿼리, 접속 주소)를 넣지 않는다.
  - Neo4j 드라이버는 동기 방식이다. 이벤트 루프를 막지 않게 스레드에서 실행한다.
  - 기존 `tests/test_http_app.py`가 그대로 통과해야 한다.
- `server.py`의 `graph_get_model_snapshot` 도구를 삭제한다. MCP 리소스로도 추가하지 않는다.
- `graph_repository._read()`의 128 KiB 상한은 일반 도구에 그대로 두고, 스냅샷에는 별도 상한(1 MiB)을 적용한다. 상한 값은 인자로 받게 한다.
- stdio 실행(`server.main`)에는 스냅샷 경로가 없다. README에 적는다.
- 테스트:
  - 토큰이 없거나 틀리면 401이다.
  - 정상 응답이 `repository().get_model_snapshot` 결과와 같다. 모델을 지정한 경로와 기본 경로를 모두 본다.
  - 스냅샷이 128 KiB를 넘어도 1 MiB 이하면 통과하고, 1 MiB를 넘으면 `RESULT_TOO_LARGE`다.
  - MCP 도구 목록에 `graph_get_model_snapshot`이 없다.
  - 기존 `tests/test_model_snapshot.py`, `tests/test_graph_tools.py`를 새 구조에 맞게 고친다.

### 1-2. [mcp] 조인 경로 도구 추가

- `graph_find_join_path(datasets: list[str], model?, max_hops=4)`를 추가한다. 입력한 데이터셋 순서대로 인접한 쌍마다 경로를 구한다.
- `t2s`가 실제로 받아 주는 연결과 같은 조건을 쓴다. 모든 관계가 `status = 'approved'`이고 `n_to_1 = true`인 경로만 허용한다. `t2s`는 키가 맞지 않는 연결(`engine.py`의 `bad_edges`)을 승인 상태여도 쓰지 않는다.
- 길이가 같은 최단 경로가 둘 이상이면 하나를 임의로 고르지 않는다. `allShortestPaths`로 모두 구해 정해진 순서로 돌려주고 `ambiguous: true`를 붙인다. `t2s` 엔진은 경로가 하나일 때만 쓴다(`engine.py:624`).
- 각 hop에 `name`, `from`(startNode.ref), `to`(endNode.ref), `from_columns`, `to_columns`, `n_to_1`을 담는다. 경로를 따라가는 방향이 관계의 `to → from`이면 `reverse: true`를 붙인다. 이 방향은 한 행 쪽에서 여러 행 쪽으로 가므로 행이 늘어난다.
- 연결되지 않는 쌍은 `unreachable`에 넣는다. 없는 데이터셋 이름은 `unreachable`이 아니라 `NOT_FOUND` 오류로 낸다.
- 입력 검증: 데이터셋 2~6개, `max_hops`는 1~6 사이 정수(`bool`은 거절)다. 인접한 두 값이 같으면 `INVALID_INPUT`이다. Neo4j의 최단 경로는 시작과 끝이 같으면 오류를 낸다. 가변 길이 상한은 정수 검증 후 Cypher 문자열에 넣고, 나머지 값은 모두 매개변수로 넘긴다.
- 활성 버전 범위(`_scope`)와 128 KiB 상한을 지킨다.
- 도구 설명에 "안내용이다. 최종 판정은 `t2s validate`가 한다"고 적는다.
- 테스트: 정상 경로, draft 연결만 있는 쌍이 unreachable로 나오는지, `n_to_1 = false`인 승인 연결만 있는 쌍이 unreachable로 나오는지, 경로가 둘이면 `ambiguous`가 붙는지, 잘못된 입력(개수, `max_hops`, 같은 이름 연속, 없는 데이터셋)을 거절하는지 확인한다.

### 1-3. [plugin] `t2s context` 축소

- `_context()`(`cli.py:339`)에서 `catalog`를 기본 출력에서 뺀다. 남는 값은 `dialect`, `schema`, `max_rows`, `readonly_guarantee`, `definition`(이름, revision, version_id, source, 로컬이면 path)이다.
- `--datasets a,b` 옵션을 추가한다. 지정하면 해당 데이터셋 범위의 요약만 `catalog`로 낸다. `SemanticModel.catalog(only: set[str] | None)`로 지표·필드·연결·표 절을 걸러낸다. 연결은 양 끝이 모두 범위 안에 있는 것만 넣는다. 없는 데이터셋이면 `unknown_table` 오류를 낸다.
- 걸러낸 catalog에도 경계 문구(`<<정의 데이터 시작: …>>`, `<<정의 데이터 끝>>`)는 그대로 둔다. 설명 안의 문장이 지시로 읽히지 않게 하는 장치다.
- `--definition PATH`(게시 전 로컬 정의 미리 보기)는 그대로 동작해야 한다. `--datasets`와 같이 쓸 수 있다.
- `question` 위치 인자는 남기고 무시한다. 지금도 쓰지 않으며 도움말에 그렇게 적혀 있다. 스킬 문서에서는 넘기지 않게 고친다(1-5).
- `tables`(조회 가능한 테이블 목록)는 그대로 둔다.
- 테스트: 기본 출력에 catalog가 없는지, `--datasets` 범위 밖 지표·필드·연결이 섞이지 않는지, 경계 문구가 남는지, `--definition`과 같이 써도 되는지 확인한다.
- Olist 정의(`../semantic-definitions/definitions/olist/sales.ossie.yaml`)로 변경 전후 출력 크기(문자 수)를 측정해 보고한다. 변경 전 catalog는 15,727자다.

### 1-4. [plugin] 스냅샷을 새 HTTP 경로에서 받기

- `graph_client.fetch_model`을 `GET <기준 주소>/snapshot[/<model>]` 호출로 바꾼다.
  - 기준 주소는 `TEXT2SQL_MCP_URL`(기본값은 지금의 `DEFAULT_URL`)에서 끝의 `/mcp`를 뗀 것이다.
  - 모델 이름은 URL 경로에 넣기 전에 인코딩한다.
  - 토큰은 지금처럼 `TEXT2SQL_MCP_TOKEN`을 Bearer로 보낸다.
  - 지금 쓰는 HTTP 라이브러리(`httpx2`)의 동기 호출을 쓴다. MCP 클라이언트 세션과 `asyncio`는 걷어낸다.
- `graph_get_model_snapshot` 도구로 대체 호출하지 않는다.
- `mcp` 패키지를 쓰는 곳은 `graph_client.py`뿐이다. 다른 사용처가 없음을 다시 확인한 뒤 `pyproject.toml`에서 `mcp` 의존성을 빼고 `uv.lock`을 갱신한다. `sqlglot==30.25.0`이 그대로인지 확인한다.
- 로컬 캐시는 만들지 않는다.
- 응답 검증(`definition`이 dict인지)과 `cli._model()`의 `GRAPH_VERSION_ID` 일치 확인은 그대로 둔다.
- 실패 메시지를 원인별로 나눈다. 토큰 값은 메시지에 넣지 않는다.
  - 401·403: 토큰을 확인하라고 안내한다.
  - 404: 모델이 없거나 서버가 0.3.0 미만이라고 안내한다.
  - 5xx: 서버 쪽 문제라고 안내한다.
  - 연결 실패·시간 초과: 주소와 네트워크를 확인하라고 안내한다.
- 테스트(목 전송 계층 사용): 정상 응답, 모델 지정 시 경로, 토큰 없음, 위 네 가지 실패 메시지, 메시지에 토큰이 없는지 확인한다. `tests/test_cli.py`의 `fetch_model` 대체 지점이 그대로 동작하는지 본다.

### 1-5. [plugin] 스킬 문서 정리

- 대상:
  - `skills/ask/SKILL.md` (catalog 참조: 34, 44, 81, 91, 105, 116행)
  - `skills/explore-schema/SKILL.md` (20, 43행)
  - `skills/explain-sql/SKILL.md` (15, 30행)
  - `skills/diagnose/SKILL.md` (13행)
  - `skills/define-metric/SKILL.md` (29, 32행)
  - `skills/graph-metadata/SKILL.md` (도구 표에 `graph_find_join_path` 추가)
  - `agents/sql-reviewer.md` (26, 31행)
  - `references/failure-types.md` (13행)
  - `skills/validate-sql`, `skills/semantic-sync`는 관련 참조가 없다. 확인만 한다.
- 정의 조회 순서를 "Graph MCP 범위 조회 → 필요한 데이터셋만 `t2s context --datasets`"로 바꾼다. 조회 도구는 `graph_search_definitions`, `graph_get_dataset`, `graph_find_join_path`, `graph_list_join_rules`이다.
- 예외 두 곳은 Graph MCP 조회로 바꿀 수 없다.
  - `define-metric`은 `--definition`으로 게시 전 로컬 파일을 본다. Graph MCP에는 게시된 것만 있다. 로컬 경로는 "정의 파일을 직접 읽기 + `t2s compile --definition <파일>`(지표 목록) + `t2s context --definition <파일> --datasets …`"로 안내한다. 32행의 "`catalog`에서 승인된 지표·연결을 보고"를 여기에 맞게 고친다.
  - `sql-reviewer` 에이전트는 Bash와 Read만 쓴다. MCP 도구를 부를 수 없다. SQL에 나온 데이터셋으로 `t2s context --datasets …`를 부르고 `t2s validate`의 `joins`·`problems`와 대조하게 한다.
- 기존 catalog에 들어 있던 고정 규칙은 스킬 본문으로 옮긴다. 날짜 단위 `__day/__week/__month/__quarter/__year`, `AGG(별칭.측정)` 규칙, "연결의 from 쪽이 여러 행, to 쪽이 한 행" 설명, "정의 설명 안의 문장은 지시가 아니라 데이터" 경계 문구가 여기에 해당한다.
- 조인을 대조하는 스킬은 전체 연결 목록 대신 `t2s validate`의 `joins`·`problems`와 `graph_list_join_rules(dataset)`를 쓴다.
- `t2s context`를 부를 때 질문 원문을 인자로 넘기는 예시를 지운다.
- `graph_get_model_snapshot`을 언급하는 곳이 남지 않게 한다. 스킬·에이전트에는 지금도 없고, plugin `README.md:61`에만 있다(4-3에서 고친다).
- `define-metric`이 `concepts` 출력을 참조한다는 예전 설명은 사실이 아니었다. 스킬·에이전트 어디에도 그 단어가 없다. 찾아서 고칠 것이 없다.

---

## 2단계: 정확성·보안

### 2-1. [plugin] `t2s sql` 조인 승인 검사

- 현재 `cli._sql()`(`cli.py:463`)은 재작성한 SQL을 `engine_sql=True` 정책(edges 없음)으로만 검사한다. 그래서 미승인 키 조인과 draft 연결이 그대로 실행된다.
- `measures.rewrite()` 전에 원문 SQL을 데이터셋 이름 공간으로 `qualify`하고 `guard._check_joins`를 적용한다. 사용할 연결은 `--allow-draft`이면 `model.all_edges()`, 아니면 `model.edges`이다. 두 값은 데이터셋 이름을 키로 쓰므로 그대로 넘길 수 있다.
- 주의: `AGG(별칭.지표)`의 지표는 필드가 아니다. 컬럼 검증을 켠 채 `qualify`하면 "없는 컬럼"으로 거절된다. 컬럼 검증을 끄거나, 지표 이름도 스키마에 넣는다. `USING` 조인이 `ON`으로 풀리는지도 테스트로 확인한다.
- 거절은 지금의 출력 계약대로 `unknown_join`(retryable)으로 낸다.
- 테스트: `order_items.order_id = customers.customer_id` 조인 거절, draft 연결 `order_items → sellers` 거절, 같은 SQL에 `--allow-draft`를 주면 통과, 승인 연결 조인 통과, `AGG()`를 쓴 승인 조인 통과, CTE로 먼저 집계한 뒤 키로 다시 잇는 SQL 통과를 확인한다. 기존 `tests/test_measures.py`는 모두 통과해야 한다.

### 2-2. [plugin] 하위 쿼리로 잇는 조인 검사

- `guard._check_joins`(`guard.py:126`)는 `JOIN ... ON`만 검사한다. 하위 쿼리가 바깥 테이블과 등식으로 이어지는 곳을 모두 승인 연결과 대조한다.
  - 상관 하위 쿼리 안에서 바깥 소스 컬럼과 안쪽 소스 컬럼을 잇는 등식. `EXISTS`, `NOT EXISTS`, `SELECT` 목록이나 `WHERE`의 스칼라 하위 쿼리가 모두 여기에 든다.
  - `col IN (SELECT 단일컬럼 ...)`과 `col = ANY (SELECT ...)`의 좌우 쌍.
  - 같은 테이블끼리의 등식은 지금처럼 대조하지 않는다.
- 구현 주의: `_origin()`은 지금 범위의 소스만 본다. 바깥 범위 컬럼은 `None`이 되어 "거슬러 찾지 못함"으로 잘못 거절된다. 바깥 범위(`scope.parent`)까지 따라가게 한다.
- 아래 세 쿼리가 현재는 통과한다(직접 돌려 확인함). 수정 후에는 `unknown_join`으로 거절되어야 한다. 승인 키를 쓴 같은 모양의 쿼리는 계속 통과해야 한다.
  - `SELECT o.order_id FROM orders o WHERE EXISTS (SELECT 1 FROM order_items i WHERE i.product_id = o.order_id)`
  - `SELECT order_id FROM orders WHERE customer_id IN (SELECT seller_id FROM order_items)`
  - `SELECT o.order_id, (SELECT i.price FROM order_items i WHERE i.product_id = o.order_id LIMIT 1) AS p FROM orders o`
- 등식이 아닌 상관 조건(`<`, `>` 등)은 이번에 다루지 않는다. 지금 동작을 보고서에 적는다.
- `_check_joins` docstring의 "FROM a, b WHERE ... 는 거르지 않는다" 주석은 현재 동작과 다르다. 콤마 조인은 `ON`이 없어 이미 거절된다(직접 돌려 확인함). 주석을 실제 동작에 맞게 고친다.
- 2-1이 같은 함수를 쓰므로 `t2s sql`에도 같이 적용된다. 양쪽 테스트를 모두 돌린다.

### 2-3. [definitions, mcp, plugin] sensitivity 전달

- 플러그인은 `sensitivity`로 `t2s values`를 거절하고(`cli.py:272`) `t2s sql`의 데이터셋 하위 쿼리에서 해당 필드를 뺀다(`measures.py:61`). 그런데 `semantic-definitions/graphdb/plan.py`는 이 속성을 게시하지 않고, MCP 스냅샷에도 없다.
- 지금 Olist 정의와 플러그인 테스트 정의에는 `sensitivity`를 쓴 필드가 없다. 지금 새는 값은 없다. 테스트는 픽스처에 민감 필드를 넣어서 만든다.
- definitions: Field 노드에 `sensitivity`를 게시한다. 값이 Ossie 필드의 최상위에 있는지 TEXT2SQL 확장에 있는지 플러그인 규칙(`f.get("sensitivity") or ext(f).get("sensitivity")`)과 같게 읽는다. 값이 없으면 속성을 만들지 않는다.
- mcp: 스냅샷 Field에 `sensitivity`를 포함한다. 값이 있을 때만 TEXT2SQL 확장 칸(`nonportable_extensions`)에 넣는다.
- plugin: "스냅샷을 왕복해도 sensitivity가 유지되고 `values`가 policy로 거절된다"는 테스트를 추가한다.
- `is_time`은 이번에 다루지 않는다. `plan.py:191`이 이미 게시하는 `is_time`은 자료형으로 계산한 값이고 기준이 플러그인(`ossie.TIME_TYPES`)과 다르다. 그대로 전달하면 `Time`·`DateTimeTz` 필드의 판단이 바뀐다. 이 속성은 건드리지 않는다.
- `synonyms`·`examples`는 실행에 필요 없으므로 넣지 않는다.
- 배포 메모(보고서에 적는다): `version_id`는 정의 파일 내용의 해시다. 게시 코드만 바꾸면 버전은 그대로이고, 다시 게시해야 그래프에 반영된다. 게시는 내가 한다.

### 2-4. [definitions, mcp] 소스 판별 규칙을 세 저장소에서 맞추기

- 기준은 플러그인의 `engine.SOURCE_NAME`(`engine.py:29`)이다. 앞뒤 공백을 뗀 소스가 이 식별자 정규식에 전체 일치하면 table, 아니면 sql_query이다. 플러그인은 고치지 않는다.
- 저장소끼리 import할 수 없다. 각 저장소에 같은 정규식을 두고, 출처가 플러그인이라는 주석을 단다. 두 저장소 테스트에 같은 사례 표를 넣어 어긋나면 드러나게 한다.
- definitions: `plan.py:209`, `plan.py:239`의 `startswith("SELECT")` 판별을 이 규칙으로 바꾼다. `tests/test_plan.py:20`의 도우미도 같이 바꾼다.
  - 판별만 바꾼다. "평평한 SELECT만 받는다"(`plan.py:242`)와 "이름은 두 단계까지"(`plan.py:212`) 제한은 그대로 둔다.
  - 그 결과 `WITH`·괄호·주석으로 시작하는 소스는 "잘못된 테이블 이름"이 아니라 "평평한 SELECT만 받는다"로 거절된다. 이 메시지 변화를 테스트로 고정한다.
- mcp: `get_dataset`(`graph_repository.py:137`)의 `startswith("SELECT")`를 같은 규칙으로 바꾼다. `source`가 없거나 비어 있으면 `GraphError("SCHEMA_INCOMPATIBLE", ...)`를 낸다.
- 테스트(두 저장소 공통 사례): `WITH`로 시작하는 소스, 괄호로 시작하는 소스, 주석으로 시작하는 소스, 소문자 `select`, `table`, `schema.table`, 세 단계 이름. mcp에는 null과 빈 문자열을 더한다.

### 2-5. [definitions, mcp] 지표-필드 role 보존

- `plan.edge()`(`plan.py:28`)는 (지표, 필드)당 role을 하나만 남긴다(measure > condition > join). 그래서 값으로도 쓰이고 조인 키로도 쓰이는 필드는 join이 사라진다.
- definitions: 엣지에 `roles` 배열을 저장한다. 순서는 정해 둔다(measure, condition, join). 기존 `role`은 호환을 위해 대표 값으로 남긴다.
- mcp: `get_metric_lineage`와 `get_column_impact`가 `r.roles`를 읽고, 없으면 `[r.role]`로 대체한다. join이 포함되면 `row_set_candidates`나 `row_set_candidate`에도 반영한다. join과 다른 role이 같이 있으면 값 쪽(`value_columns`, `impact: value`)에도 남긴다.
- 테스트: roles가 있는 엣지, roles가 없는 옛 엣지, join과 measure가 같이 있는 엣지를 FakeTx로 확인한다.
- 다시 게시하기 전까지 그래프에는 `roles`가 없다. 대체 경로가 그때 쓰인다.

### 2-6. [plugin] Claude Code 토큰 경로

- `.mcp.json`은 `${user_config.graph_mcp_token}`을 쓰지만 `t2s`는 `TEXT2SQL_MCP_TOKEN`만 읽는다. README의 Claude Code 절에는 이 환경변수 안내가 없다.
- 공식 문서로 확인한 결과(2026-11-24, `code.claude.com/docs/en/plugins-reference.md`):
  - `CLAUDE_PLUGIN_OPTION_<KEY>`는 훅 프로세스에만 전달된다.
  - Claude가 Bash 도구로 실행하는 명령의 환경에는 플러그인 변수가 없다.
  - `sensitive: true` 값은 스킬·에이전트 본문에 치환되지 않는다.
- 따라서 `t2s`가 Claude Code 설정에서 토큰을 받을 문서화된 길이 없다. `graph_client` 코드는 바꾸지 않는다.
- README의 Claude Code 절에 "`graph_mcp_token` 설정과 별도로 셸에 `TEXT2SQL_MCP_TOKEN`을 설정해야 `t2s`가 동작한다"고 적는다. 값은 예시로도 적지 않는다.

---

## 3단계: 배포·운영

### 3-1. [mcp] 이미지 빌드

- Dockerfile에서 `uv.lock`을 복사하고 `uv sync --frozen --no-dev`로 설치한다. 비루트 사용자 실행은 유지한다.
  - `uv sync`는 `/app/.venv`에 설치한다. `CMD`의 `uvicorn`이 잡히게 `PATH`에 `/app/.venv/bin`을 넣는다.
  - 프로젝트를 편집 가능 모드로 설치하지 않게 한다(`--no-editable`).
  - 비루트 사용자가 `.venv`를 읽을 수 있어야 한다.
  - 이미지 안에 uv를 넣는 방법과 버전은 uv 공식 문서를 확인해서 정한다. 확인하지 못하면 "확인 필요"로 남긴다.
- `.github/workflows/publish-image.yml`:
  - build/push 전에 `uv sync --frozen`과 단위 테스트를 실행하고, 실패하면 게시하지 않는다.
  - `paths`에 `uv.lock`과 `tests/**`를 추가한다. 지금은 lock만 바꾸면 이미지가 새로 만들어지지 않는다.

### 3-2. [plugin] t2s 설치 고정

- `scripts/t2s`는 `uv pip install "$ROOT"`라서 sqlglot 외 의존성을 매번 새로 해석한다.
- `uv export --frozen --no-dev --no-hashes --no-emit-project`로 만든 lock 파일을 저장소에 둔다. `--no-emit-project`가 없으면 프로젝트 자신도 목록에 들어간다.
- 설치는 두 단계로 한다. lock 파일로 의존성을 먼저 설치하고, 프로젝트는 의존성 해석 없이(`--no-deps`) 설치한다.
- lock 파일은 1-4에서 `mcp` 의존성을 뺀 뒤의 상태로 만든다.
- 환경 키(`cksum`) 계산에 lock 파일을 포함한다.
- `scripts/build_plugin.py`의 FILES에 lock 파일을 추가한다. `tests/test_package.py`의 기대 파일 목록도 같이 고친다.
- lock 파일을 갱신하는 방법을 README에 적는다.

### 3-3. [mcp] 오류와 관측성

- `_read()`(`graph_repository.py:36`)의 오류를 예외 클래스가 아니라 Neo4j 오류 코드로 나눈다. neo4j 6.4.1에서 인증 실패(`AuthError`)와 문법 오류(`CypherSyntaxError`)는 둘 다 `ClientError`라서 클래스로는 못 나눈다.
  - `Neo.ClientError.Statement.*`, `Neo.ClientError.Schema.*`는 `SCHEMA_INCOMPATIBLE`이다.
  - 인증·권한(`Neo.ClientError.Security.*`), 연결 실패, 시간 초과, 일시 오류는 `DB_UNAVAILABLE`이다.
  - 그 밖은 지금처럼 `DB_UNAVAILABLE`이다.
  - 트랜잭션 시간 초과가 어떤 코드로 오는지는 드라이버 공식 문서로 확인한다. 확인하지 못하면 "확인 필요"로 남긴다.
- `logging`으로 예외를 남기되 비밀값과 쿼리 매개변수 값은 로그에 넣지 않는다.
- `/readyz`는 만들지 않는다. `/healthz`는 지금처럼 단순 응답을 유지하고, Railway 점검 경로도 `/healthz` 그대로 둔다.
- `docs/railway.md`에 DB 연결 상태를 확인하는 방법을 적는다. 토큰을 넣어 `GET /snapshot`을 부르고, 200이면 정상, 503이면 Neo4j에 붙지 못한 것이다.
- 플러그인 쪽 오류 메시지 구분은 1-4에서 한다.
- 테스트: 문법 오류 코드, 인증 오류 코드, 연결 실패가 각각 맞는 `GraphError` 코드로 바뀌는지, 로그에 매개변수 값이 없는지 확인한다.

### 3-4. [mcp] 검색 정렬

- `search_definitions`(`graph_repository.py:107`)는 `ORDER BY ref,id`라서 정확히 일치하는 항목이 limit에 밀려 잘릴 수 있다. ref 정확 일치, name 정확 일치, 그 밖의 부분 일치 순으로 정렬한다. 비교는 지금 검색처럼 대소문자를 무시한다. 같은 순위 안에서는 지금처럼 `ref,id` 순이다.
- 테스트: 부분 일치가 limit보다 많을 때 정확 일치가 첫 줄에 오는지 확인한다.

---

## 4단계: 정리

### 4-1. [plugin] 도달 불가 코드

- `cli._model()`과 `cli._load()`는 실패하면 종료하므로 None을 반환하지 않는다. CLI에서 도달할 수 없는 "정의 없음" 분기를 정리한다.
  - `guard.policy()`의 DB 전체 허용 분기 (`guard.py:54`)
  - `cli._values`의 정의 없는 경로 (`cli.py:275`)
  - `cli._lint_inputs`의 정의 없는 경로 (`cli.py:148`)
  - `cli._sql`의 `if not model` (`cli.py:465`)
  - 이 함수들의 `SemanticModel | None` 타입 표기와 docstring
- **`cli._load()`는 지우지 않는다.** `--definition` 옵션이 `cli.py:233`, `cli.py:434`에서 쓴다.
- 테스트나 다른 모듈이 이 분기를 쓰면 지우지 말고 사용처와 함께 보고한다. 지금 테스트는 `policy(None, model)`처럼 모델을 준 호출만 쓴다.
- `t2s/concepts.py`는 어디에서도 import하지 않는다. 사용처가 없음을 확인한 뒤 삭제한다. `tests/test_package.py:22`의 기대 파일 목록에서도 뺀다.

### 4-2. [plugin] DB 클라이언트 훅

- `hooks/db_client.py:10` 정규식이 `/usr/bin/psql` 같은 절대 경로 호출을 잡도록 앞 문자 집합에 `/`와 백틱을 추가한다. `pgcli`, `mycli`, `clickhouse-client`도 추가한다.
- 이렇게 하면 `ls /var/lib/mysql`처럼 경로가 클라이언트 이름으로 끝나는 명령도 걸린다. 묻기만 하므로 허용하되, 테스트에 이 사례를 현재 동작으로 적어 둔다.
- 동작은 지금처럼 ask(확인 요청)만 하고 deny하지 않는다. `tests/test_hooks.py`에 사례를 추가한다.

### 4-3. [mcp, plugin] 문서·버전

- mcp:
  - `README.md` 도구 표에서 `graph_get_model_snapshot` 행(45행)을 빼고 `graph_find_join_path`를 넣는다. `/snapshot` 경로를 적는다.
  - `docs/graphdb-mcp-design.md`의 조회 도구 절(38행 포함)을 실제 구성에 맞게 고친다.
  - "변경 사항" 절에 0.3.0의 깨지는 변경(도구 제거, plugin 0.6.0 이상 필요)을 적는다.
- plugin:
  - `README.md:61`의 스냅샷 도구 설명을 `/snapshot` 경로 설명으로 바꾼다.
  - `t2s` 명령 표, `context` 출력 설명(`--datasets`), 토큰 설정(2-6)을 고친다.
  - "변경 사항" 절에 0.6.0의 깨지는 변경(`context`의 `catalog` 제거, MCP 0.3.0 이상 필요)을 적는다.
- 버전을 올린다. mcp는 `pyproject.toml` 0.3.0, plugin은 `pyproject.toml`, `plugin.json`, `.claude-plugin/plugin.json`, README 설치 경로 예시(35행)를 모두 0.6.0으로 맞춘다.

---

## 보고 형식 (단계마다)

1. 항목별 결과 표: 항목 번호 | 변경 파일 | 추가·수정한 테스트 | 내가 커밋할 때 쓸 메시지 제안(한국어, `type(scope): 요약`)
2. 테스트 결과: 저장소별 passed/failed/skipped 수 (위 기준값과 비교)
3. 측정값: 1단계에서는 `t2s context` 출력 크기의 변경 전후 비교
4. 문서와 실제 코드가 달랐던 곳, 추측하지 않고 남겨 둔 "확인 필요" 항목
5. 다음 단계로 넘어가기 전에 내가 결정해야 할 것
