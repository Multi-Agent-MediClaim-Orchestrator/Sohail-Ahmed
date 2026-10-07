"""One command for the whole system: prerequisites, containers, databases, knowledge base, both CrewAI crews, the
two-sided claim run and the evaluation report.

    make demo                  # real local model (Ollama gemma4 + nomic-embed-text), about 10 minutes per claim
    make demo-offline          # no model at all: same pipeline with rules / stand-in models, about 2-3 minutes
    make demo-check            # prerequisites only
    make demo-down             # stop the demo's background services and all containers (data volumes are kept)

    DEMO_SCENARIO=all make demo-offline   # every claim scenario (auto, reject, queries, callbacks, ...); default: auto
    DEMO_ORCH=n8n make demo               # insurer verification sequenced by the insurer's n8n flows (default: inline)
    uv run python scripts/demo.py --offline --no-containers   # infrastructure already running (no Docker on this machine)

Each step is idempotent, so a failed run can simply be started again. Logs: .e2e-logs/demo/<step>.log."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / ".e2e-logs" / "demo"
COMPOSE_FILES = ["docker-compose.base.yml", "shared.yml", "hospital.yml", "insurer.yml", "ai.yml"]
HOST_PORTS = {8010: "hospital crew", 8100: "hospital api", 8200: "doc-pipeline", 8300: "vision", 8400: "rag-service",
              8500: "tpa simulator", 8600: "insurer api", 8610: "insurer crew"}  # fmt: skip
RESULTS: list[tuple[str, bool, float, str]] = []
FAILURE: list[str] = []  # the failing step's full message, repeated after the summary table


class StepFailed(Exception):
    pass


def env_file() -> dict[str, str]:
    p = ROOT / ".env"
    out: dict[str, str] = {}
    if p.exists():
        for ln in p.read_text().splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def say(msg: str) -> None:
    print(msg, flush=True)


def sh(name: str, cmd: list[str], env: dict[str, str] | None = None, *, stream: bool = False, timeout: float | None = None,
       ok_codes: tuple[int, ...] = (0,)) -> str:  # fmt: skip
    """Run a command from the repo root; output goes to .e2e-logs/demo/<name>.log (and the console when ``stream``)."""
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{name}.log"
    full = {**os.environ, "CREWAI_TELEMETRY_OPT_OUT": "true", "OTEL_SDK_DISABLED": "true", **(env or {})}
    with log.open("w") as f:
        p = subprocess.Popen(cmd, cwd=ROOT, env=full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert p.stdout is not None
        for line in p.stdout:
            f.write(line)
            if stream:
                print("    " + line, end="", flush=True)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            raise StepFailed(f"{' '.join(cmd)} timed out; see {log.relative_to(ROOT)}") from None
    if rc not in ok_codes:
        tail = "".join(log.read_text().splitlines(keepends=True)[-15:])
        raise StepFailed(f"`{' '.join(cmd)}` failed (exit {rc}); last lines of {log.relative_to(ROOT)}:\n{tail}")
    return log.read_text()


def step(title: str, fn: Callable[[], str | None]) -> None:
    say(f"\n==> {title}")
    t = time.time()
    try:
        note = fn() or ""
    except StepFailed as e:
        RESULTS.append((title, False, time.time() - t, str(e).splitlines()[0]))
        FAILURE.append(f"{title}: {e}")
        say(f"    FAILED: {e}")
        raise
    RESULTS.append((title, True, time.time() - t, note))
    say(f"    ok ({time.time() - t:.0f}s){'  ' + note if note else ''}")


# ------------------------------------------------------------------------------------------------ prerequisites
def memory_gb() -> float | None:
    try:
        if platform.system() == "Linux":
            for ln in Path("/proc/meminfo").read_text().splitlines():
                if ln.startswith("MemTotal:"):
                    return int(ln.split()[1]) / 1024 / 1024
        if platform.system() == "Darwin":
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)) / 1024**3
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def port_busy(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def container_ports() -> dict[int, tuple[str, str | None]]:
    """Host ports the containers publish: {port: (what, .env key that moves it, if any)}."""
    e = env_file()

    def p(key: str, default: int) -> int:
        try:
            return int(e.get(key) or default)
        except ValueError:
            return default

    return {p("HOSP_DB_PORT", 5432): ("hospital Postgres", "HOSP_DB_PORT"), p("INS_DB_PORT", 5453): ("insurer Postgres", "INS_DB_PORT"),
            p("SHARED_REDIS_PORT", 6379): ("Redis", "SHARED_REDIS_PORT"), p("SHARED_MINIO_PORT", 9000): ("MinIO", "SHARED_MINIO_PORT"),
            p("SHARED_MINIO_CONSOLE_PORT", 9001): ("MinIO console", "SHARED_MINIO_CONSOLE_PORT"), 3310: ("ClamAV", None),
            8080: ("Keycloak", None), 9090: ("Keycloak health", None), p("HOSP_N8N_PORT", 5688): ("hospital n8n", "HOSP_N8N_PORT"),
            6333: ("Qdrant", None)}  # fmt: skip


def ours_running() -> set[int]:
    """Ports already held by this project's own containers (a re-run is fine)."""
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"], capture_output=True, text=True, timeout=10).stdout  # noqa: S603, S607
    except (OSError, subprocess.SubprocessError):
        return set()
    held: set[int] = set()
    e = env_file()
    for line in out.splitlines():
        name, _, ports = line.partition("\t")
        if not name.startswith("claims-"):
            continue
        for part in ports.split(","):
            if "->" in part:
                try:
                    held.add(int(part.split("->")[0].rsplit(":", 1)[1]))
                except (ValueError, IndexError):
                    pass
        if "hospital-n8n" in name:  # host networking: no published ports to read
            held.add(int(e.get("HOSP_N8N_PORT") or 5688))
    return held


