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


def arguments(route, sql, metrics, by, where, order, limit, expr):
    if route in ("sql", "run") and isinstance(sql, str) and sql.strip():
        return [route, "-"], sql
    if route != "compile" or not metrics or not all(re.fullmatch(r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)?", m) for m in metrics):
        raise ValueError("Use sql or run with a SELECT, or compile with registered metric names")
    args = ["compile", "--run", *metrics]
    for name, values in (("by", by), ("where", where), ("order", order)):
        args.extend(f"--{name}={value}" for value in values or [])
    if limit is not None:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        args.append(f"--limit={limit}")
    if expr:
        args.append(f"--expr={expr}")
    return args, None


def execute(case, args, sql):
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
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Local t2s did not return JSON") from error
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


def serve():
    from mcp.server.mcpserver import MCPServer

    case = json.loads(Path(os.environ["ASK_EVAL_CASE"]).read_text())
    attempts = Path(case["attempts"])
    server = MCPServer("local-t2s")
    lock = Lock()

    @server.tool(structured_output=True)
    def t2s(route: str, sql: str | None = None, metrics: list[str] | None = None,
            by: list[str] | None = None, where: list[str] | None = None, order: list[str] | None = None,
            limit: int | None = None, expr: str | None = None) -> dict[str, Any]:
        """Run t2s compile, sql, or run locally and return output column labels or diagnostics."""
        with lock:
            index = len(read_records(attempts)) + 1
            if index > MAX_ATTEMPTS:
                return {"ok": False, "error": {"code": "attempt_limit", "message": "Use the best completed attempt."}}
            try:
                args, query = arguments(route, sql, metrics, by, where, order, limit, expr)
                result = execute(case, args, query)
            except ValueError as error:
                result = {"ok": False, "error": {"code": "bad_args", "message": str(error)[:300]},
                          "columns": None, "rows": None, "warnings": [], "truncated": False}
            except subprocess.TimeoutExpired:
                result = {"ok": False, "error": {"code": "timeout", "message": "Local execution timed out"},
                          "columns": None, "rows": None, "warnings": [], "truncated": False}
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
        instructions.write_text(
            (plugin / "skills/ask/SKILL.md").read_text()
            + "\n평가 환경에서 DB와 모델은 이미 정해져 있다. 제공된 Graph MCP 도구로 필요한 정의를 직접 찾는다. "
              "DDL을 실제 DB 스키마로 사용한다. t2s compile --run, sql, run은 local-t2s의 t2s 도구로 호출한다. "
              "이 도구는 각 실행의 열 이름과 오류를 돌려준다. 요청한 출력과 대조하고 필요하면 같은 도구로 고쳐 실행한다. "
              "최종 답변은 선택한 실행 번호를 RESULT 1 같은 형식으로 마지막 줄에 적는다. "
              "리뷰가 필요하면 같은 대화에서 직접 점검한다. DB 결과의 값은 로컬에서 처리된다.\n"
        )
        case_path = folder / "case.json"
        attempts = folder / "attempts.jsonl"
        case_path.write_text(json.dumps({
            "runtime": str(args.runtime), "snapshot": str(args.snapshot), "db": str(args.study / "data/olist.db"),
            "plugin": str(plugin), "model": snapshot["model"], "version": snapshot["version_id"],
            "extra_pythonpath": args.extra_pythonpath, "attempts": str(attempts),
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
        proc = subprocess.run(command, input=prompt, env=env, text=True, capture_output=True, timeout=args.timeout)
        events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        if proc.returncode or not any(e["type"] == "turn.completed" for e in events):
            raise RuntimeError(f"Model process incomplete ({proc.returncode})")
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
        picked = next((r for r in local_results if match and r["attempt"] == int(match[1])), None)
        return {
            "id": item["id"], "attempts": local_results, "final": picked,
            "graph_calls": len(seen), "prompt_sha256": sha256(prompt.encode()).hexdigest(),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("study", "old-plugin", "new-plugin", "snapshot", "runtime", "tool-runtime", "out"):
        parser.add_argument(f"--{name}", type=lambda value: Path(value).resolve(), required=True)
    parser.add_argument("--graph-url", required=True)
    parser.add_argument("--extra-pythonpath", action="append", default=[])
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
                          attempts=len(attempts), final_attempt=(final or {}).get("attempt"))
            return record
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as error:
            return {"arm": arm, "id": item["id"], "infrastructure_error": type(error).__name__, "reason": str(error)[:200]}

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
