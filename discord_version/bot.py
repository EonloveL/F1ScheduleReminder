"""
Discord 平台适配层

实现与 common/scheduler.py 期望的 notify_bot 接口同名方法
（send_session_reminder / send_race_result / send_standings_update /
send_session_result / send_markdown_message / send_dm_markdown /
send_group_message / send_image_url / send_daily_schedule /
send_weekly_preview / send_startup_message）。

与 QQ 版差异：
- 图表/头像不走公网 URL：本地文件直接上传（data/exports、web/static/avatars），
  无法映射到本地的 http(s) URL 则作为 embed 远程图交给 Discord 抓取
- QQ markdown（\\r 换行、![pic#WxH](url) 图片语法）自动转换为 Discord 标准 markdown
- 时间用 Discord 原生 <t:epoch> 标记，按各用户本地时区渲染（海外多时区友好）
- scheduler 传入的 QQ 键盘 payload 直接忽略
"""
import asyncio
import logging
import os
import re
from datetime import datetime, timezone as _tzu
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from i18n import T, session_name
from common.secret_guard import scrub_text

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORTS_DIR = os.path.join(REPO_ROOT, "data", "exports")
AVATAR_STATIC_DIR = os.path.join(REPO_ROOT, "web", "static", "avatars")

MAX_MSG_LEN = 1990  # Discord 单条 2000 上限，留余量

# QQ md 图片语法 ![pic#800px #450px](url) 与标准 ![](url)
_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


def _ts(dt: datetime, style: str = "F") -> str:
    """Discord 原生时间戳标记（按用户本地时区渲染）"""
    return f"<t:{int(dt.timestamp())}:{style}>"


