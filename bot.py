#!/usr/bin/env python3
import asyncio
import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import psutil

load_dotenv()

# ── 設定 ─────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_USER_IDS = set(
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
)
ALLOWED_CHANNEL_IDS = set(
    int(x.strip()) for x in os.getenv("ALLOWED_CHANNEL_IDS", "").split(",") if x.strip()
)
COMMAND_TIMEOUT = int(os.getenv("COMMAND_TIMEOUT", "30"))
WORK_DIR = Path(os.getenv("WORK_DIR", Path.home())).expanduser()
SCREENSHOT_PATH = "/tmp/_bot_ss.png"
CAMERA_PATH = "/tmp/_bot_cam.jpg"
BLOCKED_CMDS = [
    s.strip()
    for s in os.getenv(
        "BLOCKED_COMMANDS",
        "rm -rf /,mkfs,dd if=/dev/zero of=,:(){ :|:& };:"
    ).split(",")
    if s.strip()
]
SESSION_MARKER = "__BOT_CMD_END_7f3a9b__"
ALERT_COOLDOWN = timedelta(minutes=10)
# 認証設定
BOT_AUTH_PASSWORD = os.getenv("BOT_AUTH_PASSWORD", "")   # Discord認証パスワード
SUDO_PASSWORD     = os.getenv("SUDO_PASSWORD", "")        # Linux sudoパスワード
AUTH_MAX_FAILURES = int(os.getenv("AUTH_MAX_FAILURES", "3"))
AUTH_COOLDOWN     = timedelta(minutes=int(os.getenv("AUTH_COOLDOWN_MINUTES", "5")))

# ── 状態管理 ─────────────────────────────────────────────────────────────────
# 複数ターミナルセッション: (user_id, session_name) -> {"proc"}
_sessions: dict[tuple[int, str], dict] = {}
# バックグラウンドジョブ: job_id -> {"proc", "cmd", "started"}
_bg_jobs: dict[int, dict] = {}
_next_job_id = 1
# ログwatcher: (channel_id, path_str) -> {"path", "task"}
_watchers: dict[tuple[int, str], dict] = {}
# inotify watcher: (channel_id, path_str) -> {"task"}
_iwatchers: dict[tuple[int, str], dict] = {}
# アラート: channel_id -> {"cpu","mem","disk"}
_alerts: dict[int, dict] = {}
_alert_last_sent: dict[tuple[int, str], datetime] = {}
# 自動スクリーンショット: channel_id -> task
_autoshoot: dict[int, asyncio.Task] = {}
# 認証失敗記録: user_id -> [失敗時刻, ...]
_auth_failures: dict[int, list[datetime]] = {}

# ── 永続設定 ─────────────────────────────────────────────────────────────────
CONFIG_PATH = Path("config.json")
_cfg: dict = {"alerts": {}, "autowatch": {}, "autoshot": {}, "blocked": []}


def _cfg_load() -> None:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in data.items():
                _cfg[k] = v
        except Exception as e:
            print(f"[config] 読み込みエラー: {e}")


def _cfg_save() -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(_cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[config] 保存エラー: {e}")


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ── ヘルパー ─────────────────────────────────────────────────────────────────

def is_authorized(ctx: commands.Context) -> bool:
    if ALLOWED_USER_IDS and ctx.author.id not in ALLOWED_USER_IDS:
        return False
    if ALLOWED_CHANNEL_IDS and ctx.channel.id not in ALLOWED_CHANNEL_IDS:
        return False
    return True


def is_authorized_i(interaction: discord.Interaction) -> bool:
    if ALLOWED_USER_IDS and interaction.user.id not in ALLOWED_USER_IDS:
        return False
    if ALLOWED_CHANNEL_IDS and interaction.channel_id not in ALLOWED_CHANNEL_IDS:
        return False
    return True


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


async def send_text(channel: discord.abc.Messageable, text: str) -> None:
    text = text or "(出力なし)"
    for i in range(0, len(text), 1900):
        await channel.send(f"```\n{text[i:i+1900]}\n```")


def is_blocked(cmd: str) -> str | None:
    for b in BLOCKED_CMDS + _cfg.get("blocked", []):
        if cmd.strip().startswith(b):
            return b
    return None


async def run_shell(cmd: str, timeout: int = COMMAND_TIMEOUT) -> str:
    """シェルコマンドを実行して出力を文字列で返す"""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"[タイムアウト {timeout}秒]"
    return stdout.decode("utf-8", errors="replace").strip()


async def confirm(ctx: commands.Context, prompt: str) -> bool:
    await ctx.send(f"{prompt}\n`yes` か `no` で答えてください。")
    try:
        reply = await bot.wait_for(
            "message", timeout=30,
            check=lambda m: m.author == ctx.author and m.channel == ctx.channel,
        )
        return reply.content.strip().lower() in ("yes", "y", "はい")
    except asyncio.TimeoutError:
        await ctx.send("タイムアウト。キャンセルしました。")
        return False


async def request_auth(ctx: commands.Context) -> bool:
    """
    DM経由でパスワード認証を行う。
    - BOT_AUTH_PASSWORD と照合
    - 失敗3回で AUTH_COOLDOWN 分間ロック
    - ユーザーが送ったDMメッセージは即削除
    """
    if not BOT_AUTH_PASSWORD:
        await ctx.send("⚠️ `BOT_AUTH_PASSWORD` が .env に未設定です。設定してください。")
        return False

    uid = ctx.author.id
    now = datetime.now()

    # クールダウンチェック
    recent = [t for t in _auth_failures.get(uid, []) if now - t < AUTH_COOLDOWN]
    if len(recent) >= AUTH_MAX_FAILURES:
        remaining = int((AUTH_COOLDOWN - (now - recent[-1])).total_seconds() / 60) + 1
        await ctx.send(f"🔒 認証ロック中。{remaining}分後に再試行してください。")
        return False

    # DM でパスワードを要求
    try:
        await ctx.author.send(
            f"🔐 **認証が必要です**\n"
            f"コマンド: `{ctx.message.content[:100]}`\n\n"
            f"パスワードを **このDM** に送信してください。（30秒以内）\n"
            f"送信後、メッセージは自動的に削除されます。"
        )
    except discord.Forbidden:
        await ctx.send("❌ DMを送れません。Discordの設定でサーバーメンバーからのDMを許可してください。")
        return False

    notice = await ctx.send("🔐 **DMにパスワードを送信してください。**（30秒）")

    # DM返信を待つ
    try:
        dm_reply = await bot.wait_for(
            "message",
            timeout=30,
            check=lambda m: m.author == ctx.author and isinstance(m.channel, discord.DMChannel),
        )
    except asyncio.TimeoutError:
        await notice.edit(content="⏰ タイムアウト。認証失敗。")
        return False
    finally:
        try:
            await notice.delete()
        except Exception:
            pass

    # パスワード検証（DMを即削除）
    try:
        await dm_reply.delete()
    except Exception:
        pass

    if dm_reply.content.strip() == BOT_AUTH_PASSWORD:
        _auth_failures.pop(uid, None)
        await ctx.send("✅ 認証成功。実行します。")
        return True
    else:
        _auth_failures.setdefault(uid, []).append(now)
        recent_after = [t for t in _auth_failures[uid] if now - t < AUTH_COOLDOWN]
        left = max(0, AUTH_MAX_FAILURES - len(recent_after))
        await ctx.send(f"❌ パスワードが違います。残り試行回数: **{left}**")
        return False


async def sudo_run(cmd: str, timeout: int = COMMAND_TIMEOUT) -> str:
    """sudo でコマンドを実行する（SUDO_PASSWORD を使用）。"""
    if SUDO_PASSWORD:
        escaped = SUDO_PASSWORD.replace("'", "'\\''")
        full = f"echo '{escaped}' | sudo -S {cmd} 2>&1"
    else:
        full = f"sudo {cmd} 2>&1"
    return await run_shell(full, timeout=timeout)


def _start_watch_task(channel: discord.abc.Messageable, target: Path, key: tuple) -> None:
    """ログファイルの tail タスクを開始して _watchers に登録する。"""
    init_pos = target.stat().st_size if target.exists() else 0

    async def _tail():
        pos = init_pos
        pending = ""
        try:
            while True:
                await asyncio.sleep(2)
                try:
                    size = target.stat().st_size
                except FileNotFoundError:
                    await channel.send(f"`{target}` が削除されました。監視停止。")
                    break
                if size < pos:
                    pos = 0
                if size > pos:
                    with open(target, "rb") as f:
                        f.seek(pos)
                        chunk = f.read(size - pos)
                    pos = size
                    pending += chunk.decode("utf-8", errors="replace")
                if "\n" in pending or len(pending) > 800:
                    text = pending.strip()
                    pending = ""
                    if text:
                        await send_text(channel, text)
        except asyncio.CancelledError:
            pass
        finally:
            _watchers.pop(key, None)

    task = asyncio.create_task(_tail())
    _watchers[key] = {"path": target, "task": task}


def _start_autoshot_task(channel: discord.abc.Messageable, interval: int) -> None:
    """自動スクリーンショットタスクを開始して _autoshoot に登録する。"""
    ch_id = channel.id

    async def _loop():
        try:
            while True:
                ok, _ = await _take_screenshot()
                if ok:
                    await channel.send(
                        f"`{datetime.now().strftime('%H:%M:%S')}` — 自動送信",
                        file=discord.File(SCREENSHOT_PATH, filename="screenshot.png"),
                    )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        finally:
            _autoshoot.pop(ch_id, None)

    task = asyncio.create_task(_loop())
    _autoshoot[ch_id] = task


# ── イベント ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    _cfg_load()
    # config からアラートを復元
    for ch_id_str, thresholds in _cfg.get("alerts", {}).items():
        _alerts[int(ch_id_str)] = thresholds
    print(f"起動: {bot.user}  作業Dir: {WORK_DIR}")
    asyncio.create_task(_alert_monitor_loop())
    asyncio.create_task(_restore_persistent())


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"引数が不足: `{error.param.name}`")
        return
    await ctx.send(f"エラー: {error}")