def who_listens(port: int) -> str:
    if shutil.which("lsof") is None:
        return "another program"
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.splitlines()  # noqa: S603, S607
    if len(out) < 2:
        return "another program"
    cols = out[1].split()
    return f"`{cols[0]}` (pid {cols[1]})"


def ollama_url() -> str:
    return env_file().get("OLLAMA_HOST") or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"


def required_models() -> list[str]:
    e = env_file()
    names = [e.get("HOSP_LLM_LOCAL_MODEL") or "gemma4:latest", e.get("INS_CREW_MODEL") or "gemma4:latest",
             e.get("RAG_EMBED_MODEL") or "nomic-embed-text:latest"]  # fmt: skip
    return list(dict.fromkeys(names))


def preflight(offline: bool, containers: bool = True) -> list[str]:
    """Problems that stop the demo (empty list = ready). Warnings are printed, not returned."""
    problems: list[str] = []
    tools = [("uv", "the Python services"), ("make", "the existing recipes")]
    if containers:
        tools.insert(0, ("docker", "containers (Postgres, Redis, MinIO, ClamAV, Keycloak, n8n, Qdrant)"))
    for tool, why in tools:
        if shutil.which(tool) is None:
            problems.append(f"`{tool}` is not installed (needed for {why})")
    if not containers:
        e = env_file()
        for name, url in (("Keycloak", "http://localhost:8080/realms/hospital"), ("hospital n8n", f"http://localhost:{e.get('HOSP_N8N_PORT', '5688')}/healthz")):
            try:
                httpx.get(url, timeout=3).raise_for_status()
            except httpx.HTTPError:
                problems.append(f"--no-containers: {name} is not reachable at {url}")
    elif shutil.which("docker"):
        if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:  # noqa: S603, S607
            problems.append("Docker is installed but not running: start Docker Desktop (or the docker service)")
        elif subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:  # noqa: S603, S607
            problems.append("`docker compose` (v2) is missing")
    gb = memory_gb()
    if gb is not None and gb < 15:
        say(f"    warning: {gb:.0f} GB RAM; the plan assumes 16 GB (32 GB preferred). Close other apps; demo-offline needs less.")
    busy = {p: n for p, n in HOST_PORTS.items() if port_busy(p)}
    if busy:
        problems.append("ports already in use (an earlier run still up? `make demo-down`): "
                        + ", ".join(f"{p} ({n})" for p, n in busy.items()))  # fmt: skip
    if containers and shutil.which("docker"):
        held = ours_running()
        for port, (what, key) in container_ports().items():
            if port in held or not port_busy(port):
                continue
            fix = f"stop it, or set {key}=<free port> in .env" if key else "stop it (this port is fixed)"
            problems.append(f"port {port} ({what}) is already used by {who_listens(port)} on this machine: {fix}")
    if not offline:
        try:
            tags = httpx.get(f"{ollama_url()}/api/tags", timeout=3).json()
            have = {m["name"] for m in tags.get("models", [])}
            missing = [m for m in required_models() if m not in have and f"{m}:latest" not in have]
            if missing:
                say(f"    Ollama models to pull: {', '.join(missing)} (done in the next step)")
        except (httpx.HTTPError, ValueError):
            problems.append(f"Ollama is not reachable at {ollama_url()}: start it (`ollama serve`), or run `make demo-offline`")
    return problems