def split_text(text: str, limit: int = MAX_MSG_LEN) -> List[str]:
    """按行边界切分长文本"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            if cur:
                chunks.append(cur)
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]


class DiscordBot:
    """Discord 机器人封装（scheduler 调用面为同步方法，内部投递到 bot 事件循环）"""

    def __init__(self, token: str, channel_ids: List[int], proxy: Optional[str] = None,
                 tz: str = "UTC", ping_everyone: bool = False,
                 guild_ids: Optional[List[int]] = None):
        self.token = token
        self.channel_ids = channel_ids
        self.channels: List[discord.abc.Messageable] = []
        self.ping_everyone = ping_everyone
        self.guild_ids = guild_ids  # slash 指令即时同步用（开发期）；None=全局（最长1h生效）

        from pytz import timezone as _ptz
        try:
            self.tz = _ptz(tz)
        except Exception:
            logger.warning(f"未知时区 {tz}，回退 UTC")
            self.tz = _ptz("UTC")

        intents = discord.Intents.default()
        intents.message_content = True
        kwargs: Dict[str, Any] = {"command_prefix": "!", "intents": intents}
        if proxy:
            kwargs["proxy"] = proxy
            logger.info(f"已配置代理: {proxy}")
        self.bot = commands.Bot(**kwargs)
        self.bot.event(self.on_ready)

    # ==================== 生命周期 ====================

    async def on_ready(self):
        logger.info(f"✓ Discord Bot 已登录: {self.bot.user.name} (ID: {self.bot.user.id})")
        self.channels = []
        for cid in self.channel_ids:
            ch = self.bot.get_channel(cid)
            if ch is None:
                try:
                    ch = await self.bot.fetch_channel(cid)
                except Exception as e:
                    logger.error(f"✗ 无法找到频道 {cid}: {e}")
                    continue
            self.channels.append(ch)
            logger.info(f"✓ 已连接频道: #{getattr(ch, 'name', '?')} (ID: {cid})")
        if not self.channels:
            logger.error("✗ 没有可用频道，推送将无法发送（检查 DISCORD_CHANNEL_IDS 与 Bot 邀请）")

    def run(self):
        self.bot.run(self.token)

    # ==================== 图片解析 ====================

    @staticmethod
    def _resolve_image(url: str) -> Optional[str]:
        """URL → 本地文件路径（存在则返回路径，否则返回原 URL，均不可用以 None 表示丢弃）"""
        fname = url.rstrip("/").rsplit("/", 1)[-1]
        # data/exports 图表（含 local:// 伪协议与 {RATING_BASE_URL}/exports/ 两种形态）
        if "/exports/" in url or url.startswith("local://"):
            p = os.path.join(EXPORTS_DIR, fname)
            if os.path.isfile(p):
                return p
        # 头像：显式 local:// 前缀 + /av{n}/ 路径两种形态
        # （local://files/av8/drivers/x.png 与 https://host/av8/drivers/x.png 都落这里）
        m = re.search(r"/(drivers|teams)/([^/]+\.png)$", url)
        if m and (url.startswith("local://") or "/av" in url or "/avatar" in url):
            p = os.path.join(AVATAR_STATIC_DIR, m.group(1), m.group(2))
            if os.path.isfile(p):
                return p
        if url.startswith(("http://", "https://")):
            return url  # 远程图，embed 交给 Discord 抓取
        return None

    # ==================== 底层发送 ====================

    def _submit(self, coro, timeout: int = 30) -> bool:
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
            return bool(fut.result(timeout=timeout))
        except Exception as e:
            logger.error(f"Discord 发送失败: {e}")
            try:
                coro.close()  # 未投入事件循环的协程须显式关闭，防 RuntimeWarning 刷屏（review 中 #5）
            except Exception:
                pass
            return False

    MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 免费档附件上限留余量（review 中 #6：超限整条消息失败）

    @staticmethod
    def _build_image_payload(urls: List[str]):
        """构造 (files, embeds)：本地图上传+attachment 引用，远程图直接 embed"""
        files, embeds = [], []
        if len(urls) > 10:
            logger.warning(f"图片数量 {len(urls)} 超 Discord embed 上限，仅发送前 10 张")
        for u in urls[:10]:
            resolved = DiscordBot._resolve_image(u)
            if not resolved:
                continue
            if resolved.startswith(("http://", "https://")):
                embeds.append(discord.Embed().set_image(url=resolved))
                continue
            # 本地文件（http 判定必须在文件判定之前：Linux 下 os.path.sep="/" 会把
            # URL 误判为路径——review 高 #2，顺序勿回退）
            try:
                if os.path.getsize(resolved) > DiscordBot.MAX_UPLOAD_BYTES:
                    logger.warning(f"图片超附件大小上限，跳过: {resolved}")
                    continue
            except OSError:
                continue
            fname = os.path.basename(resolved)
            files.append(discord.File(resolved, filename=fname))
            embeds.append(discord.Embed().set_image(url=f"attachment://{fname}"))
        return files, embeds

    async def _send_channel(self, target, content: str = None,
                            image_urls: List[str] = None) -> bool:
        files, embeds = self._build_image_payload(image_urls or [])
        chunks = split_text(scrub_text(content)) if content else [None]  # 出网门禁脱敏
        try:
            for i, chunk in enumerate(chunks):
                kwargs: Dict[str, Any] = {}
                if chunk:
                    kwargs["content"] = chunk
                if i == 0:
                    if files:
                        kwargs["files"] = files
                    if embeds:
                        kwargs["embeds"] = embeds
                if not kwargs:
                    continue
                await target.send(**kwargs)
            return True
        except discord.Forbidden as e:
            logger.error(f"无权限发送到 {target}: {e}")
            return False
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False

    async def _broadcast(self, content: str = None, image_urls: List[str] = None) -> bool:
        if not self.channels:
            logger.error("频道未就绪，无法发送")
            return False
        ok = True
        for ch in self.channels:
            ok = bool(await self._send_channel(ch, content, image_urls)) and ok
        return ok

    # ==================== QQ md → Discord md ====================

    @staticmethod
    def convert_md(md: str) -> (str, List[str]):
        """返回 (转换后文本, 图片URL列表)。\\r→\\n，图片语法剥出另行上传"""
        images = _IMG_RE.findall(md or "")
        text = _IMG_RE.sub("", md or "")
        text = text.replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text, images

    # ==================== scheduler 接口（同步） ====================

    def send_group_message(self, content: str, at_all: bool = False, mentions: list = None) -> bool:
        prefix = ""
        if at_all and self.ping_everyone:
            prefix = "@everyone "
        if mentions:
            prefix += " ".join(f"<@{m}>" for m in mentions) + " "
        return self._submit(self._broadcast(prefix + content))

    def send_markdown_message(self, markdown_content: str, fallback_text: str = None,
                              keyboard: Dict[str, Any] = None) -> bool:
        text, images = self.convert_md(markdown_content)
        if not text and fallback_text:
            text = fallback_text
        if not text and not images:
            return False
        return self._submit(self._broadcast(text, images))

    def send_dm_markdown(self, user_id: str, markdown_content: str, fallback_text: str = None) -> bool:
        text, images = self.convert_md(markdown_content)
        if not text:
            text = fallback_text or ""
        if not text and not images:
            return False

        async def _dm() -> bool:
            try:
                user = await self.bot.fetch_user(int(user_id))
            except Exception as e:
                logger.warning(f"DM 用户获取失败({str(user_id)[:8]}...): {e}")
                return False
            return await self._send_channel(user, text or None, images)

        return self._submit(_dm())

    def send_image_url(self, image_url: str, content: str = " ") -> bool:
        text = content if content and content.strip() else None
        return self._submit(self._broadcast(text, [image_url]))

    # ==================== 业务卡片 ====================

    _ICONS = {"fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️", "qualifying": "⏱️",
              "sprint": "⚡", "sprint_qualifying": "⏱️", "race": "🏁"}
    _MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

    def send_session_reminder(self, session: Dict[str, Any], weather_text: str = None) -> bool:
        utc_time = session["datetime"]
        icon = self._ICONS.get(session["type"], "🏎️")
        sname = session_name(session)

        countdown = ""
        try:
            delta = utc_time - datetime.now(_tzu.utc)
            total = int(delta.total_seconds() // 60)
            if total > 0:
                h, m = divmod(total, 60)
                if h >= 24:
                    d, h = divmod(h, 24)
                    countdown = T("countdown_days", d=d, h=h)
                elif h > 0:
                    countdown = T("countdown_hours", h=h, m=m)
                else:
                    countdown = T("countdown_mins", m=m)
        except Exception:
            pass

        embed = discord.Embed(
            title=T("reminder_title", icon=icon, race=session["race_name"]),
            description=T("reminder_starting", session=sname),
            color=discord.Color.red(),
        )
        embed.add_field(name=T("field_circuit"), value=session.get("circuit", "?"), inline=False)
        time_val = f"{_ts(utc_time, 'F')}\n{_ts(utc_time, 'R')}"
        if countdown:
            time_val += f"\n{countdown}"
        embed.add_field(name=T("field_time"), value=time_val, inline=False)
        if weather_text:
            embed.add_field(name=T("field_weather"), value=weather_text, inline=False)
        embed.set_footer(text=T("enjoy"))

        ping = "@everyone" if self.ping_everyone else None

        async def _send() -> bool:
            ok = True
            for ch in self.channels:
                try:
                    await ch.send(content=ping, embed=embed)
                except Exception as e:
                    logger.error(f"提醒发送失败: {e}")
                    ok = False
            return ok

        return self._submit(_send())

    def send_race_result(self, race_data: Dict[str, Any], results: Dict[str, Any],
                         lap_record_info: str = None, keyboard: Dict[str, Any] = None) -> bool:
        race_name = race_data.get("raceName", "F1 Grand Prix")
        if "Results" not in results:
            logger.warning(f"比赛结果缺 Results 键，不推空卡: {race_name}")
            return False

        lines, dnf = [], []
        for i, r in enumerate(results["Results"], 1):
            driver = r.get("Driver", {})
            name = driver.get("familyName", "Unknown")
            team = r.get("Constructor", {}).get("name", "Unknown")
            status = r.get("status", "")
            finished = status in ("Finished",) or status.startswith("+")
            tstr = (r.get("Time") or {}).get("time", "") if finished else ""
            medal = self._MEDALS.get(i, f"**{i}.**")
            line = f"{medal} **{name}** ({team})"
            if tstr:
                line += f" `{tstr}`"
            if not finished:
                line += f" ⚠️{status}"
                dnf.append(name)
            lines.append(line)

        for r in results["Results"]:
            fl = r.get("FastestLap") or {}
            if fl.get("rank") == "1":
                lines.append("")
                lines.append(T("fastest_lap",
                               driver=r.get("Driver", {}).get("familyName", ""),
                               time=(fl.get("Time") or {}).get("time", "")))
                break
        if dnf:
            lines.append(T("dnf_line", names=", ".join(dnf)))
        if lap_record_info:
            lines.append(lap_record_info)

        embed = discord.Embed(
            title=T("race_result_title", race=race_name),
            description="\n".join(lines)[:4000],
            color=discord.Color.gold(),
        )

        async def _send() -> bool:
            ok = True
            for ch in self.channels:
                try:
                    await ch.send(embed=embed)
                except Exception as e:
                    logger.error(f"比赛结果发送失败: {e}")
                    ok = False
            return ok

        return self._submit(_send())

    def send_standings_update(self, standings: Dict[str, Any], race_name: str = "",
                              keyboard: Dict[str, Any] = None) -> bool:
        from common.standings_format import parse_driver_standings
        entries = parse_driver_standings(standings.get("drivers") or {})
        if not entries:
            return False
        title = (T("driver_standings_after", race=race_name) if race_name
                 else T("driver_standings_plain"))
        lines = []
        for e in entries:
            pos = int(e.get("position", 0))
            name = e.get("Driver", {}).get("familyName", "Unknown")
            points = e.get("points", "0")
            wins = e.get("wins", "0")
            rank = self._MEDALS.get(pos, f"**{pos}.**")
            win_str = f" {T('wins_fmt', wins=wins)}" if wins != "0" else ""
            lines.append(f"{rank} **{name}** — {points} {T('points_unit')}{win_str}")
        embed = discord.Embed(title=title, description="\n".join(lines)[:4000],
                              color=discord.Color.blue())

        async def _send() -> bool:
            ok = True
            for ch in self.channels:
                try:
                    await ch.send(embed=embed)
                except Exception as e:
                    logger.error(f"积分榜发送失败: {e}")
                    ok = False
            return ok

        return self._submit(_send())

    def send_session_result(self, result: Dict[str, Any], watch_driver_ids: set = None) -> bool:
        if not result or not result.get("entries"):
            return False
        watch_driver_ids = watch_driver_ids or set()
        race_name = result.get("race_name", "")
        sname = result.get("session_name", "")
        lines = []
        for e in result["entries"]:
            pos = e["position"]
            mark = " ⭐" if e.get("driver_id") in watch_driver_ids else ""
            tstr = f" `{e['time']}`" if e.get("time") else ""
            pts = f" (+{e['points']} {T('points_unit')})" if e.get("points") else ""
            rank = self._MEDALS.get(pos, f"**{pos}.**")
            lines.append(f"{rank} **{e['driver_name']}**{tstr}{pts}{mark}")
        embed = discord.Embed(
            title=T("session_result_title", session=sname),
            description=f"> {race_name}\n" + "\n".join(lines)[:3900],
            color=discord.Color.blue(),
        )

        async def _send() -> bool:
            ok = True
            for ch in self.channels:
                try:
                    await ch.send(embed=embed)
                except Exception as e:
                    logger.error(f"环节成绩发送失败: {e}")
                    ok = False
            return ok

        return self._submit(_send())

    def send_daily_schedule(self, sessions: List[Dict[str, Any]]) -> bool:
        if not sessions:
            return False
        race_name = sessions[0]["race_name"]
        embed = discord.Embed(
            title=T("daily_title", race=race_name),
            description=f"📍 {sessions[0].get('circuit', '')}",
            color=discord.Color.blue(),
        )
        for s in sessions:
            icon = self._ICONS.get(s["type"], "🏎️")
            embed.add_field(name=f"{icon} {session_name(s)}",
                            value=f"{_ts(s['datetime'], 'F')} ({_ts(s['datetime'], 'R')})",
                            inline=False)
        embed.set_footer(text=T("daily_footer"))

        async def _send() -> bool:
            ok = True
            for ch in self.channels:
                try:
                    await ch.send(content="@everyone" if self.ping_everyone else None,
                                  embed=embed)
                except Exception as e:
                    logger.error(f"每日赛程发送失败: {e}")
                    ok = False
            return ok

        return self._submit(_send())

    def send_weekly_preview(self, race_data: Dict[str, Any], sessions: List[Dict[str, Any]],
                            circuit_data: Dict[str, Any], llm_verified: bool = False) -> bool:
        race_name = race_data.get("raceName", "F1 Grand Prix")
        flag = circuit_data.get("flag", "🏁")
        circuit_name = circuit_data.get("name") or race_data.get("Circuit", {}).get("circuitName", "")

        circuit_lines = []
        if circuit_data.get("first_race"):
            circuit_lines.append(f"• {T('first_race', year=circuit_data['first_race'])}")
        if circuit_data.get("lap_length_km"):
            circuit_lines.append(f"• {T('lap_length', km=circuit_data['lap_length_km'])}")
        if circuit_data.get("laps"):
            circuit_lines.append(f"• {T('race_laps', laps=circuit_data['laps'])}")
        if circuit_data.get("race_distance_km"):
            circuit_lines.append(f"• {T('race_distance', km=circuit_data['race_distance_km'])}")
        lap_record = circuit_data.get("lap_record", {})
        if lap_record.get("time"):
            circuit_lines.append(f"• {T('lap_record', time=lap_record['time'], driver=lap_record.get('driver', ''), year=lap_record.get('year', ''))}")
        if llm_verified:
            circuit_lines.append(T("record_verified"))

        embed = discord.Embed(
            title=T("weekly_title"),
            description=f"{T('weekly_this_week', flag=flag, race=race_name)}\n📍 {circuit_name}",
            color=discord.Color.purple(),
        )
        embed.add_field(name=T("circuit_info"),
                        value="\n".join(circuit_lines) if circuit_lines else T("no_circuit_data"),
                        inline=False)
        times = "\n".join(
            f"{self._ICONS.get(s['type'], '🏎️')} **{session_name(s)}** — {_ts(s['datetime'], 'f')}"
            for s in sessions
        )
        if times:
            embed.add_field(name=T("session_times"), value=times, inline=False)
        embed.set_footer(text=T("weekly_footer"))

        async def _send() -> bool:
            ok = True
            for ch in self.channels:
                try:
                    await ch.send(embed=embed)
                except Exception as e:
                    logger.error(f"每周预告发送失败: {e}")
                    ok = False
            return ok

        return self._submit(_send())

    def send_startup_message(self) -> bool:
        embed = discord.Embed(title=T("startup_title"), description=T("startup_body"),
                              color=discord.Color.green())

        async def _send() -> bool:
            ok = True
            for ch in self.channels:
                try:
                    await ch.send(embed=embed)
                except Exception as e:
                    logger.warning(f"启动消息发送失败: {e}")
                    ok = False
            return ok

        return self._submit(_send())

    def send_test_message(self) -> bool:
        return self.send_group_message(
            f"🧪 Test message — bot is configured correctly.\n"
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