# ── アラート監視 ───────────────────────────────────────────────────────────────

async def _alert_monitor_loop():
    while True:
        await asyncio.sleep(30)
        if not _alerts:
            continue
        try:
            cpu = psutil.cpu_percent()
            mem = psutil.virtual_memory().percent
            disk = psutil.disk_usage("/").percent
            now = datetime.now()
            for ch_id, thresholds in list(_alerts.items()):
                channel = bot.get_channel(ch_id)
                if not channel:
                    continue
                for metric, value in {"cpu": cpu, "mem": mem, "disk": disk}.items():
                    limit = thresholds.get(metric)
                    if limit is None or value < limit:
                        continue
                    key = (ch_id, metric)
                    if (last := _alert_last_sent.get(key)) and now - last < ALERT_COOLDOWN:
                        continue
                    label = {"cpu": "CPU", "mem": "メモリ", "disk": "ディスク"}[metric]
                    await channel.send(
                        f"⚠️ **アラート**: {label}使用率 **{value:.1f}%**（しきい値: {limit}%）"
                    )
                    _alert_last_sent[key] = now
        except Exception as e:
            print(f"[alert] {e}")


async def _restore_persistent() -> None:
    """Bot起動時に永続設定（autowatch / autoshot）を復元する。"""
    await asyncio.sleep(3)  # チャンネルキャッシュが揃うのを待つ

    for ch_id_str, paths in _cfg.get("autowatch", {}).items():
        ch_id = int(ch_id_str)
        channel = bot.get_channel(ch_id)
        if not channel:
            continue
        for path_str in list(paths):
            target = Path(path_str)
            if not target.is_file():
                continue
            key = (ch_id, path_str)
            if key in _watchers:
                continue
            _start_watch_task(channel, target, key)
            await channel.send(f"🔄 自動ログ監視を再開: `{path_str}`")

    for ch_id_str, interval in _cfg.get("autoshot", {}).items():
        ch_id = int(ch_id_str)
        channel = bot.get_channel(ch_id)
        if not channel or ch_id in _autoshoot:
            continue
        _start_autoshot_task(channel, interval)
        await channel.send(f"🔄 自動スクリーンショットを再開（{interval}秒ごと）")


# ════════════════════════════════════════════════════════════
#  シェルコマンド
# ════════════════════════════════════════════════════════════

@bot.command(name="run", aliases=["r"])
async def cmd_run(ctx: commands.Context, *, cmd: str):
    """コマンドを実行（リアルタイム出力）。例: !run df -h"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if (blocked := is_blocked(cmd)):
        await ctx.send(f"ブロックされたコマンド: `{blocked}`")
        return

    status = await ctx.send("⏳ 実行中...")
    collected: list[str] = []
    last_edit = asyncio.get_event_loop().time()

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=WORK_DIR,
        )
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT):
                async for raw in proc.stdout:
                    collected.append(raw.decode("utf-8", errors="replace"))
                    now = asyncio.get_event_loop().time()
                    if now - last_edit >= 2.0:
                        preview = "".join(collected)[-1500:]
                        try:
                            await status.edit(content=f"⏳ 実行中...\n```\n{preview}\n```")
                        except discord.HTTPException:
                            pass
                        last_edit = now
        except asyncio.TimeoutError:
            proc.kill()
            await status.edit(content=f"⏰ タイムアウト（{COMMAND_TIMEOUT}秒）")
            return
        await proc.wait()
        await status.delete()
        await send_text(ctx.channel, "".join(collected).strip())
    except Exception as e:
        await status.edit(content=f"エラー: {e}")


@bot.command(name="bg")
async def cmd_bg(ctx: commands.Context, *, cmd: str):
    """バックグラウンド実行。完了時に通知。例: !bg python train.py"""
    global _next_job_id
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if (blocked := is_blocked(cmd)):
        await ctx.send(f"ブロックされたコマンド: `{blocked}`")
        return
    job_id = _next_job_id
    _next_job_id += 1
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=WORK_DIR,
    )
    _bg_jobs[job_id] = {"proc": proc, "cmd": cmd, "started": datetime.now()}
    await ctx.send(f"バックグラウンド開始 **Job #{job_id}** (PID: {proc.pid})\n`{cmd}`")

    async def _notify():
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace").strip()
        s = "✅ 完了" if proc.returncode == 0 else f"❌ 終了コード {proc.returncode}"
        await ctx.channel.send(f"**Job #{job_id}** {s}: `{cmd}`")
        if output:
            await send_text(ctx.channel, output[-3000:])
        _bg_jobs.pop(job_id, None)

    asyncio.create_task(_notify())


@bot.command(name="jobs")
async def cmd_jobs(ctx: commands.Context):
    """バックグラウンドジョブ一覧。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if not _bg_jobs:
        await ctx.send("実行中のジョブはありません。")
        return
    lines = [f"#{jid}  PID:{i['proc'].pid}  [{int((datetime.now()-i['started']).total_seconds())}s]  {i['cmd']}"
             for jid, i in _bg_jobs.items()]
    await ctx.send("```\n" + "\n".join(lines) + "\n```")


@bot.command(name="stop")
async def cmd_stop(ctx: commands.Context, job_id: int):
    """バックグラウンドジョブを停止。例: !stop 1"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    info = _bg_jobs.get(job_id)
    if not info:
        await ctx.send(f"Job #{job_id} が見つかりません。")
        return
    info["proc"].terminate()
    await ctx.send(f"Job #{job_id} を停止しました。")


@bot.command(name="input", aliases=["in"])
async def cmd_input(ctx: commands.Context, job_id: int, *, text: str):
    """バックグラウンドジョブにstdinを送信。例: !input 1 yes"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    info = _bg_jobs.get(job_id)
    if not info:
        await ctx.send(f"Job #{job_id} が見つかりません。")
        return
    proc = info["proc"]
    if proc.stdin is None or proc.stdin.is_closing():
        await ctx.send("入力を受け付けていません。")
        return
    proc.stdin.write((text + "\n").encode())
    await proc.stdin.drain()
    await ctx.send(f"Job #{job_id} に送信: `{text}`")


# ════════════════════════════════════════════════════════════
#  複数ターミナルセッション
# ════════════════════════════════════════════════════════════

@bot.command(name="session")
async def cmd_session(ctx: commands.Context, action: str = "list", name: str = "main"):
    """
    ターミナルセッション管理。
      !session start [名前]  → セッションを開始（デフォルト名: main）
      !session end [名前]    → セッションを終了
      !session list          → セッション一覧
    """
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    uid = ctx.author.id

    if action == "list":
        mine = [(n, i) for (u, n), i in _sessions.items() if u == uid]
        if not mine:
            await ctx.send("起動中のセッションはありません。`!session start [名前]` で開始。")
            return
        lines = [f"`{n}`  PID:{i['proc'].pid}" for n, i in mine]
        await ctx.send("起動中のセッション:\n" + "\n".join(lines))

    elif action == "start":
        key = (uid, name)
        if key in _sessions:
            await ctx.send(f"セッション `{name}` は既に起動中です。")
            return
        proc = await asyncio.create_subprocess_shell(
            "bash",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=WORK_DIR,
        )
        _sessions[key] = {"proc": proc}
        await ctx.send(
            f"セッション **{name}** 開始 (PID: {proc.pid})\n"
            f"`!s {name} <コマンド>` で実行。終了: `!session end {name}`"
        )

    elif action == "end":
        key = (uid, name)
        info = _sessions.pop(key, None)
        if not info:
            await ctx.send(f"セッション `{name}` が見つかりません。")
            return
        info["proc"].terminate()
        await ctx.send(f"セッション **{name}** を終了しました。")

    else:
        await ctx.send("`!session start/end/list [名前]` の形式で指定してください。")


