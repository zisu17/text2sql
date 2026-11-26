# Text2SQL `ask` 비교 평가

`run_ask_comparison.py`는 같은 Olist v0.3 문항과 게시된 Graph 버전에서 두 플러그인 소스의 `ask` 지침과 `t2s` 실행기를 비교한다. 모델에게 질문과 필수 조건, 같은 SQLite DDL을 제공한다. 사용자 메시지에는 연결된 읽기 전용 DB를 사용한다는 출처 확인을 포함한다. 모델이 Graph에서 필요한 정의를 검색한다.

로컬 MCP 도구가 `t2s context`, `schema`, `validate`, `compile` 미리보기를 지원한다. `context`는 접속 정보와 테이블 목록을 제공하며 지원 버전에서 데이터셋을 지정하면 그 범위의 정의 요약도 제공한다. 저장값과 분포는 모델에 보내지 않는다. 모델은 `compile --run`, `sql`, `run`의 오류와 결과 열 이름을 보고 최초 실행 뒤 최대 세 번 수정할 수 있다. 준비 단계의 확인은 실행 횟수에서 제외한다. 정답은 로컬에서 채점에 사용한다. DB 결과 행은 로컬 임시 기록에 저장하며 모델 응답과 최종 결과 파일에는 담지 않는다. 기본 채점 대상은 마지막 실행 결과다. 모델이 다른 실행을 명시하면 그 결과를 채점한다. 실행 결과가 없거나 채점 대상 실행에 실패했으면 오답이다.

로컬 Graph MCP를 실행한 뒤 같은 버전의 인증된 `/snapshot` JSON을 준비한다. 두 실행기는 이 스냅샷을 읽으므로 0.5.1과 0.6.1의 스냅샷 전송 프로토콜 차이는 비교에 들어가지 않는다. Graph 인증에는 환경 변수 `TEXT2SQL_MCP_TOKEN`을 사용한다.

```sh
python3 evals/run_ask_comparison.py \
  --study /path/to/text-to-sql-study \
  --old-plugin /path/to/older-plugin-source \
  --new-plugin ./plugin \
  --snapshot /path/to/snapshot.json \
  --graph-url http://127.0.0.1:8000/mcp \
  --runtime ./plugin/.venv/bin/python \
  --tool-runtime ./mcp/.venv/bin/python \
  --extra-pythonpath ./mcp/.venv/lib/python3.12/site-packages \
  --out /tmp/olist-ask-eval
```

`--ids` 옵션으로 일부 문항만 점검할 수 있다. 실행이 끊기면 같은 `--out`으로 다시 시작한다. 실행 설정이 달라지면 다른 출력 디렉터리를 쓴다. `results.jsonl`에 최초·최종 채점과 시도 횟수가 남으며 `manifest.json`에 평가 조건이 기록된다.
