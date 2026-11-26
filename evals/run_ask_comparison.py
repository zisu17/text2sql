"""Run two ask skills on the study's frozen questions against the same local Graph.

The model can call the Graph tools and submit queries through a local t2s tool.
The tool sends back diagnostics and column labels. Result rows stay in the
question's local attempt log and are read only for grading.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

GRAPH_TOOLS = (
    "graph_list_models", "graph_search_definitions", "graph_get_dataset",
    "graph_get_metric_lineage", "graph_list_join_rules", "graph_find_join_path",
)
MAX_ATTEMPTS = 4  # Initial submission and the three corrections allowed by ask.
RESULT = re.compile(r"(?:^|\n)RESULT ([1-4])\s*$")
IDENTIFIER = re.compile(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?")
CHECK_ROUTES = ("context", "schema", "validate", "values")
USER_CONFIRMATION = (
    "이 질문에는 평가 도구에 연결된 읽기 전용 로컬 DB를 사용해 주세요. 이 DB를 데이터 출처로 지정합니다. "
    "답에 사용할 쿼리는 도구로 실행해 주세요. 결과 행은 로컬 채점기가 확인하므로 답변에 값을 적을 필요는 없습니다."
)
BOOT = """
import json, os
from pathlib import Path
from t2s import cli
snapshot = json.loads(Path(os.environ["ASK_EVAL_SNAPSHOT"]).read_text())
def fixed_snapshot(model):
    if model not in (None, snapshot["model"]):
        raise cli.GraphClientError("Unexpected model in this evaluation")
    return snapshot