@bot.command(name="s")
async def cmd_s(ctx: commands.Context, *, args: str):
    """
    セッション内でコマンドを実行（cd/exportが引き継がれる）。
      !s <コマンド>             → mainセッションで実行
      !s <セッション名> <コマンド> → 指定セッションで実行
    """
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    uid = ctx.author.id
    user_sessions = {n: i for (u, n), i in _sessions.items() if u == uid}

    if not user_sessions:
        await ctx.send("セッションがありません。`!session start` で開始してください。")
        return

    # 先頭の単語がセッション名かチェック
    parts = args.split(None, 1)
    if len(parts) == 2 and parts[0] in user_sessions:
        name, cmd = parts[0], parts[1]
    elif "main" in user_sessions:
        name, cmd = "main", args
    elif len(user_sessions) == 1:
        name = next(iter(user_sessions))
        cmd = args
    else:
        names = " / ".join(f"`{n}`" for n in user_sessions)
        await ctx.send(f"セッション名を先頭に指定してください。例: `!s work ls -la`\n利用可能: {names}")
        return

    if (blocked := is_blocked(cmd)):
        await ctx.send(f"ブロックされたコマンド: `{blocked}`")
        return

    info = user_sessions[name]
    proc = info["proc"]
    if proc.returncode is not None:
        _sessions.pop((uid, name), None)
        await ctx.send(f"セッション `{name}` は終了しています。`!session start {name}` で再起動してください。")
        return

    payload = f"{{ {cmd}; }} 2>&1; echo '{SESSION_MARKER}'\n"
    proc.stdin.write(payload.encode())
    await proc.stdin.drain()

    output_lines: list[str] = []
    try:
        async with asyncio.timeout(COMMAND_TIMEOUT):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace")
                if SESSION_MARKER in decoded:
                    break
                output_lines.append(decoded)
    except asyncio.TimeoutError:
        output_lines.append(f"\n[タイムアウト ({COMMAND_TIMEOUT}秒)]")

    await send_text(ctx.channel, "".join(output_lines).strip())


# ════════════════════════════════════════════════════════════
#  ファイル管理
# ════════════════════════════════════════════════════════════

@bot.command(name="ls")
async def cmd_ls(ctx: commands.Context, path: str = "."):
    """ディレクトリ内容を表示。例: !ls /var/log"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    target = (WORK_DIR / path).resolve()
    if not target.exists():
        await ctx.send(f"存在しないパス: `{target}`")
        return
    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        lines = []
        for e in entries:
            icon = "📄" if e.is_file() else "📁"
            size = f"  {human_size(e.stat().st_size)}" if e.is_file() else ""
            lines.append(f"{icon} {e.name}{size}")
        await ctx.send(f"`{target}`\n```\n{chr(10).join(lines)[:1800] or '(空)'}\n```")
    except PermissionError:
        await ctx.send("アクセス権限がありません。")


@bot.command(name="cat")
async def cmd_cat(ctx: commands.Context, path: str):
    """ファイル内容を表示。例: !cat /etc/os-release"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    target = (WORK_DIR / path).resolve()
    if not target.is_file():
        await ctx.send(f"ファイルが見つかりません: `{target}`")
        return
    try:
        await send_text(ctx.channel, target.read_text(errors="replace"))
    except PermissionError:
        await ctx.send("アクセス権限がありません。")


@bot.command(name="download", aliases=["dl"])
async def cmd_download(ctx: commands.Context, path: str):
    """ファイルをDiscordに送信（JPG等の画像も可）。例: !download /home/user/photo.jpg"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    target = (WORK_DIR / path).resolve()
    if not target.is_file():
        await ctx.send(f"ファイルが見つかりません: `{target}`")
        return
    size = target.stat().st_size
    if size > 25 * 1024 * 1024:
        await ctx.send(f"ファイルが大きすぎます（{human_size(size)} / 上限25MB）。")
        return
    await ctx.send(file=discord.File(str(target)))


@bot.command(name="upload", aliases=["ul"])
async def cmd_upload(ctx: commands.Context, path: str = "."):
    """添付ファイルをLinuxに保存。例: !upload /home/user/"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if not ctx.message.attachments:
        await ctx.send("ファイルを添付してください。")
        return
    dest_dir = (WORK_DIR / path).resolve()
    if not dest_dir.is_dir():
        await ctx.send(f"ディレクトリが存在しません: `{dest_dir}`")
        return
    saved = []
    for att in ctx.message.attachments:
        dest = dest_dir / att.filename
        dest.write_bytes(await att.read())
        saved.append(str(dest))
    await ctx.send("保存完了:\n" + "\n".join(f"`{p}`" for p in saved))


@bot.command(name="rm")
async def cmd_rm(ctx: commands.Context, path: str):
    """ファイル/ディレクトリを削除（確認あり）。例: !rm /tmp/test.txt"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    target = (WORK_DIR / path).resolve()
    if not target.exists():
        await ctx.send(f"存在しないパス: `{target}`")
        return
    if not await confirm(ctx, f"`{target}` を削除しますか？"):
        return
    shutil.rmtree(target) if target.is_dir() else target.unlink()
    await ctx.send(f"削除しました: `{target}`")


@bot.command(name="find")
async def cmd_find(ctx: commands.Context, pattern: str, path: str = "."):
    """ファイル名で検索。例: !find *.log /var/log"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    target = (WORK_DIR / path).resolve()
    out = await run_shell(f"find {target} -name '{pattern}' 2>/dev/null | head -50", timeout=15)
    await send_text(ctx.channel, out or "見つかりませんでした。")


@bot.command(name="grep")
async def cmd_grep(ctx: commands.Context, pattern: str, path: str = "."):
    """ファイル内テキストを検索。例: !grep ERROR /var/log/syslog"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    target = (WORK_DIR / path).resolve()
    out = await run_shell(f"grep -rn '{pattern}' {target} 2>/dev/null | head -30", timeout=15)
    await send_text(ctx.channel, out or "マッチなし。")


# ════════════════════════════════════════════════════════════
#  動的コマンドブロック管理
# ════════════════════════════════════════════════════════════

@bot.command(name="block")
async def cmd_block(ctx: commands.Context, action: str, *, cmd: str = ""):
    """
    ブロックするコマンドを動的に管理。
      !block list            → 現在のブロックリスト（.env + 動的追加）
      !block add <cmd>       → コマンドをブロックに追加
      !block remove <cmd>    → コマンドをブロックから削除
    """
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return

    dynamic: list = _cfg.setdefault("blocked", [])

    if action == "list":
        env_part = "\n".join(f"  `{b}`" for b in BLOCKED_CMDS) or "  (なし)"
        dyn_part = "\n".join(f"  `{b}`" for b in dynamic) or "  (なし)"
        await ctx.send(
            f"**ブロックリスト**\n"
            f"`.env` 設定:\n{env_part}\n"
            f"動的追加 (`!block add`):\n{dyn_part}"
        )

    elif action == "add":
        if not cmd:
            await ctx.send("ブロックするコマンドを指定してください。例: `!block add curl evil.com`")
            return
        if cmd in dynamic:
            await ctx.send(f"既にブロック済み: `{cmd}`")
            return
        dynamic.append(cmd)
        _cfg_save()
        await ctx.send(f"ブロックリストに追加しました: `{cmd}`")

    elif action == "remove":
        if not cmd:
            await ctx.send("削除するコマンドを指定してください。")
            return
        if cmd not in dynamic:
            await ctx.send(f"`{cmd}` はブロックリストにありません。`!block list` で確認してください。")
            return
        dynamic.remove(cmd)
        _cfg_save()
        await ctx.send(f"ブロックリストから削除しました: `{cmd}`")

    else:
        await ctx.send("`!block list / add <cmd> / remove <cmd>` の形式で指定してください。")


# ════════════════════════════════════════════════════════════
#  システム情報
# ════════════════════════════════════════════════════════════

@bot.command(name="sysinfo", aliases=["sys"])
async def cmd_sysinfo(ctx: commands.Context):
    """CPU・メモリ・ディスク情報。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    embed = discord.Embed(title="システム情報", color=discord.Color.blue())
    embed.add_field(name="CPU", value=f"{cpu}%", inline=True)
    embed.add_field(name="メモリ", value=f"{human_size(mem.used)} / {human_size(mem.total)} ({mem.percent}%)", inline=True)
    embed.add_field(name="ディスク (/)", value=f"{human_size(disk.used)} / {human_size(disk.total)} ({disk.percent}%)", inline=True)
    uptime = await run_shell("uptime -p 2>/dev/null")
    if uptime:
        embed.add_field(name="稼働時間", value=uptime, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="meminfo")