def pull_models() -> str:
    tags = httpx.get(f"{ollama_url()}/api/tags", timeout=5).json()
    have = {m["name"] for m in tags.get("models", [])}
    pulled = []
    for m in required_models():
        if m in have or f"{m}:latest" in have:
            continue
        say(f"    pulling {m} (first time only; several GB) ...")
        with httpx.stream("POST", f"{ollama_url()}/api/pull", json={"model": m}, timeout=httpx.Timeout(3600, connect=10)) as r:
            last = ""
            for line in r.iter_lines():
                status = json.loads(line).get("status", "") if line else ""
                if status != last:
                    say(f"      {status}")
                    last = status
        pulled.append(m)
    return f"pulled {', '.join(pulled)}" if pulled else "all models present: " + ", ".join(required_models())


# ------------------------------------------------------------------------------------------------ rag-service
def rag_env(offline: bool) -> dict[str, str]:
    e = env_file()
    common = {"QDRANT_URL": "http://localhost:6333", "QDRANT_API_KEY": e.get("QDRANT_API_KEY", ""), "JWT_SECRET": e.get("RAG_JWT_SECRET", ""),
              "RAG_PORT": e.get("RAG_PORT", "8400"), "DOCPIPE_URL": "", "LLM_GATEWAY_URL": "http://localhost:11434", "LLM_GATEWAY_KEY": "ollama"}  # fmt: skip
    if offline:  # in-memory store and hash embedder, seeded at start: no Qdrant data, no embedding model
        return {**common, "STORE": "memory", "EMBEDDER": "hash", "RAG_SEED_ON_START": "1", "DB_URL": str(LOGS / "rag-offline.db")}
    return {**common, "STORE": "qdrant", "EMBEDDER": "gateway", "EMBED_ALIAS": e.get("RAG_EMBED_MODEL", "nomic-embed-text:latest"),
            "CHAT_ALIAS": e.get("RAG_CHAT_MODEL", "gemma4:latest"), "CHAT_REASONING_EFFORT": "none", "DB_URL": str(ROOT / ".e2e-logs" / "rag.db")}  # fmt: skip


def start_rag(offline: bool) -> str:
    env = rag_env(offline)
    if offline:
        (LOGS / "rag-offline.db").unlink(missing_ok=True)  # memory store starts empty: its metadata must too
    port = env["RAG_PORT"]
    log = (LOGS / "rag-service.log").open("w")
    p = subprocess.Popen(["uv", "run", "uvicorn", "rag_service.main:app", "--port", port, "--log-level", "warning"],  # noqa: S603, S607
                         cwd=ROOT / "services/rag-service", env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)  # fmt: skip
    (LOGS / "rag-service.pid").write_text(str(p.pid))
    for _ in range(120):
        if p.poll() is not None:
            raise StepFailed(f"rag-service exited; see {(LOGS / 'rag-service.log').relative_to(ROOT)}")
        try:
            if httpx.get(f"http://localhost:{port}/health", timeout=2).status_code == 200:
                return f"http://localhost:{port} ({'in-memory, hash embeddings' if offline else 'Qdrant, nomic-embed-text'})"
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise StepFailed("rag-service did not become healthy in 120 s")


