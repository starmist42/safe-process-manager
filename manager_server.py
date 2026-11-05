"""
manager_server.py — Local Process Manager (precise PID binding)

关键修复：
  · 不再每次轮询都全盘模糊搜索；启动时记录 Popen / 首次发现到的 PID + 创建时间
  · 状态只看“已绑定 PID”是否还活着（PID 复用用 create_time 校验）
  · 绑定的进程死了就是 Stopped，不会被后来同名的进程“顶替”成 Running
  · 安全红线不变：软关闭 → 缓冲期 → 超时只 Warning；只有人工 confirm 才 kill

安全：
  · 自身 PID 永不匹配、永不强杀
  · 只有显式 /api/force_kill + confirm=true 才执行 kill
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import psutil
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "manager_config.json"
LOG_FILE = BASE / "manager.log"
PORT = 8791

app = FastAPI(title="Local Process Manager")
OWN_PID = os.getpid()
MENTION = ("manager_server.py",)
SAFE_NAMES = {"powershell.exe", "pwsh.exe", "conhost.exe", "explorer.exe", "windowsterminal.exe"}

STATE: dict[str, dict] = {}
LOGS: deque = deque(maxlen=600)
_seq = 0


def log(msg: str) -> None:
    global _seq
    _seq += 1
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    LOGS.append({"seq": _seq, "time": datetime.now().isoformat(timespec="seconds"), "msg": msg})
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def load_cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"services": []}


def save_cfg(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def svc_by_id(sid: str) -> dict | None:
    for s in load_cfg().get("services", []):
        if s.get("id") == sid:
            return s
    return None


def state_of(sid: str) -> dict:
    return STATE.setdefault(sid, {
        "status": "Stopped",
        "warning": None,
        "bound": {},       # pid -> create_time（精准绑定）
        "discovered": False,
    })


def is_alive(pid: int, created: float | None = None) -> bool:
    try:
        p = psutil.Process(pid)
        if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
            return False
        if created is not None and abs(p.create_time() - created) > 1.5:
            return False       # PID 被复用，不能算同一个进程
        return True
    except Exception:
        return False


def bind(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except Exception:
        return None


def discover_pids(svc: dict) -> list[int]:
    """仅在“从未绑定过”或“刚启动服务”时使用一次，之后只认绑定 PID。"""
    kws = [str(k).lower() for k in svc.get("keywords", []) if k]
    pids: list[int] = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = p.info["pid"]
            if pid == OWN_PID:
                continue
            name = p.info.get("name") or ""
            if name.lower() in SAFE_NAMES:
                continue
            argv = p.info.get("cmdline") or []
            cmd = " ".join(argv[:3])
            text = f"{name} {cmd}".lower()
            if any(m in text for m in MENTION):
                continue
            if any(k in text for k in kws):
                pids.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def ensure_binding(svc: dict) -> list[int]:
    """返回当前绑定的存活 PID；第一次调用时做一次发现并绑定。"""
    st = state_of(svc["id"])
    alive = []
    for pid, created in list(st["bound"].items()):
        if is_alive(pid, created):
            alive.append(pid)
        else:
            st["bound"].pop(pid, None)
    if alive:
        return alive

    if not st["discovered"]:
        st["discovered"] = True
        found = discover_pids(svc)
        for pid in found:
            ct = bind(pid)
            if ct is not None:
                st["bound"][pid] = ct
        if found:
            log(f"{svc['name']}：首次发现并绑定 PID {found}")
        return [p for p in found if p in st["bound"]]
    return []


def proc_info(pid: int) -> dict | None:
    try:
        p = psutil.Process(pid)
        return {
            "pid": pid,
            "name": p.name(),
            "cpu": round(p.cpu_percent(interval=0.1), 1),
            "mem_mb": round(p.memory_info().rss / 1024 / 1024, 1),
            "cmd": " ".join(p.cmdline())[:180],
        }
    except Exception:
        return None


def wal_diag(work_dir: str) -> list[dict]:
    out: list[dict] = []
    try:
        wd = Path(work_dir)
        if not wd.exists():
            return out
        for f in list(wd.glob("*.db-wal")) + list(wd.glob("*/*.db-wal")):
            try:
                st = f.stat()
                if st.st_size > 0:
                    out.append({
                        "file": str(f),
                        "mb": round(st.st_size / 1024 / 1024, 2),
                        "age_sec": int(time.time() - st.st_mtime),
                    })
            except Exception:
                continue
    except Exception:
        pass
    return out


def diagnostics(svc: dict, pids: list[int]) -> dict:
    procs = [i for i in (proc_info(p) for p in pids) if i]
    return {
        "processes": procs,
        "wal": wal_diag(svc.get("work_dir") or ""),
        "at": datetime.now().isoformat(timespec="seconds"),
    }


def _send_wm_close(pid: int) -> bool:
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        WM_CLOSE = 0x0010
        hit: list[int] = []
        SKIP_CLASSES = {"ConsoleWindowClass", "PseudoConsoleWindow",
                        "CASCADIA_HOSTING_WINDOW_CLASS", "Windows.UI.Core.CoreWindow"}

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _lp):
            wpid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf, 256)
                if buf.value not in SKIP_CLASSES:
                    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                    hit.append(hwnd)
            return True

        user32.EnumWindows(cb, 0)
        return bool(hit)
    except Exception:
        return False


def graceful_close_group(pids: list[int]) -> dict[int, str]:
    methods: dict[int, str] = {}
    windowed = []
    for pid in pids:
        if _send_wm_close(pid):
            methods[pid] = "WM_CLOSE"
            windowed.append(pid)
    if windowed:
        for pid in pids:
            methods.setdefault(pid, "wait-for-parent")
        return methods
    for pid in pids:
        try:
            psutil.Process(pid).terminate()
            methods[pid] = "terminate"
        except Exception:
            methods[pid] = "failed"
    return methods


def service_view(svc: dict) -> dict:
    st = state_of(svc["id"])
    pids = ensure_binding(svc)
    if pids:
        st["pids"] = pids
        if st["status"] != "Warning":
            st["status"] = "Running"
    else:
        st["pids"] = []
        if st["status"] not in ("Warning", "Stopping"):
            st["status"] = "Stopped"
    procs = [i for i in (proc_info(p) for p in pids) if i]
    candidates = [] if pids else sorted(discover_pids(svc))
    return {
        "id": svc["id"],
        "name": svc.get("name", svc["id"]),
        "status": st["status"],
        "pids": pids,
        "bound": sorted(st["bound"].keys()),
        "cpu": round(sum(p["cpu"] for p in procs), 1),
        "mem_mb": round(sum(p["mem_mb"] for p in procs), 1),
        "warning": st.get("warning"),
        "timeout": svc.get("graceful_timeout", 15),
        "candidates": candidates,
    }


@app.get("/api/services")
def api_services() -> JSONResponse:
    return JSONResponse({
        "services": [service_view(s) for s in load_cfg().get("services", [])],
        "manager_pid": OWN_PID,
        "ts": datetime.now().isoformat(timespec="seconds"),
    })


@app.get("/api/status")
def api_status() -> JSONResponse:
    return api_services()


async def _watch(svc: dict, pids: list[int], timeout: int) -> None:
    st = state_of(svc["id"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(is_alive(p, st["bound"].get(p)) for p in pids):
            st["status"] = "Stopped"
            st["warning"] = None
            st["bound"].clear()
            log(f"{svc['name']}：已优雅退出（PID {pids}）")
            return
        await asyncio.sleep(0.5)

    alive = [p for p in pids if is_alive(p, st["bound"].get(p))]
    diag = diagnostics(svc, alive)
    st["status"] = "Warning"
    st["warning"] = {"pids": alive, "diag": diag, "since": datetime.now().isoformat(timespec="seconds")}
    wal = f"，检测到 {len(diag['wal'])} 个 WAL 文件在写" if diag["wal"] else ""
    log(f"{svc['name']}：PID {alive} 超时未退出{wal}，已挂起等待人工处理（不会自动强杀）")


@app.post("/api/stop")
async def api_stop(req: Request) -> JSONResponse:
    body = await req.json()
    svc = svc_by_id(body.get("id", ""))
    if not svc:
        return JSONResponse({"ok": False, "error": "unknown service"}, status_code=404)
    pids = ensure_binding(svc)
    if not pids:
        st = state_of(svc["id"])
        st["status"] = "Stopped"
        return JSONResponse({"ok": True, "message": "已经是停止状态"})
    methods = graceful_close_group(pids)
    st = state_of(svc["id"])
    st["status"] = "Stopping"
    st["pids"] = pids
    timeout = int(svc.get("graceful_timeout", 15))
    log(f"{svc['name']}：发送软关闭 {methods}，等待 {timeout}s 缓冲期")
    asyncio.create_task(_watch(svc, pids, timeout))
    return JSONResponse({"ok": True, "pids": pids, "methods": methods, "timeout": timeout})


@app.post("/api/force_kill")
async def api_force_kill(req: Request) -> JSONResponse:
    body = await req.json()
    if body.get("confirm") is not True:
        return JSONResponse({"ok": False, "error": "必须显式确认强杀"}, status_code=400)
    svc = svc_by_id(body.get("id", ""))
    if not svc:
        return JSONResponse({"ok": False, "error": "unknown service"}, status_code=404)
    st = state_of(svc["id"])
    pids = set((st.get("warning") or {}).get("pids") or []) | set(ensure_binding(svc))
    killed, skipped = [], []
    for pid in pids:
        if pid == OWN_PID:
            skipped.append(pid)
            continue
        try:
            psutil.Process(pid).kill()
            killed.append(pid)
        except Exception:
            skipped.append(pid)
    # 等一小会儿再核实，避免误报“已杀”
    if os.name == "nt" and killed:
        await asyncio.sleep(0.4)
    still = [p for p in killed if is_alive(p)]
    if still:
        st["status"] = "Warning"
        st["warning"] = {"pids": still, "diag": diagnostics(svc, still),
                         "since": datetime.now().isoformat(timespec="seconds")}
        log(f"{svc['name']}：强杀后仍有进程存活 {still}（可能受保护/权限不足）")
        return JSONResponse({"ok": False, "error": f"仍有进程存活：{still}", "killed": killed, "skipped": skipped}, status_code=409)
    st["status"] = "Stopped"
    st["warning"] = None
    st["bound"].clear()
    log(f"{svc['name']}：人工终审强杀 → {killed}（跳过 {skipped}）")
    return JSONResponse({"ok": True, "killed": killed, "skipped": skipped})


@app.post("/api/start")
async def api_start(req: Request) -> JSONResponse:
    body = await req.json()
    svc = svc_by_id(body.get("id", ""))
    if not svc:
        return JSONResponse({"ok": False, "error": "unknown service"}, status_code=404)
    if ensure_binding(svc):
        return JSONResponse({"ok": True, "message": "已经在运行"})
    st = state_of(svc["id"])
    try:
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            svc["start_cmd"], cwd=svc.get("work_dir") or str(BASE),
            shell=True, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # 拉起后最多 5 秒内发现真实子进程并绑定（shell/cmd 的 PID 不算）
        deadline = time.time() + 5
        found: list[int] = []
        while time.time() < deadline and not found:
            await asyncio.sleep(0.4)
            found = discover_pids(svc)
        for pid in found:
            ct = bind(pid)
            if ct is not None:
                st["bound"][pid] = ct
        st["discovered"] = True
        st["status"] = "Running" if found else "Stopped"
        log(f"{svc['name']}：已拉起并绑定 PID {found or '（未发现，可能启动失败）'}")
        return JSONResponse({"ok": True, "pids": found, "message": "启动命令已执行"})
    except Exception as exc:
        log(f"{svc['name']}：启动失败 {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/sleep_all")
async def api_sleep_all() -> JSONResponse:
    results = []
    for svc in load_cfg().get("services", []):
        pids = ensure_binding(svc)
        if not pids:
            results.append({"id": svc["id"], "name": svc.get("name"), "pids": [], "note": "already stopped"})
            continue
        methods = graceful_close_group(pids)
        st = state_of(svc["id"])
        st["status"] = "Stopping"
        st["pids"] = pids
        timeout = int(svc.get("graceful_timeout", 15))
        asyncio.create_task(_watch(svc, pids, timeout))
        results.append({"id": svc["id"], "name": svc.get("name"), "pids": pids, "methods": methods, "timeout": timeout})
    log(f"一键安全休眠：已对所有服务发送软关闭（{len(results)} 个）")
    return JSONResponse({"ok": True, "results": results})


@app.post("/api/rebind")
async def api_rebind(req: Request) -> JSONResponse:
    """绑定的进程死了但系统里有同名进程时，由用户手动重新绑定。"""
    body = await req.json()
    svc = svc_by_id(body.get("id", ""))
    if not svc:
        return JSONResponse({"ok": False, "error": "unknown service"}, status_code=404)
    st = state_of(svc["id"])
    found = discover_pids(svc)
    for pid in found:
        ct = bind(pid)
        if ct is not None:
            st["bound"][pid] = ct
    st["discovered"] = True
    st["status"] = "Running" if st["bound"] else "Stopped"
    st["warning"] = None
    log(f"{svc['name']}：人工重新绑定 PID {sorted(st['bound'].keys())}")
    return JSONResponse({"ok": True, "pids": sorted(st["bound"].keys())})


@app.get("/api/logs")
def api_logs(after: int = 0) -> JSONResponse:
    return JSONResponse({"logs": [x for x in LOGS if x["seq"] > after]})



@app.post("/api/remove_service")
async def api_remove_service(req: Request) -> JSONResponse:
    """从配置中移除服务；只改 manager_config.json，不碰任何本地文件。"""
    body = await req.json()
    sid = body.get("id") or body.get("service_id") or ""
    svc = svc_by_id(sid)
    if not svc:
        return JSONResponse({"ok": False, "error": "unknown service"}, status_code=404)

    # 安全闸：只要还能找到活着的匹配进程，就拒绝移除
    live = ensure_binding(svc)
    if not live:
        live = discover_pids(svc)
    if live:
        return JSONResponse(
            {"ok": False, "error": "请先安全关闭该服务，再执行移除操作。"},
            status_code=400,
        )

    cfg = load_cfg()
    before = len(cfg.get("services", []))
    cfg["services"] = [s for s in cfg.get("services", []) if s.get("id") != sid]
    save_cfg(cfg)
    STATE.pop(sid, None)
    log(f"已从配置移除服务：{svc.get('name', sid)}（仅删除配置项）")
    return JSONResponse({"ok": True, "removed": sid, "remaining": len(cfg["services"]), "before": before})


@app.get("/api/config")
def api_config() -> JSONResponse:
    return JSONResponse(load_cfg())


@app.post("/api/config")
async def api_add_config(req: Request) -> JSONResponse:
    body = await req.json()
    name = (body.get("name") or "").strip()
    keyword = (body.get("keyword") or "").strip()
    start_cmd = (body.get("start_cmd") or "").strip()
    if not name or not keyword or not start_cmd:
        return JSONResponse({"ok": False, "error": "名称 / 进程关键词 / 启动命令都不能为空"}, status_code=400)
    cfg = load_cfg()
    sid = (body.get("id") or name).strip().replace(" ", "-")[:32]
    if any(s.get("id") == sid for s in cfg.get("services", [])):
        sid = f"{sid}-{int(time.time()) % 10000}"
    cfg.setdefault("services", []).append({
        "id": sid,
        "name": name,
        "keywords": [keyword],
        "start_cmd": start_cmd,
        "work_dir": (body.get("work_dir") or str(BASE)).strip(),
        "graceful_timeout": int(body.get("graceful_timeout") or 15),
    })
    save_cfg(cfg)
    log(f"新增受管服务：{name}（关键词 {keyword}）")
    return JSONResponse({"ok": True, "id": sid})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE / "index.html")


def main() -> int:
    if sys.stdout is None:
        fh = open(BASE / "manager_stdout.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = fh
        sys.stderr = fh
    import uvicorn

    log(f"进程管家启动（v2 精准绑定），自身 PID={OWN_PID}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