async def cmd_meminfo(ctx: commands.Context):
    """メモリの詳細情報を表示。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    embed = discord.Embed(title="メモリ詳細", color=discord.Color.blue())
    embed.add_field(name="合計",        value=human_size(mem.total),     inline=True)
    embed.add_field(name="使用中",      value=human_size(mem.used),      inline=True)
    embed.add_field(name="空き",        value=human_size(mem.available),  inline=True)
    embed.add_field(name="キャッシュ",   value=human_size(getattr(mem, "cached", 0)), inline=True)
    embed.add_field(name="バッファ",     value=human_size(getattr(mem, "buffers", 0)), inline=True)
    embed.add_field(name="使用率",      value=f"{mem.percent}%",         inline=True)
    embed.add_field(name="Swap 合計",   value=human_size(swap.total),    inline=True)
    embed.add_field(name="Swap 使用",   value=human_size(swap.used),     inline=True)
    embed.add_field(name="Swap 使用率", value=f"{swap.percent}%",        inline=True)

    # メモリ消費 Top5
    top = sorted(
        psutil.process_iter(["pid", "name", "memory_info"]),
        key=lambda p: p.info["memory_info"].rss if p.info["memory_info"] else 0,
        reverse=True,
    )[:5]
    top_lines = [f"{human_size(p.info['memory_info'].rss):>9}  {p.info['name']}" for p in top if p.info["memory_info"]]
    if top_lines:
        embed.add_field(name="メモリ消費 Top5", value="```\n" + "\n".join(top_lines) + "\n```", inline=False)

    await ctx.send(embed=embed)


@bot.command(name="temp")
async def cmd_temp(ctx: commands.Context):
    """CPU・GPU温度を表示。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    lines = []

    # psutil でセンサー取得
    try:
        temps = psutil.sensors_temperatures()
        for name, entries in temps.items():
            for e in entries:
                label = e.label or name
                lines.append(f"{label}: {e.current:.1f}°C")
    except (AttributeError, Exception):
        pass

    # sensors コマンドでフォールバック
    if not lines:
        out = await run_shell("sensors 2>/dev/null | grep -E '°C|temp'")
        if out:
            lines = out.splitlines()

    # GPU温度
    gpu_temp = await run_shell(
        "nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader 2>/dev/null"
    )
    if gpu_temp.isdigit():
        lines.append(f"GPU (NVIDIA): {gpu_temp}°C")
    else:
        amd = await run_shell("rocm-smi --showtemp 2>/dev/null | grep -i temp | head -3")
        if amd:
            lines.extend(amd.splitlines())

    if not lines:
        await ctx.send("温度センサーが見つかりません。`sudo apt install lm-sensors` を試してください。")
        return
    await ctx.send("```\n" + "\n".join(lines) + "\n```")


@bot.command(name="gpu")
async def cmd_gpu(ctx: commands.Context):
    """GPU使用率・VRAM・温度を表示（NVIDIA/AMD対応）。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return

    # NVIDIA
    nvidia = await run_shell(
        "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu "
        "--format=csv,noheader,nounits 2>/dev/null"
    )
    if nvidia and "failed" not in nvidia.lower():
        embed = discord.Embed(title="GPU情報 (NVIDIA)", color=discord.Color.green())
        for i, line in enumerate(nvidia.splitlines()):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                name, util, mem_used, mem_total, temp = parts[:5]
                embed.add_field(name=f"GPU {i}: {name}", inline=False,
                    value=f"使用率: {util}%  VRAM: {mem_used}/{mem_total} MiB  温度: {temp}°C")
        await ctx.send(embed=embed)
        return

    # AMD
    amd = await run_shell("rocm-smi 2>/dev/null")
    if amd and "not found" not in amd.lower():
        await send_text(ctx.channel, amd)
        return

    await ctx.send("GPUが見つかりません。nvidia-smi または rocm-smi をインストールしてください。")


@bot.command(name="ps")
async def cmd_ps(ctx: commands.Context, count: int = 15):
    """CPU使用率上位プロセス。例: !ps 20"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    procs = sorted(
        psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
        key=lambda p: p.info["cpu_percent"] or 0, reverse=True,
    )[:count]
    lines = [f"{'PID':>6}  {'CPU%':>5}  {'MEM%':>5}  名前", "-" * 38]
    for p in procs:
        lines.append(f"{p.info['pid']:>6}  {p.info['cpu_percent'] or 0:>5.1f}  {p.info['memory_percent'] or 0:>5.1f}  {p.info['name']}")
    await ctx.send("```\n" + "\n".join(lines) + "\n```")


@bot.command(name="kill")
async def cmd_kill(ctx: commands.Context, pid: int):
    """プロセスを終了。例: !kill 1234"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if not await request_auth(ctx):
        return
    try:
        p = psutil.Process(pid)
        name = p.name()
        p.terminate()
        await ctx.send(f"プロセス `{name}` (PID: {pid}) を終了しました。")
    except psutil.NoSuchProcess:
        await ctx.send(f"PID {pid} は存在しません。")
    except psutil.AccessDenied:
        await ctx.send("権限不足で終了できません。")


@bot.command(name="killapp")
async def cmd_killapp(ctx: commands.Context, *, name: str):
    """アプリ名でプロセスを終了。例: !killapp firefox"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return

    # 対象プロセスを先に表示して確認
    matched = await run_shell(f"pgrep -a -i '{name}' 2>/dev/null | head -20")
    if not matched:
        await ctx.send(f"`{name}` に一致するプロセスが見つかりません。")
        return

    await ctx.send(f"以下のプロセスを終了します:\n```\n{matched[:1000]}\n```")
    if not await request_auth(ctx):
        return
    if not await confirm(ctx, f"`{name}` に一致する全プロセスを終了しますか？"):
        return

    out = await run_shell(f"pkill -i '{name}' 2>&1; echo exit:$?")
    if "exit:0" in out or "exit:1" in out:
        await ctx.send(f"`{name}` を終了しました。")
    else:
        await ctx.send(f"終了失敗。権限不足の場合は `sudo pkill` が必要です。\n```\n{out[:300]}\n```")


@bot.command(name="service")
async def cmd_service(ctx: commands.Context, action: str, name: str):
    """systemdサービスを操作。例: !service restart nginx"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if action not in ("start", "stop", "restart", "status"):
        await ctx.send("action は `start` / `stop` / `restart` / `status` のいずれか。")
        return
    # status は読み取りのみなので認証不要、変更系のみ認証
    if action != "status":
        if not await request_auth(ctx):
            return
    out = await sudo_run(f"systemctl {action} {name}")
    await send_text(ctx.channel, out or "完了")


# ════════════════════════════════════════════════════════════
#  ネットワーク
# ════════════════════════════════════════════════════════════

@bot.command(name="netinfo")
async def cmd_netinfo(ctx: commands.Context):
    """ネットワークインターフェース・IPアドレス・ルート情報を表示。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    out = await run_shell("ip addr show 2>/dev/null && echo '---' && ip route 2>/dev/null")
    await send_text(ctx.channel, out)


@bot.command(name="ping")
async def cmd_ping(ctx: commands.Context, host: str, count: int = 4):
    """疎通確認。例: !ping google.com 4"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    out = await run_shell(f"ping -c {min(count, 10)} {host}", timeout=30)
    await send_text(ctx.channel, out)


@bot.command(name="curl")
async def cmd_curl(ctx: commands.Context, url: str, *, options: str = ""):
    """HTTPリクエストを送信して結果を表示。例: !curl http://localhost:8080/api/status"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    out = await run_shell(f"curl -s --max-time 15 {options} '{url}' 2>&1 | head -100")
    await send_text(ctx.channel, out)


# ════════════════════════════════════════════════════════════
#  電源管理
# ════════════════════════════════════════════════════════════

@bot.command(name="shutdown")
async def cmd_shutdown(ctx: commands.Context, minutes: int = 0):
    """PCをシャットダウン。例: !shutdown 5 (5分後)  !shutdown 0 (即時)"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if not await request_auth(ctx):
        return
    msg = f"{minutes}分後にシャットダウンしますか？" if minutes > 0 else "今すぐシャットダウンしますか？"
    if not await confirm(ctx, msg):
        return
    out = await sudo_run(f"shutdown -h +{minutes}")
    await ctx.send(f"シャットダウン予約しました。\n```\n{out}\n```" if out else "シャットダウンします。")


@bot.command(name="reboot")
async def cmd_reboot(ctx: commands.Context, target: str = ""):
    """
    PCを再起動。OSを指定すると次回そのOSで起動。
      !reboot            → 通常再起動
      !reboot windows    → Windowsで起動
      !reboot ubuntu     → Ubuntu（デフォルト）で起動
    """
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if not await request_auth(ctx):
        return

    if target:
        # grub.cfg からキーワードに一致するエントリを自動検出
        entries_raw = await run_shell(
            r"awk -F\' '/^menuentry /{print NR-1, $2}' /boot/grub/grub.cfg 2>/dev/null"
        )
        matched = [
            line for line in entries_raw.splitlines()
            if target.lower() in line.lower()
        ]
        if not matched:
            await ctx.send(
                f"`{target}` に一致するGRUBエントリが見つかりません。\n"
                "`!bootentries` でエントリ一覧を確認してください。"
            )
            return
        # 最初に一致したエントリ番号を使用
        entry_num = matched[0].split()[0]
        entry_name = matched[0][len(entry_num):].strip()
        if not await confirm(ctx, f"**{entry_name}** で起動して再起動しますか？"):
            return
        result = await sudo_run(f'grub-reboot {entry_num}')
        if result and "error" in result.lower():
            await ctx.send(f"grub-reboot に失敗しました:\n```\n{result[:300]}\n```")
            return
        await ctx.send(f"次回起動: **{entry_name}**\n再起動します...")
    else:
        if not await confirm(ctx, "再起動しますか？"):
            return
        await ctx.send("再起動します。")

    await sudo_run("reboot")


@bot.command(name="sleep")
async def cmd_sleep_pc(ctx: commands.Context):
    """PCをスリープ（サスペンド）。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if not await request_auth(ctx):
        return
    if not await confirm(ctx, "スリープ（サスペンド）しますか？"):
        return
    await ctx.send("スリープします。")
    await sudo_run("systemctl suspend")