cli.fetch_model = fixed_snapshot
cli.main()
"""


def read_records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def append_record(path, result):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False) + "\n")


def arguments(route, sql, metrics, by, where, order, limit, expr, read, preview):
    if route in ("sql", "run", "validate"):
        if not sql or not sql.strip():
            raise ValueError(f"Provide a SELECT for {route}")
        return [route, "-", *([f"--read={read}"] if read else [])], sql
    if route != "compile" or not metrics or not all(IDENTIFIER.fullmatch(m) for m in metrics):
        raise ValueError("Use sql or run with a SELECT, or compile with registered metric names")
    args = ["compile", *([] if preview else ["--run"]), *metrics]
    for name, values in (("by", by), ("where", where), ("order", order)):
        args.extend(f"--{name}={value}" for value in values or [])
    if limit is not None:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        args.append(f"--limit={limit}")
    if expr:
        args.append(f"--expr={expr}")
    return args, None


def call_t2s(case, args, sql=None):
    env = {
        **os.environ, "PYTHONPATH": os.pathsep.join((case["plugin"], *case["extra_pythonpath"])),
        "ASK_EVAL_SNAPSHOT": case["snapshot"], "GRAPH_MODEL": case["model"],
        "GRAPH_VERSION_ID": case["version"], "DATABASE_URL": f"sqlite:///file:{case['db']}?mode=ro&uri=true",
        "QUERY_TIMEOUT": "10", "MAX_ROWS": "1000", "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("SEMANTIC_MODEL", "DB_SCHEMA", "SQL_DIALECT", "T2S_LOG"):
        env.pop(name, None)
    proc = subprocess.run([case["runtime"], "-c", BOOT, *args], env=env, cwd=case["plugin"],
                          input=sql, text=True, capture_output=True, timeout=55)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Local t2s did not return JSON") from error


def execution_result(payload):
    result = payload.get("result", payload)
    return {
        "ok": bool(result.get("ok") and not result.get("truncated")),
        "columns": result.get("columns"), "rows": result.get("rows"),
        "truncated": bool(result.get("truncated")), "error": result.get("error"),
        "warnings": payload.get("warnings", []),
    }


def model_result(result):
    # A DB result row must never be included in MCP tool output.
    output = {k: result[k] for k in ("attempt", "ok", "columns", "error", "truncated")}
    output["warnings"] = [w["kind"] for w in result["warnings"] if "kind" in w]
    return output


def tool_error(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def check_result(case, route, args, sql, tables, datasets):
    if route == "values":
        return tool_error("values_unavailable", "Use the question and Graph definitions to confirm values in this evaluation.")
    if route == "context":
        if any(not IDENTIFIER.fullmatch(name) for name in datasets or []):
            raise ValueError("Provide registered dataset names")
        args = ["context"]
        if datasets and case["scoped_context"]:
            args.append("--datasets=" + ",".join(datasets))
        payload = call_t2s(case, args)
        result = {key: payload[key] for key in ("dialect", "schema", "max_rows", "readonly_guarantee", "definition", "tables", "error") if key in payload}
        if datasets and case["scoped_context"] and "catalog" in payload:
            result["catalog"] = payload["catalog"]
        elif "error" not in result:
            result["message"] = "Use graph_get_dataset for selected semantic definitions."
        if "error" not in result:
            result["database_source"] = "The user selected the connected read-only local database."
        return result
    if route == "schema":
        if any(not IDENTIFIER.fullmatch(name) for name in tables or []):
            raise ValueError("Provide table names without options")
        payload = call_t2s(case, ["schema", *(tables or [])])
        return {key: payload[key] for key in ("tables", "describe", "outside_definition", "error") if key in payload}
    payload = call_t2s(case, args, sql)
    keys = ("ok", "sql", "problems", "error", "warnings", "joins", "filters", "group_by")
    return {key: payload[key] for key in keys if key in payload}


def serve():
    from mcp.server.mcpserver import MCPServer

    case = json.loads(Path(os.environ["ASK_EVAL_CASE"]).read_text())
    attempts = Path(case["attempts"])
    checks = Path(case["checks"])
    server = MCPServer("local-t2s")
    lock = Lock()

    @server.tool(structured_output=True)
    def t2s(route: str, sql: str | None = None, metrics: list[str] | None = None,
            by: list[str] | None = None, where: list[str] | None = None, order: list[str] | None = None,
            limit: int | None = None, expr: str | None = None, read: str | None = None,
            tables: list[str] | None = None, datasets: list[str] | None = None,
            preview: bool = False) -> dict[str, Any]:
        """Inspect context, schema or a query, or execute t2s compile, sql or run locally."""
        if route in ("compile --run", "compile_run"):
            route, preview = "compile", False
        with lock:
            try:
                args, query = ([], None) if route in ("context", "schema", "values") else arguments(
                    route, sql, metrics, by, where, order, limit, expr, read, preview)
            except ValueError as error:
                append_record(checks, {"route": route, "issue": "bad_args"})
                return tool_error("bad_args", str(error))
            if route in CHECK_ROUTES or (route == "compile" and preview):
                try:
                    result = check_result(case, route, args, query, tables, datasets)
                except ValueError as error:
                    result = tool_error("bad_args", str(error))
                except subprocess.TimeoutExpired:
                    result = tool_error("timeout", "Local metadata check timed out")
                error = result.get("error") or {}
                append_record(checks, {"route": route, "issue": error.get("code") or error.get("kind")})
                return result
            index = len(read_records(attempts)) + 1
            if index > MAX_ATTEMPTS:
                return tool_error("attempt_limit", "Use the best completed attempt.")
            try:
                result = execution_result(call_t2s(case, args, query))
            except subprocess.TimeoutExpired:
                result = execution_result(tool_error("timeout", "Local execution timed out"))
            result.update(attempt=index, route=route)
            append_record(attempts, result)
        return model_result(result)

    server.run(transport="stdio")


def add_server(command, name, config):
    for key, value in config.items():
        command[-1:-1] = ["-c", f"mcp_servers.{name}.{key}={json.dumps(value)}"]


def generate(args, item, plugin, snapshot, provider, prompt):
    graph_name = "text2sql-graph"
    with tempfile.TemporaryDirectory(prefix="olist-ask-") as folder:
        folder = Path(folder)
        work = folder / "work"
        work.mkdir()
        instructions = folder / "instructions.txt"
        skill = (plugin / "skills/ask/SKILL.md").read_text()
        instructions.write_text(
            skill
            + "\n사용자가 연결된 읽기 전용 로컬 DB를 지정했다. 데이터 출처는 확인되었으므로 다시 묻지 않는다. "
              "Graph 모델은 이미 정해져 있다. 제공된 Graph MCP 도구로 필요한 정의를 직접 찾는다. "
              "DDL을 실제 DB 스키마로 사용한다. t2s 명령은 local-t2s의 t2s 도구로 호출한다. "
              "context의 datasets와 schema의 tables는 이름 목록으로 준다. context의 전체 catalog는 제공하지 않는다. "
              "context, schema, validate와 compile의 preview는 점검이다. 필터 값은 질문과 Graph 정의에서 확인한다. "
              "compile --run은 preview 없이 호출한다. compile --run, sql, run만 질의 실행 횟수로 센다. "
              "이 도구는 각 실행의 열 이름과 오류를 돌려준다. 요청한 출력과 대조하고 필요하면 같은 도구로 고쳐 실행한다. "
              "마지막 실행 결과를 답에 사용한다. 앞선 실행을 선택할 경우에만 마지막 줄에 RESULT 1처럼 그 번호를 적는다. "
              "리뷰가 필요하면 같은 대화에서 직접 점검한다. DB 결과의 값은 로컬에서 처리된다. "
              "결과 값을 보려고 SQL을 바꾸거나 실행 결과를 다른 형태로 감싸지 않는다.\n"
        )
        case_path = folder / "case.json"
        attempts = folder / "attempts.jsonl"
        checks = folder / "checks.jsonl"
        case_path.write_text(json.dumps({
            "runtime": str(args.runtime), "snapshot": str(args.snapshot), "db": str(args.study / "data/olist.db"),
            "plugin": str(plugin), "model": snapshot["model"], "version": snapshot["version_id"],
            "extra_pythonpath": args.extra_pythonpath, "attempts": str(attempts), "checks": str(checks),
            "scoped_context": "context --datasets" in skill,
        }))
        command = provider.command(work, instructions)
        add_server(command, graph_name, {
            "url": args.graph_url, "bearer_token_env_var": "TEXT2SQL_MCP_TOKEN",
            "enabled_tools": GRAPH_TOOLS, "required": True, "startup_timeout_sec": 25, "tool_timeout_sec": 30,
        })
        add_server(command, "local-t2s", {
            "command": str(args.tool_runtime), "args": [str(Path(__file__).resolve()), "--tool"],
            "required": True, "startup_timeout_sec": 25, "tool_timeout_sec": 65,
            "env.ASK_EVAL_CASE": str(case_path),
        })
        for server, tools in ((graph_name, GRAPH_TOOLS), ("local-t2s", ("t2s",))):
            for tool in tools:
                command[-1:-1] = ["-c", f'mcp_servers.{server}.tools.{tool}.approval_mode="approve"']
        env = {**os.environ, "ASK_EVAL_CASE": str(case_path)}
        for key in ("DATABASE_URL", "OPENAI_API_KEY", "NEO4J_PASSWORD", "NEO4J_AUTH", "MCP_API_TOKEN"):
            env.pop(key, None)
        confirmed_prompt = f"{prompt}\n\n{USER_CONFIRMATION}"
        proc = subprocess.run(command, input=confirmed_prompt, env=env, text=True, capture_output=True, timeout=args.timeout)
        events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        if proc.returncode or not any(e["type"] == "turn.completed" for e in events):
            failures = [str(e.get("error") or e.get("message") or "") for e in events
                        if e.get("type") in ("error", "turn.failed")]
            detail = (failures or proc.stderr.strip().splitlines() or [""])[-1]
            if token := env.get("TEXT2SQL_MCP_TOKEN"):
                detail = detail.replace(token, "[redacted]")
            raise RuntimeError(f"Model process incomplete ({proc.returncode}): {detail[-500:]}")
        messages = [e["item"]["text"] for e in events if e["type"] == "item.completed"
                    and e.get("item", {}).get("type") == "agent_message"]
        seen = [e["item"] for e in events if e["type"] == "item.completed"
                and e.get("item", {}).get("type") == "mcp_tool_call" and e["item"]["server"] == graph_name]
        for call in seen:
            result = call.get("result") or {}
            content = result.get("structuredContent") or result.get("structured_content")
            if not content and not result.get("isError"):
                block = next((b for b in result.get("content", []) if b["type"] == "text"), None)
                content = json.loads(block["text"]) if block else None
            if content and not result.get("isError") and "version_id" in content:
                if (content.get("version_id"), content.get("model")) != (snapshot["version_id"], snapshot["model"]):
                    raise RuntimeError("Graph tools returned a different active version")
        match = RESULT.search(messages[-1]) if messages else None
        local_results = read_records(attempts)
        picked = local_results[-1] if local_results else None
        if match:
            picked = next((r for r in local_results if r["attempt"] == int(match[1])), None)
        return {
            "id": item["id"], "attempts": local_results, "final": picked,
            "checks": read_records(checks),
            "graph_calls": len(seen), "prompt_sha256": sha256(confirmed_prompt.encode()).hexdigest(),
            **({"no_execution_reply": messages[-1]} if not local_results and messages else {}),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("study", "old-plugin", "new-plugin", "snapshot", "out"):
        parser.add_argument(f"--{name}", type=lambda value: Path(value).resolve(), required=True)
    for name in ("runtime", "tool-runtime"):
        # Preserve the venv entry point even when it is a symlink to the base Python.
        parser.add_argument(f"--{name}", type=lambda value: Path(value).expanduser().absolute(), required=True)
    parser.add_argument("--graph-url", required=True)
    parser.add_argument("--extra-pythonpath", action="append", type=lambda value: str(Path(value).expanduser().absolute()), default=[])
    parser.add_argument("--ids", nargs="+")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    url = urlsplit(args.graph_url)
    if url.scheme != "http" or url.hostname != "127.0.0.1":
        parser.error("The benchmark Graph endpoint must be on the local loopback interface")
    sys.path.insert(0, str(args.study / "scripts"))
    from codex_provider import CodexGenerator
    from common_pipeline import answer_free
    from run_baseline import build_prompt, compare
    from run_pipeline_suite import select_inputs

    snapshot = json.loads(args.snapshot.read_text())
    frozen = json.loads((args.study / "evals/combined/manifest.v0.3.json").read_text())
    db = args.study / "data/olist.db"
    assert sha256(db.read_bytes()).hexdigest() == frozen["db_sha256"]
    gold_path = args.study / frozen["dataset"]
    assert sha256(gold_path.read_bytes()).hexdigest() == frozen["dataset_sha256"]
    gold = {r["id"]: r for r in json.loads(gold_path.read_text())}
    provider = CodexGenerator("gpt-6-luna", "medium")
    provider_info = provider.preflight()
    provider_info["tool_policy"] = "allowlisted local Graph and t2s tools; DB result rows withheld"
    items = select_inputs(args.ids)
    plugins = {"nov22": args.old_plugin, "updated": args.new_plugin}
    identity = {"provider": provider_info, "version": snapshot["version_id"], "db": frozen["db_sha256"],
                "ids": [i["id"] for i in items], "max_attempts": MAX_ATTEMPTS,
                "sqlite": subprocess.check_output([str(args.runtime), "-c", "import sqlite3; print(sqlite3.sqlite_version)"], text=True).strip(),
                "source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
                "plugins": {label: sha256(b"".join(p.read_bytes() for p in sorted(plugin.glob("t2s/*.py")))
                                          + (plugin / "skills/ask/SKILL.md").read_bytes()).hexdigest()
                            for label, plugin in plugins.items()}}
    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != identity:
        raise RuntimeError("Checkpoint configuration differs")
    manifest_path.write_text(json.dumps(identity, ensure_ascii=False, indent=2))
    path = args.out / "results.jsonl"
    completed = {(r["arm"], r["id"]): r for r in read_records(path)}
    jobs = [(arm, item) for index, item in enumerate(items)
            for arm in (tuple(plugins) if index % 2 == 0 else tuple(reversed(plugins)))
            if (arm, item["id"]) not in completed]

    def run(job):
        arm, item = job
        try:
            record = generate(args, item, plugins[arm], snapshot, provider, build_prompt(answer_free(item)))
            attempts = record.pop("attempts")
            final = record.pop("final")
            def grade(result):
                if result and result["ok"]:
                    return compare(gold[item["id"]], result["columns"], result["rows"])
                error = (result or {}).get("error") or {}
                return False, error.get("code") or error.get("kind") or "no_executed_result"
            record.update(arm=arm, first=grade(attempts[0] if attempts else None), score=grade(final),
                          attempts=len(attempts), final_attempt=(final or {}).get("attempt"),
                          attempt_scores=[{"route": attempt["route"], "score": grade(attempt)} for attempt in attempts])
            return record
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as error:
            return {"arm": arm, "id": item["id"], "infrastructure_error": type(error).__name__, "reason": str(error)[:550]}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed(pool.submit(run, job) for job in jobs):
            record = future.result()
            if record.get("infrastructure_error"):
                append_record(args.out / "infrastructure-errors.jsonl", record)
            else:
                append_record(path, record)
                completed[(record["arm"], record["id"])] = record
            print(len(completed), "/", len(items) * 2, record["arm"], record["id"],
                  record.get("infrastructure_error") or record["score"][0], flush=True)
    if len(completed) != len(items) * 2:
        raise SystemExit("Incomplete run. Rerun the same command to resume.")
    for arm in plugins:
        group = [r for (label, _), r in completed.items() if label == arm]
        print(arm, "first", sum(r["first"][0] for r in group), "final", sum(r["score"][0] for r in group),
              "/", len(group), flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--tool"]:
        serve()
    else:
        main()