def stop_rag() -> None:
    pid_file = LOGS / "rag-service.pid"
    if pid_file.exists():
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGTERM)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
        pid_file.unlink(missing_ok=True)


# ------------------------------------------------------------------------------------------------ the run
def e2e_env(offline: bool) -> dict[str, str]:
    return {"E2E_LLM": "rules" if offline else "ollama", "E2E_CREW": "offline" if offline else "ollama",
            "E2E_ORCH": os.environ.get("DEMO_ORCH", "inline"), "E2E_SCENARIO": os.environ.get("DEMO_SCENARIO", "auto"),
            "HOSP_RAG_URL": f"http://localhost:{env_file().get('RAG_PORT', '8400')}"}  # fmt: skip


def run_claims(offline: bool) -> str:
    out = sh("e2e-full", ["uv", "run", "python", "scripts/e2e_full.py"], e2e_env(offline), stream=True)
    if "FULL E2E PASSED" not in out:
        raise StepFailed("the claim run did not pass; see .e2e-logs/demo/e2e-full.log and .e2e-logs/*.log")
    return "FULL E2E PASSED"


def evaluate() -> str:
    """Exit code 1 from the harness means "some metric is below its target"; the report says which, and so do we."""
    out = sh("eval", ["uv", "run", "python", "eval/run_eval.py", "--n", "300", "--seed", "42", "--out", str(LOGS / "eval")], ok_codes=(0, 1))
    runs = sorted((LOGS / "eval").glob("*/report.md"))
    if not runs:
        raise StepFailed(f"no report written:\n{out[-500:]}")
    report = runs[-1]
    say("\n" + "\n".join("    " + ln for ln in report.read_text().splitlines()))
    metrics = json.loads((report.parent / "metrics.json").read_text())
    rows = metrics if isinstance(metrics, list) else metrics.get("metrics", [])
    below = [m["name"] for m in rows if m.get("pass") is False]
    return f"{report.relative_to(ROOT)}" + (f"; below target: {', '.join(below)}" if below else "; all metrics on target")


def compose_down() -> None:
    files = [a for f in COMPOSE_FILES for a in ("-f", f"infra/compose/{f}")]
    profiles = [a for p in ("infra", "hospital", "insurer", "n8n", "rag") for a in ("--profile", p)]
    sh("down", ["docker", "compose", "--env-file", ".env", *files, *profiles, "down", "--remove-orphans"])


def mk(*targets: str, done: str = "done") -> Callable[[], str]:
    """A step that runs Makefile targets in order (each logged to .e2e-logs/demo/<target>.log)."""

    def run() -> str:
        for t in targets:
            sh(t, ["make", "-s", t])
        return done

    return run


def up_n8n() -> str:
    """The n8n containers use host networking. Docker Desktop (Mac, Windows) runs containers in a VM, so unless host
    networking is switched on there, n8n is healthy inside but unreachable from this machine; catch that here."""
    sh("up-n8n", ["make", "-s", "up-n8n"])
    url = f"http://localhost:{env_file().get('HOSP_N8N_PORT', '5688')}/healthz"
    for _ in range(30):
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return "up"
        except httpx.HTTPError:
            pass
        time.sleep(2)
    hint = (" On Docker Desktop: Settings -> Resources -> Network -> tick 'Enable host networking', Apply & restart, then run"
            " the demo again." if platform.system() in ("Darwin", "Windows") else "")
    raise StepFailed(f"hospital n8n is running but not reachable at {url}.{hint}")