@bot.command(name="bootentries")
async def cmd_bootentries(ctx: commands.Context):
    """GRUBの起動エントリ一覧を表示。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return

    out = await run_shell(
        r"awk -F\' '/^menuentry /{print NR-1, $2}' /boot/grub/grub.cfg 2>/dev/null"
    )
    if not out:
        # grub2-editenv でフォールバック
        out = await run_shell(
            "sudo grub-mkconfig 2>/dev/null | grep -E '^menuentry' | "
            r"awk -F\' '{print NR-1, $2}' | head -20"
        )
    if not out:
        await ctx.send(
            "GRUBエントリを取得できませんでした。\n"
            "`/boot/grub/grub.cfg` が存在するか確認してください。"
        )
        return

    lines = ["```", "番号  エントリ名"]
    for line in out.splitlines():
        lines.append(line)
    lines.append("```")
    lines.append("`!bootinto <番号またはエントリ名>` で次回起動先を指定できます。")
    await ctx.send("\n".join(lines))


@bot.command(name="bootinto")
async def cmd_bootinto(ctx: commands.Context, *, entry: str):
    """
    次回のみ指定OSで起動してreboot。
      !bootinto 1
      !bootinto Windows Boot Manager
    """
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if not await request_auth(ctx):
        return
    if not await confirm(ctx, f"次回起動を **{entry}** に設定して再起動しますか？"):
        return

    result = await sudo_run(f'grub-reboot "{entry}"')
    if result and "error" in result.lower():
        await ctx.send(f"grub-reboot に失敗しました:\n```\n{result[:300]}\n```")
        return

    await ctx.send(f"次回起動: **{entry}**\n再起動します...")
    await sudo_run("reboot")


# ════════════════════════════════════════════════════════════
#  Docker
# ════════════════════════════════════════════════════════════

@bot.command(name="docker")
async def cmd_docker(ctx: commands.Context, action: str, name: str = ""):
    """
    Dockerコンテナを管理。
      !docker ps              → コンテナ一覧
      !docker start <名前>    → 起動
      !docker stop <名前>     → 停止
      !docker restart <名前>  → 再起動
      !docker logs <名前>     → ログを表示
    """
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if action == "ps":
        out = await run_shell("docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' 2>&1")
    elif action in ("start", "stop", "restart") and name:
        out = await run_shell(f"docker {action} {name} 2>&1")
    elif action == "logs" and name:
        out = await run_shell(f"docker logs --tail 50 {name} 2>&1")
    else:
        await ctx.send("使い方: `!docker ps/start/stop/restart/logs [コンテナ名]`")
        return
    await send_text(ctx.channel, out)


# ════════════════════════════════════════════════════════════
#  ログ監視
# ════════════════════════════════════════════════════════════

@bot.command(name="log")
async def cmd_log(ctx: commands.Context, path: str, lines: int = 50):
    """ログの末尾N行を送信。例: !log /var/log/syslog 100"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    target = Path(path).expanduser()
    if not target.is_file():
        await ctx.send(f"ファイルが見つかりません: `{target}`")
        return
    out = await run_shell(f"tail -n {lines} {target}")
    await send_text(ctx.channel, out)


@bot.command(name="watch")
async def cmd_watch(ctx: commands.Context, path: str):
    """ログをリアルタイム監視。複数ファイル同時OK。例: !watch /var/log/syslog"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    target = Path(path).expanduser()
    if not target.is_file():
        await ctx.send(f"ファイルが見つかりません: `{target}`")
        return
    key = (ctx.channel.id, str(target))
    if key in _watchers:
        await ctx.send(f"`{target}` は既に監視中です。")
        return

    _start_watch_task(ctx.channel, target, key)
    await ctx.send(f"`{target}` の監視を開始。停止: `!unwatch {path}`")


@bot.command(name="unwatch")
async def cmd_unwatch(ctx: commands.Context, path: str):
    """ログ監視を停止。例: !unwatch /var/log/syslog"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    key = (ctx.channel.id, str(Path(path).expanduser()))
    info = _watchers.pop(key, None)
    if not info:
        await ctx.send(f"監視していません。`!watches` で一覧確認。")
        return
    info["task"].cancel()
    await ctx.send(f"`{path}` の監視を停止しました。")


@bot.command(name="watches")
async def cmd_watches(ctx: commands.Context):
    """このチャンネルで監視中のログ一覧。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    active = [str(v["path"]) for (cid, _), v in _watchers.items() if cid == ctx.channel.id]
    await ctx.send(("監視中:\n" + "\n".join(f"`{p}`" for p in active)) if active else "監視中のファイルはありません。")


@bot.command(name="autowatch")
async def cmd_autowatch(ctx: commands.Context, action: str = "list", *, path: str = ""):
    """
    Bot起動時に自動で再開するログ監視を管理。
      !autowatch list           → 設定済み一覧
      !autowatch add <パス>     → 追加（このチャンネルで起動時に自動監視）
      !autowatch remove <パス>  → 削除
    """
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return

    ch_str = str(ctx.channel.id)

    if action == "list":
        lines = []
        for cid_str, paths in _cfg.get("autowatch", {}).items():
            ch = bot.get_channel(int(cid_str))
            ch_name = f"#{ch.name}" if ch else f"channel:{cid_str}"
            for p in paths:
                running = "▶" if (int(cid_str), p) in _watchers else "⏹"
                lines.append(f"{running} `{p}` ({ch_name})")
        await ctx.send(
            ("自動監視リスト (▶=監視中 ⏹=停止中):\n" + "\n".join(lines))
            if lines else "自動監視の設定はありません。`!autowatch add <パス>` で追加できます。"
        )

    elif action == "add":
        if not path:
            await ctx.send("パスを指定してください。例: `!autowatch add /var/log/syslog`")
            return
        target = Path(path).expanduser()
        if not target.is_file():
            await ctx.send(f"ファイルが見つかりません: `{target}`")
            return
        path_str = str(target)
        ch_watches: list = _cfg.setdefault("autowatch", {}).setdefault(ch_str, [])
        if path_str in ch_watches:
            await ctx.send(f"既に登録済み: `{path_str}`")
            return
        ch_watches.append(path_str)
        _cfg_save()
        # 今すぐ監視も開始
        key = (ctx.channel.id, path_str)
        if key not in _watchers:
            _start_watch_task(ctx.channel, target, key)
        await ctx.send(f"自動監視に追加し、監視を開始しました: `{path_str}`")

    elif action == "remove":
        if not path:
            await ctx.send("パスを指定してください。")
            return
        path_str = str(Path(path).expanduser())
        ch_watches = _cfg.get("autowatch", {}).get(ch_str, [])
        if path_str not in ch_watches:
            await ctx.send(f"`{path_str}` は登録されていません。`!autowatch list` で確認してください。")
            return
        ch_watches.remove(path_str)
        if not ch_watches:
            _cfg.get("autowatch", {}).pop(ch_str, None)
        _cfg_save()
        # 今の監視も停止
        key = (ctx.channel.id, path_str)
        info = _watchers.pop(key, None)
        if info:
            info["task"].cancel()
        await ctx.send(f"自動監視から削除し、監視を停止しました: `{path_str}`")

    else:
        await ctx.send("`!autowatch list / add <パス> / remove <パス>` の形式で指定してください。")


# ════════════════════════════════════════════════════════════
#  ファイル変更監視 (inotify)
# ════════════════════════════════════════════════════════════

@bot.command(name="iwatch")
async def cmd_iwatch(ctx: commands.Context, path: str):
    """ファイル/ディレクトリの変更をリアルタイムで通知。例: !iwatch /home/user/downloads"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    target = Path(path).expanduser()
    if not target.exists():
        await ctx.send(f"存在しないパス: `{target}`")
        return
    key = (ctx.channel.id, str(target))
    if key in _iwatchers:
        await ctx.send(f"`{target}` は既に監視中です。")
        return

    async def _inotify():
        try:
            proc = await asyncio.create_subprocess_shell(
                f"inotifywait -m -r --format '%T %e %w%f' --timefmt '%H:%M:%S' {target} 2>/dev/null",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    await ctx.channel.send(f"📁 `{text}`")
        except asyncio.CancelledError:
            pass
        finally:
            _iwatchers.pop(key, None)

    task = asyncio.create_task(_inotify())
    _iwatchers[key] = {"task": task}
    await ctx.send(
        f"`{target}` のファイル変更監視を開始。停止: `!iunwatch {path}`\n"
        "※ `inotifywait` が必要: `sudo apt install inotify-tools`"
    )


@bot.command(name="iunwatch")
async def cmd_iunwatch(ctx: commands.Context, path: str):
    """ファイル変更監視を停止。例: !iunwatch /home/user/downloads"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    key = (ctx.channel.id, str(Path(path).expanduser()))
    info = _iwatchers.pop(key, None)
    if not info:
        await ctx.send("監視していません。")
        return
    info["task"].cancel()
    await ctx.send(f"`{path}` のファイル変更監視を停止しました。")


# ════════════════════════════════════════════════════════════
#  アラート
# ════════════════════════════════════════════════════════════

@bot.command(name="alert")
async def cmd_alert(ctx: commands.Context, metric: str, threshold: int):
    """しきい値アラートを設定。例: !alert cpu 90  / !alert mem 85  / !alert disk 95"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if metric not in ("cpu", "mem", "disk"):
        await ctx.send("metric は `cpu` / `mem` / `disk` のいずれか。")
        return
    _alerts.setdefault(ctx.channel.id, {})[metric] = threshold
    _cfg.setdefault("alerts", {})[str(ctx.channel.id)] = _alerts[ctx.channel.id]
    _cfg_save()
    label = {"cpu": "CPU", "mem": "メモリ", "disk": "ディスク"}[metric]
    await ctx.send(f"{label}使用率が **{threshold}%** を超えたらここに通知します。")


@bot.command(name="alerts")
async def cmd_alerts(ctx: commands.Context):
    """設定中のアラート一覧。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    t = _alerts.get(ctx.channel.id, {})
    if not t:
        await ctx.send("アラートが設定されていません。")
        return
    label = {"cpu": "CPU", "mem": "メモリ", "disk": "ディスク"}
    await ctx.send("アラート設定:\n" + "\n".join(f"{label[k]}: **{v}%**" for k, v in t.items()))


@bot.command(name="unalert")
async def cmd_unalert(ctx: commands.Context, metric: str):
    """アラートを解除。例: !unalert cpu"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    if metric not in ("cpu", "mem", "disk"):
        await ctx.send("metric は `cpu` / `mem` / `disk` のいずれか。")
        return
    _alerts.get(ctx.channel.id, {}).pop(metric, None)
    if not _alerts.get(ctx.channel.id):
        _alerts.pop(ctx.channel.id, None)
        _cfg.get("alerts", {}).pop(str(ctx.channel.id), None)
    else:
        _cfg.setdefault("alerts", {})[str(ctx.channel.id)] = _alerts[ctx.channel.id]
    _cfg_save()
    label = {"cpu": "CPU", "mem": "メモリ", "disk": "ディスク"}[metric]
    await ctx.send(f"{label}のアラートを解除しました。")


# ════════════════════════════════════════════════════════════
#  スクリーンショット
# ════════════════════════════════════════════════════════════

_SS_FULLSCREEN = [
    ["scrot", "-o", SCREENSHOT_PATH],
    ["grim", SCREENSHOT_PATH],
    ["gnome-screenshot", "-f", SCREENSHOT_PATH],
    ["spectacle", "--background", "--output", SCREENSHOT_PATH],
    ["import", "-window", "root", SCREENSHOT_PATH],
]
_SS_ACTIVE_WINDOW = [
    ["scrot", "-u", "-o", SCREENSHOT_PATH],
    ["gnome-screenshot", "-w", "-f", SCREENSHOT_PATH],
    ["spectacle", "--background", "--activewindow", "--output", SCREENSHOT_PATH],
]


def _ss_env() -> dict:
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":0")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    return env


