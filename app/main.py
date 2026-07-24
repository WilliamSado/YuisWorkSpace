from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import sqlite3
import time
import tomllib
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from croniter import croniter
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("CI_CONFIG", BASE.parent / "config.toml"))
DEVICES_PATH = Path(os.getenv("CI_DEVICES_CONFIG", BASE.parent / "devices.toml"))
DATA_DIR = Path(os.getenv("CI_DATA_DIR", BASE.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
with CONFIG_PATH.open("rb") as fh:
    config: dict[str, Any] = tomllib.load(fh)
with DEVICES_PATH.open("rb") as fh:
    devices_config: dict[str, Any] = tomllib.load(fh)

TZ = ZoneInfo(config.get("server", {}).get("timezone", "UTC"))
SECRET = os.getenv("CI_SECRET_KEY", "")
PASSWORD = os.getenv("CI_ADMIN_PASSWORD", "")
if not SECRET or not PASSWORD:
    raise RuntimeError("CI_SECRET_KEY and CI_ADMIN_PASSWORD must be set")

DB_PATH = DATA_DIR / "ci.sqlite3"
running: dict[int, asyncio.subprocess.Process] = {}
queue: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue()
next_runs: dict[str, datetime] = {}
telegram_monitors: dict[int, asyncio.Task[None]] = {}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS builds (
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, job_name TEXT NOT NULL,
          status TEXT NOT NULL, trigger TEXT NOT NULL, created_at TEXT NOT NULL,
          started_at TEXT, finished_at TEXT, exit_code INTEGER, log_path TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_builds_created ON builds(created_at DESC);
        """)
        conn.execute(
            "UPDATE builds SET status='interrupted', finished_at=? "
            "WHERE status IN ('queued','running','signing','uploading')",
            (now(),),
        )


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def jobs() -> list[dict[str, Any]]:
    return devices_config.get("devices", [])


def find_job(job_id: str) -> dict[str, Any]:
    job = next((j for j in jobs() if j.get("id") == job_id), None)
    if job:
        return job
    matches = [j for j in jobs() if j.get("enabled", True) and j.get("id", "").split("-")[-1] == job_id]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        choices = ", ".join(j["id"] for j in matches)
        raise HTTPException(409, f"设备对应多个任务，请指定：{choices}")
    raise HTTPException(404, "job not found")


def token_for(expiry: int) -> str:
    raw = str(expiry)
    sig = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def valid_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expiry, sig = token.split(".", 1)
    expected = hmac.new(SECRET.encode(), expiry.encode(), hashlib.sha256).hexdigest()
    return expiry.isdigit() and int(expiry) > time.time() and hmac.compare_digest(sig, expected)


def require_auth(request: Request) -> None:
    if not valid_token(request.cookies.get("ci_session")):
        raise HTTPException(401, "authentication required")


async def notify(message: str) -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        tg = config.get("telegram", {})
        tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if tg.get("enabled") and tg_token:
            for chat_id in tg.get("allowed_chat_ids", []):
                try:
                    await client.post(f"https://api.telegram.org/bot{tg_token}/sendMessage", json={"chat_id": chat_id, "text": message})
                except httpx.HTTPError:
                    pass
        qq = config.get("qq", {})
        if qq.get("enabled"):
            headers = onebot_headers()
            url = qq.get("onebot_api_url", "").rstrip("/")
            targets = [("send_private_msg", "user_id", x) for x in qq.get("allowed_user_ids", [])]
            targets += [("send_group_msg", "group_id", x) for x in qq.get("allowed_group_ids", [])]
            for action, key, target in targets:
                try:
                    await client.post(f"{url}/{action}", headers=headers, json={key: target, "message": message})
                except httpx.HTTPError:
                    pass


def onebot_headers() -> dict[str, str]:
    token = os.getenv("QQ_ONEBOT_ACCESS_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


async def enqueue(job: dict[str, Any], trigger: str) -> int:
    log_name = f"{int(time.time())}-{secrets.token_hex(3)}.log"
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO builds(job_id,job_name,status,trigger,created_at,log_path) VALUES(?,?,?,?,?,?)",
            (job["id"], job.get("name", job["id"]), "queued", trigger, now(), str(DATA_DIR / log_name)),
        )
        build_id = int(cur.lastrowid)
    await queue.put((build_id, job))
    return build_id


async def worker() -> None:
    while True:
        build_id, job = await queue.get()
        log_path = Path(row(build_id)["log_path"])
        try:
            with db() as conn:
                conn.execute("UPDATE builds SET status='running',started_at=? WHERE id=?", (now(), build_id))
            await notify(f"🚀 #{build_id} {job.get('name', job['id'])} 开始构建")
            with log_path.open("wb") as log:
                proc = await asyncio.create_subprocess_shell(
                    job["command"], cwd=job["workdir"], stdout=log,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                    executable="/bin/bash",
                )
                running[build_id] = proc
                try:
                    code = await asyncio.wait_for(proc.wait(), timeout=int(job.get("timeout_minutes", 360)) * 60)
                    status = "success" if code == 0 else "failed"
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, signal.SIGTERM)
                    await proc.wait()
                    code, status = -1, "timeout"
                if status == "success" and job.get("sign_command"):
                    with db() as conn:
                        conn.execute("UPDATE builds SET status='signing' WHERE id=?", (build_id,))
                    await notify(f"🔏 #{build_id} 编译完成，开始发布签名")
                    log.write(b"\n[ci] Build succeeded; starting signing command\n")
                    log.flush()
                    sign_proc = await asyncio.create_subprocess_shell(
                        job["sign_command"], cwd=job["workdir"], stdout=log,
                        stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                        executable="/bin/bash",
                    )
                    running[build_id] = sign_proc
                    try:
                        sign_code = await asyncio.wait_for(
                            sign_proc.wait(), timeout=int(job.get("sign_timeout_minutes", 180)) * 60,
                        )
                    except asyncio.TimeoutError:
                        os.killpg(sign_proc.pid, signal.SIGTERM)
                        await sign_proc.wait()
                        sign_code = -1
                    if sign_code != 0:
                        code, status = sign_code, "sign_failed"
                if status == "success" and job.get("post_success_command"):
                    with db() as conn:
                        conn.execute("UPDATE builds SET status='uploading' WHERE id=?", (build_id,))
                    completed_stage = "签名" if job.get("sign_command") else "构建"
                    await notify(f"📤 #{build_id} {completed_stage}完成，开始上传")
                    log.write(b"\n[ci] Build succeeded; starting post-success command\n")
                    log.flush()
                    post_proc = await asyncio.create_subprocess_shell(
                        job["post_success_command"], cwd=job["workdir"], stdout=log,
                        stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                        executable="/bin/bash",
                    )
                    running[build_id] = post_proc
                    try:
                        post_code = await asyncio.wait_for(
                            post_proc.wait(),
                            timeout=int(job.get("post_success_timeout_minutes", 180)) * 60,
                        )
                    except asyncio.TimeoutError:
                        os.killpg(post_proc.pid, signal.SIGTERM)
                        await post_proc.wait()
                        post_code = -1
                    if post_code != 0:
                        code, status = post_code, "upload_failed"
            # cancel_build updates the row before SIGTERM makes wait() return.
            if row(build_id)["status"] == "cancelled":
                status = "cancelled"
            with db() as conn:
                conn.execute("UPDATE builds SET status=?,finished_at=?,exit_code=? WHERE id=?", (status, now(), code, build_id))
            icon = "✅" if status == "success" else "❌"
            await notify(
                f"{icon} #{build_id} {job.get('name', job['id'])} "
                f"{labels_for_bot(status)}（退出码 {code}）"
            )
        except Exception as exc:
            with log_path.open("ab") as log:
                log.write(f"\n[ci internal error] {exc}\n".encode())
            with db() as conn:
                conn.execute("UPDATE builds SET status='failed',finished_at=?,exit_code=-2 WHERE id=?", (now(), build_id))
        finally:
            running.pop(build_id, None)
            queue.task_done()


async def scheduler() -> None:
    while True:
        current = datetime.now(TZ)
        for job in jobs():
            if not job.get("enabled", True) or not job.get("cron"):
                continue
            job_id = job["id"]
            target = next_runs.setdefault(job_id, croniter(job["cron"], current).get_next(datetime))
            if current >= target:
                await enqueue(job, "schedule")
                next_runs[job_id] = croniter(job["cron"], current).get_next(datetime)
        await asyncio.sleep(15)


def row(build_id: int) -> sqlite3.Row:
    with db() as conn:
        item = conn.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
    if not item:
        raise HTTPException(404, "build not found")
    return item


def bot_allowed(source: str, user_id: int, group_id: int | None = None) -> bool:
    section = config.get(source, {})
    return user_id in section.get("allowed_user_ids" if source == "qq" else "allowed_chat_ids", []) or (
        source == "qq" and group_id in section.get("allowed_group_ids", [])
    )


def build_progress(item: sqlite3.Row | dict[str, Any]) -> str:
    path = Path(item["log_path"])
    if not path.exists():
        return "等待日志输出"
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 256 * 1024))
        lines = fh.read().decode(errors="replace").splitlines()[-400:]
    percent = None
    stage = "正在构建"
    for line in reversed(lines):
        if percent is None:
            match = re.search(r"\[\s*(\d{1,3})%\s+\d+/\d+\]", line)
            if match:
                percent = min(int(match.group(1)), 100)
        clean = line.strip()
        if clean and not clean.startswith(("[", "FAILED:", "cd ")):
            stage = clean[-100:]
            if percent is not None:
                break
    return f"{percent}% · {stage}" if percent is not None else stage


def jobs_text() -> tuple[str, bool]:
    lines = ["🧰 构建任务"]
    has_running = False
    latest = {}
    with db() as conn:
        for item in conn.execute("SELECT * FROM builds ORDER BY id DESC"):
            latest.setdefault(item["job_id"], item)
    for job in jobs():
        item = latest.get(job["id"])
        schedule = job.get("cron", "手动")
        lines.append(f"\n• {job.get('name', job['id'])}\n  ID: {job['id']} · cron: {schedule}")
        if item:
            state = item["status"]
            if state in ("running", "signing", "uploading"):
                detail = build_progress(item)
                if item["started_at"]:
                    elapsed = max(0, int((datetime.now(TZ) - datetime.fromisoformat(item["started_at"])).total_seconds()))
                    detail += f" · 已运行 {elapsed // 3600}h {(elapsed % 3600) // 60}m"
            else:
                detail = labels_for_bot(state)
            lines.append(f"  #{item['id']} · {detail}")
            has_running |= state in ("queued", "running", "signing", "uploading")
    if has_running:
        lines.append(f"\n🔄 自动更新 · {datetime.now(TZ).strftime('%H:%M:%S')}")
    return "\n".join(lines), has_running


def labels_for_bot(status: str) -> str:
    return {"queued": "排队中", "running": "构建中", "signing": "签名中", "uploading": "上传中", "success": "构建、签名并上传成功", "failed": "构建失败", "sign_failed": "构建成功但签名失败", "upload_failed": "签名成功但上传失败", "timeout": "构建超时", "cancelled": "已取消", "interrupted": "已中断"}.get(status, status)


def wen_text(device: str) -> str:
    query = device.strip().lower()
    matched = [
        job for job in jobs()
        if job.get("enabled", True) and (
            query == job["id"].lower().split("-")[-1]
            or query == job["id"].lower()
            or query in job.get("name", "").lower()
        )
    ]
    if not matched:
        available = sorted({job["id"].split("-")[-1] for job in jobs() if job.get("enabled", True)})
        return f"没有找到设备 {device}\n可用设备：{' / '.join(available)}"

    lines = [f"📅 {device} 构建安排"]
    with db() as conn:
        for job in matched:
            cron = job.get("cron")
            next_run = croniter(cron, datetime.now(TZ)).get_next(datetime) if cron else None
            latest = conn.execute(
                "SELECT * FROM builds WHERE job_id=? ORDER BY id DESC LIMIT 1", (job["id"],)
            ).fetchone()
            lines.append(f"\n• {job.get('name', job['id'])}")
            lines.append(f"  下次：{next_run.strftime('%Y-%m-%d %H:%M')}" if next_run else "  下次：仅手动构建")
            if latest:
                state = build_progress(latest) if latest["status"] == "running" else labels_for_bot(latest["status"])
                lines.append(f"  最近：#{latest['id']} · {state}")
            else:
                lines.append("  最近：暂无构建记录")
    return "\n".join(lines)


async def bot_command(text: str, trigger: str, can_control: bool = True) -> str:
    parts = text.strip().split()
    cmd = parts[0].lstrip("/").split("@")[0] if parts else ""
    if cmd in ("start", "help"):
        return "命令：status｜jobs｜wen <设备>｜build <任务ID>｜cancel <构建ID>"
    if cmd == "jobs":
        return jobs_text()[0]
    if cmd == "wen" and len(parts) == 2:
        return wen_text(parts[1])
    if cmd == "status":
        with db() as conn:
            items = conn.execute("SELECT id,job_name,status FROM builds ORDER BY id DESC LIMIT 5").fetchall()
        return "\n".join(f"#{x['id']} {x['job_name']} · {x['status']}" for x in items) or "暂无构建"
    if cmd == "build" and len(parts) == 2:
        if not can_control:
            return "⛔ 只有群管理员可以触发构建"
        try:
            job = find_job(parts[1])
        except HTTPException as exc:
            return str(exc.detail)
        build_id = await enqueue(job, trigger)
        return f"已加入队列：#{build_id}"
    if cmd == "cancel" and len(parts) == 2 and parts[1].isdigit():
        if not can_control:
            return "⛔ 只有群管理员可以取消构建"
        return await cancel_build(int(parts[1]))
    return "未知命令，发送 help 查看帮助"


async def telegram_poller() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not config.get("telegram", {}).get("enabled") or not token:
        return
    offset = 0
    async with httpx.AsyncClient(timeout=40) as client:
        while True:
            try:
                res = await client.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"offset": offset, "timeout": 30})
                for update in res.json().get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    if chat_id in config.get("telegram", {}).get("allowed_chat_ids", []) and msg.get("text"):
                        text = msg["text"]
                        if not text.lstrip().startswith("/"):
                            continue
                        chat_type = msg.get("chat", {}).get("type", "private")
                        can_control = True
                        first_word = text.split()[0] if text.split() else ""
                        if chat_type in ("group", "supergroup") and first_word.split("@")[0] in ("/build", "/cancel"):
                            try:
                                member = await client.get(
                                    f"https://api.telegram.org/bot{token}/getChatMember",
                                    params={"chat_id": chat_id, "user_id": msg.get("from", {}).get("id")},
                                )
                                can_control = member.json().get("result", {}).get("status") in ("administrator", "creator")
                            except (httpx.HTTPError, ValueError):
                                can_control = False
                        answer = await bot_command(text, "telegram", can_control)
                        sent = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": answer})
                        command = text.split()[0].lstrip("/").split("@")[0]
                        if command == "jobs" and jobs_text()[1]:
                            old = telegram_monitors.pop(chat_id, None)
                            if old:
                                old.cancel()
                            message_id = sent.json().get("result", {}).get("message_id")
                            if message_id:
                                telegram_monitors[chat_id] = asyncio.create_task(
                                    monitor_jobs(client, token, chat_id, message_id)
                                )
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(5)


async def monitor_jobs(client: httpx.AsyncClient, token: str, chat_id: int, message_id: int) -> None:
    try:
        while True:
            await asyncio.sleep(20)
            message, active = jobs_text()
            try:
                await client.post(
                    f"https://api.telegram.org/bot{token}/editMessageText",
                    json={"chat_id": chat_id, "message_id": message_id, "text": message},
                )
            except httpx.HTTPError:
                pass
            if not active:
                break
    finally:
        if telegram_monitors.get(chat_id) is asyncio.current_task():
            telegram_monitors.pop(chat_id, None)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    tasks = [asyncio.create_task(worker()), asyncio.create_task(scheduler()), asyncio.create_task(telegram_poller())]
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="YuisWorkSpace", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE / "static" / "index.html")


@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    body = await request.json()
    if not hmac.compare_digest(str(body.get("password", "")), PASSWORD):
        raise HTTPException(401, "密码错误")
    response = JSONResponse({"ok": True})
    response.set_cookie("ci_session", token_for(int(time.time()) + 86400 * 7), httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return response


@app.post("/api/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie("ci_session")
    return response


@app.get("/api/dashboard")
async def dashboard(request: Request) -> dict[str, Any]:
    require_auth(request)
    with db() as conn:
        builds = [dict(x) for x in conn.execute("SELECT * FROM builds ORDER BY id DESC LIMIT 50")]
    public_jobs = []
    for job in jobs():
        public_jobs.append({k: job.get(k) for k in ("id", "name", "cron", "enabled", "timeout_minutes")} | {"next_run": next_runs.get(job["id"]).isoformat() if next_runs.get(job["id"]) else None})
    return {"jobs": public_jobs, "builds": builds, "queue_size": queue.qsize(), "running": list(running)}


@app.post("/api/jobs/{job_id}/build")
async def start_build(job_id: str, request: Request) -> dict[str, int]:
    require_auth(request)
    return {"id": await enqueue(find_job(job_id), "web")}


async def cancel_build(build_id: int) -> str:
    item = row(build_id)
    proc = running.get(build_id)
    if not proc:
        return f"#{build_id} 当前不可取消（{item['status']}）"
    os.killpg(proc.pid, signal.SIGTERM)
    with db() as conn:
        conn.execute("UPDATE builds SET status='cancelled',finished_at=? WHERE id=?", (now(), build_id))
    return f"已取消 #{build_id}"


@app.post("/api/builds/{build_id}/cancel")
async def cancel(build_id: int, request: Request) -> dict[str, str]:
    require_auth(request)
    return {"message": await cancel_build(build_id)}


@app.get("/api/builds/{build_id}/log")
async def build_log(build_id: int, request: Request, tail: int = 400) -> PlainTextResponse:
    require_auth(request)
    path = Path(row(build_id)["log_path"])
    if not path.exists():
        return PlainTextResponse("")
    lines = path.read_text(errors="replace").splitlines()[-min(max(tail, 1), 3000):]
    return PlainTextResponse("\n".join(lines))


@app.post("/api/onebot/webhook")
async def onebot(request: Request, x_signature: str | None = Header(default=None)) -> dict[str, Any]:
    raw = await request.body()
    webhook_secret = os.getenv("QQ_WEBHOOK_SECRET", "")
    if webhook_secret:
        expected = "sha1=" + hmac.new(webhook_secret.encode(), raw, hashlib.sha1).hexdigest()
        if not x_signature or not hmac.compare_digest(x_signature, expected):
            raise HTTPException(401, "invalid signature")
    event = json.loads(raw)
    if event.get("post_type") != "message":
        return {"ok": True}
    user_id, group_id = event.get("user_id"), event.get("group_id")
    if not bot_allowed("qq", user_id, group_id):
        raise HTTPException(403, "not allowed")
    answer = await bot_command(str(event.get("raw_message", "")), "qq")
    params = {"message": answer}
    action = "send_group_msg" if group_id else "send_private_msg"
    params["group_id" if group_id else "user_id"] = group_id or user_id
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(f"{config['qq']['onebot_api_url'].rstrip('/')}/{action}", headers=onebot_headers(), json=params)
    return {"ok": True}