def init() -> str:
    sh("init-secrets", ["make", "-s", "init-secrets"])
    sh("uv-sync", ["uv", "sync", "--all-packages"])
    return "ready"


def plan(offline: bool, containers: bool = True) -> list[tuple[str, Callable[[], str | None]]]:
    orch = os.environ.get("DEMO_ORCH", "inline")
    steps: list[tuple[str, Callable[[], str | None]]] = [("Settings and packages (.env secrets, uv sync incl. CrewAI)", init)]
    if not offline:
        steps.append(("Ollama models", pull_models))
    if containers:
        steps += [
            ("Infrastructure containers (Postgres, Redis, MinIO, ClamAV, Keycloak)", mk("up-infra", done="up")),
            ("Insurer database container", mk("up-insurer", done="up")),
            ("Hospital n8n (13 flows)", up_n8n),
        ]
    if orch == "n8n" and containers:
        steps.append(("Insurer n8n (19 flows)", mk("up-insurer-n8n", done="up")))
    steps.append(("Hospital database: migrate and seed demo users", mk("migrate", "seed")))
    if not offline:
        steps += [
            *([("Qdrant vector database", mk("up-rag", done="up"))] if containers else []),
            ("Knowledge base: policy wordings into Qdrant (nomic-embed-text)", mk("seed-kb", done="seeded")),
        ]
    steps += [
        ("rag-service (policy wording search for both crews)", lambda: start_rag(offline)),
        ("Claims end to end: hospital ClaimFlow -> insurer CrewAI agents -> decision -> settlement", lambda: run_claims(offline)),
        ("Evaluation report", evaluate),
    ]
    return steps


def summary(offline: bool, ok: bool) -> None:
    say("\n" + "=" * 100)
    for title, good, secs, note in RESULTS:
        say(f"  {'PASS' if good else 'FAIL'}  {title:<88} {secs:6.0f}s  {note[:60]}")
    say("=" * 100)
    say(f"{'DEMO PASSED' if ok else 'DEMO FAILED'} ({'offline: no model' if offline else 'real local model'}); logs in {LOGS.relative_to(ROOT)}")
    for f in FAILURE:
        say(f"\nWhat failed:\n{f}")
    if ok:
        say("Next: `make crew-demo` / `make ins-crew-demo` (CrewAI flows alone), `make crew-plot` (flow diagrams),"
            " `make demo-down` (stop everything).")  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="no model: rules and stand-in models")
    ap.add_argument("--check", action="store_true", help="prerequisites only")
    ap.add_argument("--down", action="store_true", help="stop background services and containers")
    ap.add_argument("--dry-run", action="store_true", help="print the steps without running them")
    ap.add_argument("--no-containers", action="store_true", help="infrastructure is already running; skip Docker")
    a = ap.parse_args()
    os.chdir(ROOT)
    if a.down:
        stop_rag()
        compose_down()
        say("stopped rag-service and all containers (volumes kept; `make nuke` deletes the data)")
        return 0
    if a.dry_run:
        say(f"make demo{'-offline' if a.offline else ''} would run:")
        for i, (title, _) in enumerate(plan(a.offline, not a.no_containers), 1):
            say(f"  {i:2d}. {title}")
        say(f"  e2e settings: {e2e_env(a.offline)}")
        return 0
    say(f"Multi-agent claim processing demo ({'offline, no model' if a.offline else 'real local model via Ollama'})")
    if not (ROOT / ".env").exists():
        shutil.copy(ROOT / ".env.example", ROOT / ".env")
    say("\n==> Prerequisites")
    problems = preflight(a.offline, not a.no_containers)
    if problems:
        say("    not ready:\n" + "\n".join(f"      - {p}" for p in problems))
        return 2
    say("    ok")
    if a.check:
        return 0
    ok = True
    try:
        for title, fn in plan(a.offline, not a.no_containers):
            step(title, fn)
    except StepFailed:
        ok = False
    except KeyboardInterrupt:
        ok = False
        say("\ninterrupted")
    finally:
        stop_rag()
        summary(a.offline, ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