async def _run_ss_cmd(cmd: list[str]) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=_ss_env(),
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)
        return Path(SCREENSHOT_PATH).is_file()
    except (FileNotFoundError, asyncio.TimeoutError):
        return False


async def _take_screenshot(target: str | None = None) -> tuple[bool, str]:
    Path(SCREENSHOT_PATH).unlink(missing_ok=True)

    if target and target != "window":
        # xdotool でウィンドウ検索
        win_id = await run_shell(f"xdotool search --name '{target}' 2>/dev/null | head -1", timeout=5)
        if win_id:
            if await _run_ss_cmd(["import", "-window", win_id, SCREENSHOT_PATH]):
                return True, f"ウィンドウ「{target}」"
        # wmctrl フォールバック
        wmid = await run_shell(f"wmctrl -l | grep -i '{target}' | awk '{{print $1}}' | head -1", timeout=5)
        if wmid:
            try:
                if await _run_ss_cmd(["import", "-window", str(int(wmid, 16)), SCREENSHOT_PATH]):
                    return True, f"ウィンドウ「{target}」"
            except ValueError:
                pass
        return False, (
            f"「{target}」のウィンドウが見つかりませんでした。\n"
            "```\nsudo apt install xdotool wmctrl\n```"
        )

    if target == "window":
        for cmd in _SS_ACTIVE_WINDOW:
            if await _run_ss_cmd(cmd):
                return True, "フォーカス中のウィンドウ"
        win_id = await run_shell("xdotool getactivewindow 2>/dev/null", timeout=5)
        if win_id and await _run_ss_cmd(["import", "-window", win_id, SCREENSHOT_PATH]):
            return True, "フォーカス中のウィンドウ"
        return False, "アクティブウィンドウの撮影に失敗。\n```\nsudo apt install scrot xdotool\n```"

    for cmd in _SS_FULLSCREEN:
        if await _run_ss_cmd(cmd):
            return True, "画面全体"
    return False, "スクリーンショットに失敗。\n```\nsudo apt install scrot    # X11\nsudo apt install grim     # Wayland\n```"


@bot.command(name="screenshot", aliases=["ss"])
async def cmd_screenshot(ctx: commands.Context, target: str | None = None):
    """
    スクリーンショットを送信。
      !ss              → 画面全体
      !ss window       → フォーカス中のウィンドウ
      !ss <アプリ名>   → アプリ名でウィンドウ指定（例: !ss firefox）
    """
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    ok, desc = await _take_screenshot(target)
    if not ok:
        await ctx.send(desc)
        return
    await ctx.send(
        f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}` — {desc}",
        file=discord.File(SCREENSHOT_PATH, filename="screenshot.png"),
    )


# ════════════════════════════════════════════════════════════
#  自動スクリーンショット
# ════════════════════════════════════════════════════════════

@bot.command(name="autoshot")
async def cmd_autoshot(ctx: commands.Context, interval: int = 30):
    """定期的にスクリーンショットを送信。例: !autoshot 60 (60秒ごと)"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    interval = max(10, interval)
    if ctx.channel.id in _autoshoot:
        await ctx.send("既に自動送信中です。`!stopauto` で停止してください。")
        return

    _start_autoshot_task(ctx.channel, interval)
    _cfg.setdefault("autoshot", {})[str(ctx.channel.id)] = interval
    _cfg_save()
    await ctx.send(f"自動スクリーンショット開始（{interval}秒ごと）。停止: `!stopauto`")


@bot.command(name="stopauto")
async def cmd_stopauto(ctx: commands.Context):
    """自動スクリーンショットを停止。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    task = _autoshoot.pop(ctx.channel.id, None)
    if not task:
        await ctx.send("自動送信は起動していません。")
        return
    task.cancel()
    _cfg.get("autoshot", {}).pop(str(ctx.channel.id), None)
    _cfg_save()
    await ctx.send("自動スクリーンショットを停止しました。")


# ════════════════════════════════════════════════════════════
#  Webカメラ
# ════════════════════════════════════════════════════════════

@bot.command(name="camera", aliases=["cam"])
async def cmd_camera(ctx: commands.Context, device: str = "/dev/video0"):
    """Webカメラで撮影して送信。例: !camera /dev/video0"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    Path(CAMERA_PATH).unlink(missing_ok=True)

    # fswebcam を試す
    out = await run_shell(
        f"fswebcam -d {device} -r 1280x720 --no-banner {CAMERA_PATH} 2>&1", timeout=15
    )
    if not Path(CAMERA_PATH).is_file():
        # ffmpeg でフォールバック
        out = await run_shell(
            f"ffmpeg -y -i {device} -vframes 1 {CAMERA_PATH} 2>&1", timeout=15
        )
    if not Path(CAMERA_PATH).is_file():
        await ctx.send(
            "カメラの撮影に失敗しました。\n"
            "```\nsudo apt install fswebcam\n# または\nsudo apt install ffmpeg\n```\n"
            f"エラー: {out[:300]}"
        )
        return
    await ctx.send(
        f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}` — カメラ ({device})",
        file=discord.File(CAMERA_PATH, filename="camera.jpg"),
    )


# ════════════════════════════════════════════════════════════
#  クリップボード
# ════════════════════════════════════════════════════════════

@bot.command(name="clip")
async def cmd_clip(ctx: commands.Context, action: str, *, text: str = ""):
    """
    クリップボードを操作。
      !clip get       → クリップボードの内容をDiscordに送信
      !clip set <テキスト> → クリップボードにテキストをセット
    """
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    env = _ss_env()

    if action == "get":
        # X11
        out = await run_shell("xclip -selection clipboard -o 2>/dev/null || xsel --clipboard --output 2>/dev/null || wl-paste 2>/dev/null")
        await send_text(ctx.channel, out or "(クリップボードが空です)")

    elif action == "set":
        if not text:
            await ctx.send("セットするテキストを指定してください。")
            return
        escaped = text.replace("'", "'\\''")
        out = await run_shell(
            f"echo '{escaped}' | xclip -selection clipboard 2>/dev/null || "
            f"echo '{escaped}' | xsel --clipboard --input 2>/dev/null || "
            f"echo '{escaped}' | wl-copy 2>/dev/null"
        )
        await ctx.send(f"クリップボードにセットしました: `{text[:100]}`")
    else:
        await ctx.send("`!clip get` または `!clip set <テキスト>` を指定してください。")


# ════════════════════════════════════════════════════════════
#  マウス・キーボード操作 (xdotool)
# ════════════════════════════════════════════════════════════

@bot.command(name="click")
async def cmd_click(ctx: commands.Context, x: int, y: int, button: int = 1):
    """マウスを移動してクリック。例: !click 960 540  / !click 960 540 3 (右クリック)"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    out = await run_shell(f"DISPLAY=:0 xdotool mousemove {x} {y} click {button} 2>&1")
    result = out or "クリックしました。"
    await ctx.send(f"`({x}, {y})` ボタン{button}をクリック。" + (f"\n```\n{out}\n```" if out else ""))


@bot.command(name="type")
async def cmd_type(ctx: commands.Context, *, text: str):
    """キーボードでテキストを入力。例: !type Hello World"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    escaped = text.replace("'", "'\\''")
    out = await run_shell(f"DISPLAY=:0 xdotool type --clearmodifiers '{escaped}' 2>&1")
    await ctx.send(f"入力しました: `{text[:100]}`" + (f"\n```\n{out}\n```" if out else ""))


@bot.command(name="key")
async def cmd_key(ctx: commands.Context, *, keys: str):
    """キーを送信。例: !key ctrl+c  / !key Return  / !key alt+F4"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return
    out = await run_shell(f"DISPLAY=:0 xdotool key {keys} 2>&1")
    await ctx.send(f"キー送信: `{keys}`" + (f"\n```\n{out}\n```" if out else ""))


# ════════════════════════════════════════════════════════════
#  ウィンドウ管理
# ════════════════════════════════════════════════════════════

@bot.command(name="windows", aliases=["wins"])
async def cmd_windows(ctx: commands.Context):
    """開いているウィンドウの一覧を表示。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return

    out = await run_shell("DISPLAY=:0 wmctrl -l 2>/dev/null")
    if not out:
        await ctx.send(
            "ウィンドウ一覧を取得できません。\n"
            "```\nsudo apt install wmctrl\n```"
        )
        return

    # wmctrl -l の出力: 0x00400001  0  hostname  Title
    lines = ["```", f"{'ID':<14} {'タイトル'}"]
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 4:
            win_id, _, _, title = parts
            lines.append(f"{win_id:<14} {title}")
        elif len(parts) == 3:
            win_id = parts[0]
            lines.append(f"{win_id:<14} (タイトルなし)")
    lines.append("```")
    await ctx.send("\n".join(lines))


@bot.command(name="focus")
async def cmd_focus(ctx: commands.Context, *, title: str):
    """
    ウィンドウを最前面に表示。タイトルまたはウィンドウIDで指定。
      !focus Terminal
      !focus 0x04000003
    """
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return

    # 0x... 形式ならIDとして直接使用
    if title.startswith("0x"):
        out = await run_shell(f"DISPLAY=:0 wmctrl -ia '{title}' 2>&1")
    else:
        out = await run_shell(f"DISPLAY=:0 wmctrl -a '{title}' 2>&1")

    if out:
        await ctx.send(f"フォーカス失敗:\n```\n{out[:300]}\n```\n`!windows` でウィンドウIDを確認してください。")
    else:
        await ctx.send(f"ウィンドウ `{title}` を最前面に表示しました。")


@bot.command(name="wclose")
async def cmd_wclose(ctx: commands.Context, *, title: str):
    """
    特定のウィンドウだけを閉じる（他のインスタンスは残る）。タイトルまたはIDで指定。
      !wclose 0x04000001
      !wclose GitHub — Firefox
    """
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return

    # 対象ウィンドウを先に表示
    if title.startswith("0x"):
        info = await run_shell(f"DISPLAY=:0 wmctrl -l 2>/dev/null | grep -i '{title}'")
    else:
        info = await run_shell(f"DISPLAY=:0 wmctrl -l 2>/dev/null | grep -i '{title}' | head -5")

    if not info:
        await ctx.send(f"`{title}` に一致するウィンドウが見つかりません。`!windows` で一覧を確認してください。")
        return

    await ctx.send(f"以下のウィンドウを閉じます:\n```\n{info[:500]}\n```")
    if not await confirm(ctx, "このウィンドウを閉じますか？"):
        return

    if title.startswith("0x"):
        out = await run_shell(f"DISPLAY=:0 wmctrl -ic '{title}' 2>&1")
    else:
        out = await run_shell(f"DISPLAY=:0 wmctrl -c '{title}' 2>&1")

    if out:
        await ctx.send(f"閉じるのに失敗しました:\n```\n{out[:300]}\n```")
    else:
        await ctx.send(f"ウィンドウ `{title}` を閉じました。")


# ════════════════════════════════════════════════════════════
#  スラッシュコマンド
# ════════════════════════════════════════════════════════════

@bot.tree.command(name="run", description="シェルコマンドを実行して結果を送信")
@app_commands.describe(cmd="実行するコマンド")
async def slash_run(interaction: discord.Interaction, cmd: str):
    if not is_authorized_i(interaction):
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    if (b := is_blocked(cmd)):
        await interaction.response.send_message(f"ブロックされたコマンド: `{b}`", ephemeral=True)
        return
    await interaction.response.defer()
    out = await run_shell(cmd) or "(出力なし)"
    for i in range(0, len(out), 1900):
        await interaction.followup.send(f"```\n{out[i:i+1900]}\n```")


@bot.tree.command(name="sysinfo", description="CPU・メモリ・ディスク情報を表示")
async def slash_sysinfo(interaction: discord.Interaction):
    if not is_authorized_i(interaction):
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    await interaction.response.defer()
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    embed = discord.Embed(title="システム情報", color=discord.Color.blue())
    embed.add_field(name="CPU", value=f"{cpu}%", inline=True)
    embed.add_field(name="メモリ", value=f"{human_size(mem.used)} / {human_size(mem.total)} ({mem.percent}%)", inline=True)
    embed.add_field(name="ディスク (/)", value=f"{human_size(disk.used)} / {human_size(disk.total)} ({disk.percent}%)", inline=True)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="screenshot", description="スクリーンショットを送信（省略=全体 / window=アクティブ / アプリ名=指定）")
@app_commands.describe(target="省略:画面全体 / window:フォーカス中 / アプリ名:ウィンドウ指定")
async def slash_screenshot(interaction: discord.Interaction, target: str = ""):
    if not is_authorized_i(interaction):
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    await interaction.response.defer()
    ok, desc = await _take_screenshot(target or None)
    if not ok:
        await interaction.followup.send(desc)
        return
    await interaction.followup.send(
        f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}` — {desc}",
        file=discord.File(SCREENSHOT_PATH, filename="screenshot.png"),
    )


@bot.tree.command(name="ps", description="CPU使用率上位のプロセスを表示")
@app_commands.describe(count="表示件数（デフォルト15）")
async def slash_ps(interaction: discord.Interaction, count: int = 15):
    if not is_authorized_i(interaction):
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    await interaction.response.defer()
    procs = sorted(
        psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
        key=lambda p: p.info["cpu_percent"] or 0, reverse=True,
    )[:count]
    lines = [f"{'PID':>6}  {'CPU%':>5}  {'MEM%':>5}  名前", "-" * 38]
    for p in procs:
        lines.append(f"{p.info['pid']:>6}  {p.info['cpu_percent'] or 0:>5.1f}  {p.info['memory_percent'] or 0:>5.1f}  {p.info['name']}")
    await interaction.followup.send("```\n" + "\n".join(lines) + "\n```")


@bot.tree.command(name="log", description="ログファイルの末尾を表示")
@app_commands.describe(path="ログファイルのパス", lines="表示行数（デフォルト50）")
async def slash_log(interaction: discord.Interaction, path: str, lines: int = 50):
    if not is_authorized_i(interaction):
        await interaction.response.send_message("権限がありません。", ephemeral=True)
        return
    await interaction.response.defer()
    target = Path(path).expanduser()
    if not target.is_file():
        await interaction.followup.send(f"ファイルが見つかりません: `{target}`")
        return
    out = await run_shell(f"tail -n {lines} {target}") or "(出力なし)"
    for i in range(0, len(out), 1900):
        await interaction.followup.send(f"```\n{out[i:i+1900]}\n```")


# ════════════════════════════════════════════════════════════
#  永続設定・メタ
# ════════════════════════════════════════════════════════════

@bot.command(name="settings", aliases=["config"])
async def cmd_settings(ctx: commands.Context):
    """永続設定の一覧を表示（再起動後も保持される設定）。"""
    if not is_authorized(ctx):
        await ctx.send("権限がありません。")
        return

    embed = discord.Embed(
        title="永続設定一覧",
        description=f"保存先: `{CONFIG_PATH.resolve()}`",
        color=discord.Color.purple(),
    )

    # アラート
    alert_lines = []
    for ch_id_str, thresholds in _cfg.get("alerts", {}).items():
        ch = bot.get_channel(int(ch_id_str))
        ch_name = f"#{ch.name}" if ch else f"ch:{ch_id_str}"
        parts = " / ".join(f"{k.upper()}>{v}%" for k, v in thresholds.items())
        alert_lines.append(f"{ch_name}: {parts}")
    embed.add_field(
        name="アラート (`!alert` / `!unalert`)",
        value="\n".join(alert_lines) or "(なし)",
        inline=False,
    )

    # 自動ログ監視
    watch_lines = []
    for cid_str, paths in _cfg.get("autowatch", {}).items():
        ch = bot.get_channel(int(cid_str))
        ch_name = f"#{ch.name}" if ch else f"ch:{cid_str}"
        for p in paths:
            running = "▶" if (int(cid_str), p) in _watchers else "⏹"
            watch_lines.append(f"{running} `{p}` ({ch_name})")
    embed.add_field(
        name="自動ログ監視 (`!autowatch`) — 起動時に自動再開",
        value="\n".join(watch_lines) or "(なし)",
        inline=False,
    )

    # 自動スクリーンショット
    shot_lines = []
    for cid_str, interval in _cfg.get("autoshot", {}).items():
        ch = bot.get_channel(int(cid_str))
        ch_name = f"#{ch.name}" if ch else f"ch:{cid_str}"
        running = "▶" if int(cid_str) in _autoshoot else "⏹"
        shot_lines.append(f"{running} {ch_name}: {interval}秒ごと")
    embed.add_field(
        name="自動スクリーンショット (`!autoshot`) — 起動時に自動再開",
        value="\n".join(shot_lines) or "(なし)",
        inline=False,
    )

    # 動的ブロックコマンド
    dynamic_blocked = _cfg.get("blocked", [])
    embed.add_field(
        name="動的ブロックコマンド (`!block add/remove`)",
        value=("\n".join(f"`{b}`" for b in dynamic_blocked) or "(なし)"),
        inline=False,
    )

    embed.set_footer(text="▶=現在動作中  ⏹=設定のみ（次回起動時に復元）")
    await ctx.send(embed=embed)


@bot.command(name="sync")
async def cmd_sync(ctx: commands.Context):
    """スラッシュコマンドをDiscordに同期（初回のみ）。"""
    if ALLOWED_USER_IDS and ctx.author.id not in ALLOWED_USER_IDS:
        await ctx.send("権限がありません。")
        return
    synced = await bot.tree.sync()
    await ctx.send(f"{len(synced)} 個のスラッシュコマンドを同期しました。")


@bot.command(name="cmds")
async def cmd_help(ctx: commands.Context):
    """コマンド一覧を表示。"""
    embed = discord.Embed(title="コマンド一覧", color=discord.Color.green())
    sections = [
        ("シェル実行", [
            ("!run <cmd>",          "コマンド実行（リアルタイムストリーミング）"),
            ("!bg <cmd>",           "バックグラウンド実行（完了時に通知）"),
            ("!jobs / !stop <id>",  "ジョブ一覧 / 停止"),
            ("!input <id> <text>",  "ジョブにキー入力を送信"),
        ]),
        ("ターミナルセッション", [
            ("!session start [名前]",   "セッション開始（複数同時OK）"),
            ("!session end/list [名前]", "セッション終了 / 一覧"),
            ("!s <cmd>",               "mainセッションで実行"),
            ("!s <名前> <cmd>",         "指定セッションで実行（cd/exportが引き継がれる）"),
        ]),
        ("ファイル管理", [
            ("!ls / !cat / !rm",        "一覧 / 表示 / 削除"),
            ("!download / !upload",     "ファイル送受信（JPG等の画像も可）"),
            ("!find <パターン> [パス]",  "ファイル名で検索"),
            ("!grep <パターン> [パス]",  "ファイル内容で検索"),
        ]),
        ("システム情報", [
            ("!sysinfo",                "CPU・メモリ・ディスク"),
            ("!meminfo",                "メモリ詳細（キャッシュ・Swap・Top5）"),
            ("!temp",                   "CPU/GPU温度"),
            ("!gpu",                    "GPU使用率・VRAM（NVIDIA/AMD）"),
            ("!ps [件数]",              "プロセス一覧（CPU順）"),
            ("!kill <PID>",             "プロセス終了（PID指定）"),
            ("!killapp <名前>",         "アプリ名でプロセスを終了"),
            ("!service <act> <name>",   "systemdサービス操作"),
        ]),
        ("ネットワーク", [
            ("!netinfo",                "IP・インターフェース・ルート"),
            ("!ping <host> [回数]",     "疎通確認"),
            ("!curl <URL> [オプション]", "HTTPリクエストを送信"),
        ]),
        ("電源管理", [
            ("!shutdown [分]",          "シャットダウン（確認あり）"),
            ("!reboot [os名]",           "再起動。`!reboot windows` でWindows起動"),
            ("!sleep",                  "スリープ（確認あり）"),
            ("!bootentries",            "GRUBの起動エントリ一覧を表示"),
            ("!bootinto <エントリ>",    "次回のみ指定OSで起動してreboot"),
        ]),
        ("Docker", [
            ("!docker ps",                  "コンテナ一覧"),
            ("!docker start/stop/restart <名前>", "コンテナ操作"),
            ("!docker logs <名前>",          "ログを表示"),
        ]),
        ("ログ・監視", [
            ("!log <パス> [行数]",          "末尾N行を送信"),
            ("!watch / !unwatch <パス>",    "リアルタイム監視（複数ファイル同時OK）"),
            ("!autowatch add/remove/list",  "起動時に自動再開する監視を管理"),
            ("!iwatch / !iunwatch <パス>",  "ファイル変更を通知（inotify）"),
            ("!alert <cpu|mem|disk> <%>",   "しきい値アラートを設定"),
        ]),
        ("画面・カメラ", [
            ("!ss",              "画面全体のスクリーンショット"),
            ("!ss window",       "フォーカス中のウィンドウ"),
            ("!ss <アプリ名>",    "アプリ名でウィンドウ指定"),
            ("!autoshot [秒]",   "定期スクリーンショット自動送信"),
            ("!stopauto",        "自動送信停止"),
            ("!camera [device]", "Webカメラで撮影して送信"),
        ]),
        ("ウィンドウ管理", [
            ("!windows",              "開いているウィンドウ一覧（ID・タイトル）"),
            ("!focus <タイトル|ID>",  "ウィンドウを最前面に表示"),
            ("!wclose <タイトル|ID>", "特定のウィンドウだけを閉じる"),
        ]),
        ("クリップボード・入力", [
            ("!clip get",           "クリップボードの内容をDiscordに送信"),
            ("!clip set <テキスト>", "クリップボードにテキストをセット"),
            ("!click <x> <y> [btn]", "マウスクリック（xdotool）"),
            ("!type <テキスト>",     "テキスト入力（xdotool）"),
            ("!key <キー>",          "キー送信（例: ctrl+c, Return）"),
        ]),
        ("永続設定", [
            ("!settings",                       "全永続設定を一覧表示（再起動後も保持）"),
            ("!block list/add/remove <cmd>",    "ブロックコマンドを動的管理"),
            ("!autowatch list/add/remove <path>","ログ自動監視を管理（起動時に復元）"),
            ("!autoshot [秒] / !stopauto",       "自動スクショ（設定は再起動後も復元）"),
            ("!alert / !unalert",                "アラート（設定は再起動後も復元）"),
        ]),
        ("スラッシュコマンド", [
            ("/run /sysinfo /screenshot /ps /log", "主要コマンドのスラッシュ版"),
            ("!sync",              "スラッシュコマンドをDiscordに同期（初回のみ）"),
        ]),
    ]
    for title, items in sections:
        embed.add_field(
            name=title,
            value="\n".join(f"`{c}` — {d}" for c, d in items),
            inline=False,
        )
    await ctx.send(embed=embed)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(".env に DISCORD_TOKEN が未設定です。")
    bot.run(TOKEN)
