"""
F1赛程提醒机器人 - QQ官方群聊版本
基于QQ官方Bot API V2实现
支持：群聊消息、@全体成员、定时提醒

API文档：https://bot.q.qq.com/wiki/develop/api/
"""

import logging
import json
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import requests
import time

logger = logging.getLogger(__name__)

# 实时计时面板链接（默认复用 RATING_BASE_URL，可单独配置 LIVE_BASE_URL）
LIVE_BASE_URL = os.getenv("LIVE_BASE_URL") or os.getenv("RATING_BASE_URL") or "https://your-domain.example.com"

# 赛道数据管理器（惰性单例，用于赛道当地时间换算）
_circuits_mgr = None


def _get_circuits_mgr():
    global _circuits_mgr
    if _circuits_mgr is None:
        from .circuits_manager import CircuitsManager
        _circuits_mgr = CircuitsManager()
    return _circuits_mgr


def _track_tz(circuit_name: str) -> Optional[str]:
    """按赛道名查 IANA 时区；失败返回 None（调用方回退北京时间显示）"""
    try:
        return _get_circuits_mgr().get_circuit_timezone(circuit_name or "")
    except Exception:
        return None


def _dual_time_str(utc_dt, circuit_name: str = "") -> str:
    """双时区时间显示（赛道当地 / 北京时间）；无赛道时区时仅北京时间"""
    try:
        return _get_circuits_mgr().format_dual_time(utc_dt, _track_tz(circuit_name))
    except Exception:
        from pytz import timezone as _tz
        bj = utc_dt.astimezone(_tz('Asia/Shanghai'))
        return f"{bj.strftime('%m月%d日 %H:%M')}（北京时间）"


class QQGroupBot:
    """
    QQ官方群聊机器人封装类
    支持：群消息发送、@全体成员
    """
    
    # API基础URL
    API_BASE_URL = "https://api.sgroup.qq.com"
    SANDBOX_API_BASE_URL = "https://sandbox.api.sgroup.qq.com"
    TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
    
    def __init__(self, appid: str, secret: str, group_id: str = None, use_sandbox: bool = False,
                 group_ids: list = None):
        """
        初始化QQ官方群聊机器人
        
        Args:
            appid: QQ开放平台AppID
            secret: QQ开放平台AppSecret
            group_id: QQ群号（单群，兼容旧用法）
            use_sandbox: 是否使用沙箱环境（新机器人默认只能在沙箱环境测试）
            group_ids: 多群openid列表（优先于group_id）
        """
        import threading
        self.appid = appid
        self.secret = secret
        self.group_ids = list(group_ids) if group_ids else ([group_id] if group_id else [])
        self.group_id = self.group_ids[0] if self.group_ids else group_id
        self.use_sandbox = use_sandbox
        self.access_token = None
        self.token_expires_at = 0
        self._token_lock = threading.Lock()  # token刷新互斥（多线程并发发送时防重复刷新/互相清token）
        self._tl = threading.local()  # 广播时的当前群（线程隔离）
        # 已发送消息缓存 {message_id: content}，供"引用机器人卡片/消息"时被AI会话引用
        # 持久化到 data/sent_messages.json，容器重启后引用历史推送仍可用
        self._sent_messages = {}
        self._sent_ts = {}  # {message_id: 记录时间戳}，供TTL过期判断
        self._sent_lock = threading.Lock()
        self._sent_order = []  # 记录插入顺序以便淘汰
        self._sent_dirty = False
        self._load_sent_cache()
        
        # 设置API基础URL
        self.base_url = self.SANDBOX_API_BASE_URL if use_sandbox else self.API_BASE_URL
        
        env_name = "沙箱环境" if use_sandbox else "正式环境"
        logger.info(f"✓ QQ官方群聊机器人初始化成功 [{env_name}]")
        logger.info(f"  - 目标群: {len(self.group_ids)} 个 {self.group_ids}")
        
        # 立即获取一次token测试
        if self._get_access_token():
            logger.info("✓ Access Token获取成功")
        else:
            logger.warning("⚠️ Access Token获取失败，将在发送消息时重试")
    
    @property
    def _current_group(self):
        """广播上下文中的当前群；非广播时为主群"""
        return getattr(self._tl, 'gid', None) or self.group_id
    
    def _make_broadcast(fn):
        """包装公开发送方法：多群时遍历所有群发送；嵌套在广播内时直接单群执行"""
        import functools as _ft
        @_ft.wraps(fn)
        def wrapped(self, *args, **kwargs):
            if getattr(self._tl, 'gid', None):
                return fn(self, *args, **kwargs)
            if len(self.group_ids) <= 1:
                return fn(self, *args, **kwargs)
            ok_any = False
            for gid in self.group_ids:
                self._tl.gid = gid
                try:
                    ok_any = fn(self, *args, **kwargs) or ok_any
                except Exception as e:
                    logger.error(f"群 {gid[:8]}... 发送失败: {e}")
                finally:
                    self._tl.gid = None
            return ok_any
        return wrapped
    
    def _get_access_token(self, force_refresh: bool = False) -> bool:
        """获取QQ Bot Access Token（加锁+双重检查，防多线程并发重复刷新）"""
        # 快速路径：token 有效（提前60秒视为过期）
        if not force_refresh and self.access_token and time.time() < self.token_expires_at - 60:
            return True
        with self._token_lock:
            # 双重检查：等待锁期间其他线程可能已刷新
            if not force_refresh and self.access_token and time.time() < self.token_expires_at - 60:
                return True
            try:
                logger.debug("正在获取Access Token...")

                response = requests.post(
                    "https://bots.qq.com/app/getAppAccessToken",
                    json={
                        "appId": self.appid,
                        "clientSecret": self.secret
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                    proxies={"http": None, "https": None}
                )

                logger.debug(f"Token响应状态: {response.status_code}")

                if response.status_code == 200:
                    data = response.json()
                    logger.debug(f"Token响应: {data}")

                    if "access_token" in data:
                        self.access_token = data["access_token"]
                        expires_in = int(data.get("expires_in") or 7200)
                        self.token_expires_at = time.time() + expires_in
                        logger.info(f"✓ Access Token获取成功 (有效期{expires_in}秒)")
                        return True
                    else:
                        logger.error(f"Token响应中无access_token: {data}")
                        return False
                else:
                    logger.error(f"获取Token HTTP错误: {response.status_code} - {response.text}")
                    return False

            except requests.exceptions.RequestException as e:
                logger.error(f"获取Token请求异常: {e}")
                return False
            except Exception as e:
                logger.error(f"获取Token异常: {e}")
                return False
    
    def send_group_message(self, content: str, at_all: bool = False, mentions: list = None) -> bool:
        """
        发送消息到QQ群
        
        Args:
            content: 消息内容
            at_all: 是否@全体成员（需要机器人是群主或管理员）
            mentions: 要@的成员openid列表
            
        Returns:
            发送是否成功
        """
        try:
            # 确保有有效的token
            if not self._get_access_token():
                logger.error("无法获取Access Token，发送失败")
                return False
            
            # 构建消息
            message_content = content
            if mentions:
                mention_prefix = "".join(f"<@!{m}>" for m in mentions)
                message_content = f"{mention_prefix}\n{content}"
            if at_all:
                message_content = f"@全体成员\n{content}"
            
            # 构建请求
            headers = {
                "Authorization": f"QQBot {self.access_token}",
                "Content-Type": "application/json"
            }
            
            # 构建消息体 - 简化版本
            # 注意：QQ群消息不支持 mentions 字段，只能通过文本@全体成员
            import random
            message_data = {
                "content": message_content,
                "msg_type": 0,  # 文本消息
                "msg_seq": random.randint(1, 1000000)  # 去重序号，必填
            }
            
            logger.debug(f"发送消息到群 {self._current_group}")
            logger.debug(f"请求体: {message_data}")
            
            # 发送群消息
            response = requests.post(
                f"{self.base_url}/v2/groups/{self._current_group}/messages",
                headers=headers,
                json=message_data,
                timeout=10,
                proxies={"http": None, "https": None}
            )
            
            logger.debug(f"发送消息响应: {response.status_code} - {response.text}")
            
            if response.status_code == 200:
                result = response.json()
                if result.get("id") or result.get("code") == 0:
                    logger.info(f"✓ 群消息发送成功 (群: {self._current_group[:8]}...)")
                    self._record_sent(result.get("id"), message_content)
                    return True
                else:
                    logger.error(f"发送消息API错误: {result}")
                    return False
            elif response.status_code == 401:
                logger.warning("Token 失效(401)，强制刷新后重试一次")
                if self._get_access_token(force_refresh=True):
                    response = requests.post(
                        f"{self.base_url}/v2/groups/{self._current_group}/messages",
                        headers={
                            "Authorization": f"QQBot {self.access_token}",
                            "Content-Type": "application/json"
                        },
                        json=message_data,
                        timeout=10,
                        proxies={"http": None, "https": None}
                    )
                    if response.status_code == 200:
                        result = response.json()
                        if result.get("id") or result.get("code") == 0:
                            logger.info(f"✓ 群消息发送成功-重试 (群: {self._current_group[:8]}...)")
                            self._record_sent(result.get("id"), message_content)
                            return True
                logger.error(f"401 刷新重试后仍失败: {response.status_code} - {response.text[:200]}")
                return False
            elif response.status_code == 403:
                logger.error("权限不足，请检查机器人是否已在群内，或是否有发送消息权限")
                return False
            else:
                err_text = response.text
                logger.error(f"发送消息HTTP错误: {response.status_code} - {err_text}")
                if '"code":11255' in err_text or '"err_code":11255' in err_text:
                    logger.error("错误码 11255: 请求无效")
                    logger.error("可能原因：")
                    logger.error("  1. 机器人不在该群中")
                    logger.error("  2. 配置的是普通群号，但QQ Bot API V2 需要使用 group_openid")
                    logger.error("     请查看上方日志中的群列表，使用 group_openid 替换 .env 中的 QQ_GROUP_ID")
                return False
            
        except requests.exceptions.Timeout:
            logger.error("发送消息超时")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"发送消息请求异常: {e}")
            return False
        except Exception as e:
            logger.error(f"发送群消息异常: {e}")
            return False
    
    # ==================== 引用消息内容查询（供AI会话引用机器人卡片/消息） ====================

    SENT_CACHE_MAX = 200  # 已发送消息缓存上限（超出淘汰最旧）
    SENT_CACHE_TTL = 7 * 24 * 3600  # 持久化条目7天过期
    SENT_SAVE_EVERY = 10  # 每记录N条落盘一次（写入节流）

    @staticmethod
    def _sent_cache_file() -> str:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, "data", "sent_messages.json")

    def _load_sent_cache(self):
        """启动时从磁盘加载已发送消息缓存（过期条目丢弃）"""
        import time as _t
        path = self._sent_cache_file()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            now = _t.time()
            items = [(mid, v.get("c", ""), v.get("ts", 0)) for mid, v in raw.items()
                     if now - v.get("ts", 0) < self.SENT_CACHE_TTL and v.get("c")]
            items.sort(key=lambda x: x[2])  # 按时间升序恢复插入顺序
            for mid, content, ts in items[-self.SENT_CACHE_MAX:]:
                self._sent_messages[mid] = content
                self._sent_ts[mid] = ts
                self._sent_order.append(mid)
            logger.info(f"✓ 已发消息缓存已恢复: {len(self._sent_order)} 条")
        except Exception as e:
            logger.warning(f"已发消息缓存加载失败: {e}")

    def _save_sent_cache(self):
        """落盘已发送消息缓存（保留原始时间戳，供TTL过期）"""
        try:
            data = {mid: {"c": self._sent_messages[mid], "ts": self._sent_ts.get(mid, 0)}
                    for mid in self._sent_order if mid in self._sent_messages}
            path = self._sent_cache_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            self._sent_dirty = False
        except Exception as e:
            logger.warning(f"已发消息缓存保存失败: {e}")

    def _record_sent(self, message_id: str, content: str):
        """记录已发送消息的 id→内容，供引用时回查（每N条落盘）"""
        import time as _t
        if not message_id or not content:
            return
        with self._sent_lock:
            if message_id in self._sent_messages:
                self._sent_messages[message_id] = content
                return
            self._sent_messages[message_id] = content
            self._sent_ts[message_id] = _t.time()
            self._sent_order.append(message_id)
            while len(self._sent_order) > self.SENT_CACHE_MAX:
                oldest = self._sent_order.pop(0)
                self._sent_messages.pop(oldest, None)
                self._sent_ts.pop(oldest, None)
            self._sent_dirty = True
            if len(self._sent_order) % self.SENT_SAVE_EVERY == 0:
                self._save_sent_cache()

    def get_referenced_content(self, message_id: str) -> Optional[str]:
        """按 message_id 回查机器人此前发送的消息内容（引用机器人卡片/消息时用）"""
        if not message_id:
            return None
        with self._sent_lock:
            return self._sent_messages.get(message_id)

    # ==================== 业务方法（与企业微信版本保持一致接口） ====================

    def _send_card(self, md: str, text: str, at_all: bool = False,
                   ping_text: str = "👆 赛事动态，请查收") -> bool:
        """Markdown 卡片优先的主动推送

        卡片发送成功且 at_all=True 时，补发一条纯文本 @全体成员 短提示
        （QQ 群 md 卡片不支持 @全体）；卡片失败降级纯文本（保留 @全体）。
        """
        ok = self.send_markdown_message(md, fallback_text=None)
        if ok:
            if at_all:
                self.send_group_message(ping_text, at_all=True)
            return True
        return self.send_group_message(text, at_all=at_all)

    def send_session_reminder(self, session: Dict[str, Any], weather_text: str = None) -> bool:
        """发送比赛环节提醒消息（md卡片 + 双时区时间）"""
        from pytz import timezone

        utc_time = session['datetime']
        local_tz = timezone('Asia/Shanghai')
        local_time = utc_time.astimezone(local_tz)

        date_str = local_time.strftime("%Y年%m月%d日")
        time_str = local_time.strftime("%H:%M")
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][local_time.weekday()]
        dual_str = _dual_time_str(utc_time, session.get('circuit', ''))

        icons = {
            "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
            "qualifying": "⏱️", "sprint": "⚡", "sprint_qualifying": "⏱️", "race": "🏁"
        }

        icon = icons.get(session['type'], "🏎️")

        # 倒计时（pushplus版功能）
        countdown = ""
        try:
            from datetime import datetime as _dt, timezone as _tzu
            delta = utc_time - _dt.now(_tzu.utc)
            total_minutes = int(delta.total_seconds() // 60)
            if total_minutes > 0:
                hours, minutes = divmod(total_minutes, 60)
                if hours >= 24:
                    days, hours = divmod(hours, 24)
                    countdown = f"⏳ 距离开始还有约 {days}天{hours}小时"
                elif hours > 0:
                    countdown = f"⏳ 距离开始还有约 {hours}小时{minutes}分钟"
                else:
                    countdown = f"⏳ 距离开始还有约 {minutes}分钟"
        except Exception:
            pass

        weather_line = f"🌤️ {weather_text}" if weather_text else ""

        md = (f"## {icon} F1提醒：{session['race_name']}\r\r"
              f"📍 {session['circuit']}\r\r"
              f"**【{session['name']}】即将开始**\r\r"
              f"⏰ {dual_str}\r")
        if countdown:
            md += f"{countdown}\r"
        if weather_line:
            md += f"{weather_line}\r"
        md += (f"\r📺 [腾讯体育](https://sports.qq.com/) · "
               f"[腾讯F1专区](https://sports.qq.com/kbsweb/index.htm#100360)\r\r"
               f"📊 [实时计时面板]({LIVE_BASE_URL}/live)\r\r"
               "敬请期待精彩比赛！🏎️💨")

        text = f"""{icon} 【F1提醒】{icon}

🏆 {session['race_name']}
📍 {session['circuit']}

【{session['name']}】即将开始

⏰ {date_str} {weekday} {time_str} (北京时间)
{countdown}
{weather_line}
📺 观看地址：
腾讯体育 https://sports.qq.com/
腾讯F1专区 https://sports.qq.com/kbsweb/index.htm#100360

📊 实时计时面板：{LIVE_BASE_URL}/live

敬请期待精彩比赛！🏎️💨

━━━━━━━━━━━━━━━
#F1 #Formula1"""

        ping = f"{icon} {session['race_name']}【{session['name']}】即将开始，详见上方卡片"
        return self._send_card(md, text, at_all=True, ping_text=ping)
    
    def send_race_result(self, race_data: Dict[str, Any], results: Dict[str, Any],
                         lap_record_info: str = None) -> bool:
        """发送比赛结果（md卡片优先，完整名次+车手头像+车队图标）"""
        from .openf1_api import get_avatar_md, get_team_icon_md
        race_name = race_data.get('raceName', 'F1大奖赛')

        text = f"🏁 【{race_name} 比赛结果】\n\n"
        md = f"## 🏁 {race_name} 比赛结果\r\r"

        if "Results" in results:
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            dnf = []
            for i, result in enumerate(results["Results"], 1):
                driver = result.get("Driver", {})
                family_name = driver.get("familyName", "Unknown")
                team = result.get("Constructor", {}).get("name", "Unknown")
                status = result.get("status", "")
                finished = status in ("Finished",) or status.startswith("+")
                time_str = ((result.get("Time") or {}).get("time", "")) if finished else ""

                medal = medals.get(i, f"**{i}.**")
                text += f"{medal} {family_name} ({team}){(' - ' + time_str) if time_str else ''}" \
                        f"{'' if finished else ' - ' + status}\n"
                avatar = get_avatar_md(family_name)
                avatar = f"{avatar} " if avatar else ""
                ticon = get_team_icon_md(team)
                ticon = f"{ticon} " if ticon else ""
                md += (f"{medal} {avatar}**{family_name}** {ticon}{team}"
                       + (f" `{time_str}`" if time_str else "")
                       + ("" if finished else f" ⚠️{status}") + "\r")
                if not finished:
                    dnf.append(family_name)

            # 全场最快圈（rank=="1"）常由非冠军车手做出，需遍历全部结果
            for r in results["Results"]:
                fl = r.get("FastestLap") or {}
                if fl.get("rank") == "1":
                    fastest_driver = r.get("Driver", {}).get("familyName", "")
                    lap_time = (fl.get("Time") or {}).get("time", "")
                    text += f"\n⚡ 最快圈速: {fastest_driver} - {lap_time}\n"
                    md += f"\r⚡ 最快圈速：**{fastest_driver}** - {lap_time}\r"
                    break

        if lap_record_info:
            text += f"\n{lap_record_info}\n"
            md += f"\r{lap_record_info}\r"

        text += "\n━━━━━━━━━━━━━━━\n#F1 #Formula1"
        return self._send_card(md, text, at_all=False)
    
    def send_daily_schedule(self, sessions: List[Dict[str, Any]]) -> bool:
        """发送每日赛程汇总（md卡片 + 双时区时间）"""
        if not sessions:
            return False

        from pytz import timezone
        local_tz = timezone('Asia/Shanghai')

        race_name = sessions[0]['race_name']
        circuit = sessions[0]['circuit']

        text = f"📅 【今日F1赛程】{race_name}\n\n📍 {circuit}\n━━━━━━━━━━━━━━━\n"
        md = f"## 📅 今日F1赛程：{race_name}\r\r📍 {circuit}\r"

        icons = {
            "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
            "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"
        }

        for session in sessions:
            utc_time = session['datetime']
            local_time = utc_time.astimezone(local_tz)
            time_str = local_time.strftime("%H:%M")
            icon = icons.get(session['type'], "🏎️")
            text += f"\n{icon} {session['name']}: {time_str}"
            md += f"\r{icon} **{session['name']}**：{_dual_time_str(utc_time, circuit)}"

        text += "\n\n━━━━━━━━━━━━━━━\n记得准时观看！🏎️💨"
        md += "\r\r记得准时观看！🏎️💨"

        return self._send_card(md, text, at_all=True,
                               ping_text=f"📅 今日F1赛程：{race_name}，详见上方卡片")

    def send_weekly_preview(self, race_data: Dict[str, Any], sessions: List[Dict[str, Any]],
                            circuit_data: Dict[str, Any], llm_verified: bool = False) -> bool:
        """
        发送比赛周预告（每周一推送，wecom/pushplus版同款功能）

        Args:
            race_data: 比赛数据（get_schedule()中的单站）
            sessions: get_all_sessions() 返回的环节列表
            circuit_data: 赛道数据（circuits_data.json格式）
            llm_verified: 圈速纪录是否经LLM联网核查更新
        """
        from pytz import timezone
        local_tz = timezone('Asia/Shanghai')

        race_name = race_data.get('raceName', 'F1大奖赛')
        flag = circuit_data.get('flag', '🏁')
        circuit_name = circuit_data.get('name') or race_data.get('Circuit', {}).get('circuitName', '')

        lap_record = circuit_data.get('lap_record', {})
        record_line = ""
        if lap_record.get('time'):
            record_line = (f"• 圈速纪录：{lap_record.get('time')} "
                           f"({lap_record.get('driver', '')}，{lap_record.get('year', '')})")

        verified_tag = "\n🔎 圈速纪录已联网核实更新" if llm_verified else ""

        circuit_lines = []
        if circuit_data.get('first_race'):
            circuit_lines.append(f"• 首次办赛：{circuit_data['first_race']}年")
        if circuit_data.get('lap_length_km'):
            circuit_lines.append(f"• 单圈长度：{circuit_data['lap_length_km']}km")
        if circuit_data.get('laps'):
            circuit_lines.append(f"• 正赛圈数：{circuit_data['laps']}圈")
        if circuit_data.get('race_distance_km'):
            circuit_lines.append(f"• 正赛距离：{circuit_data['race_distance_km']}km")
        if record_line:
            circuit_lines.append(record_line)
        circuit_text = "\n".join(circuit_lines) if circuit_lines else "暂无赛道详细数据"

        icons = {"fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
                 "qualifying": "⏱️", "sprint": "⚡", "sprint_qualifying": "⏱️", "race": "🏁"}
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

        schedule_text = ""
        schedule_md = ""
        for s in sessions:
            local_time = s['datetime'].astimezone(local_tz)
            date_str = local_time.strftime("%m月%d日")
            time_str = local_time.strftime("%H:%M")
            wd = weekdays[local_time.weekday()]
            icon = icons.get(s['type'], "🏎️")
            schedule_text += f"\n{icon} {s['name']}: {date_str}({wd}) {time_str}"
            schedule_md += f"\r{icon} **{s['name']}**：{_dual_time_str(s['datetime'], circuit_name)}"

        text = f"""🎉 【欢迎来到比赛周！】

本周为 {flag} {race_name}
📍 {circuit_name}

📊 赛道信息
{circuit_text}{verified_tag}

⏰ 比赛时间（北京时间）{schedule_text}

━━━━━━━━━━━━━━━
🏎️💨 准备好享受速度与激情了吗？
#F1 #Formula1"""

        md = (f"## 🎉 欢迎来到比赛周！\r\r"
              f"本周为 {flag} **{race_name}**\r"
              f"📍 {circuit_name}\r\r"
              f"**📊 赛道信息**\r"
              + "\r".join(circuit_lines if circuit_lines else ["暂无赛道详细数据"])
              + (f"\r🔎 圈速纪录已联网核实更新" if llm_verified else "")
              + f"\r\r**⏰ 比赛时间**{schedule_md}\r\r"
              "🏎️💨 准备好享受速度与激情了吗？")

        return self._send_card(md, text, at_all=False)

    def send_startup_message(self) -> bool:
        """发送启动通知"""
        message = """✅ 【F1赛程提醒机器人已启动】

将为您自动推送：
🎉 每周一比赛周预告（含赛道信息）
🏎️ 练习赛提醒
⏱️ 排位赛提醒  
🏁 正赛提醒
📊 赛后结果
🎉 新圈速纪录即时播报

@我 可直接提问F1技术/规则/历史问题（联网核实）

━━━━━━━━━━━━━━━
Formula 1 🏎️💨"""
        
        return self.send_group_message(message, at_all=False)
    
    def send_test_message(self) -> bool:
        """发送测试消息"""
        message = f"""🧪 【测试消息】

如果您收到这条消息，说明QQ官方群聊机器人配置成功！

当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
目标群: {self._current_group}"""
        
        return self.send_group_message(message, at_all=False)
    
    # ==================== 图片消息（富媒体） ====================

    def send_image_url(self, image_url: str, content: str = " ") -> bool:
        """
        发送图片消息（QQ富媒体接口：提供公网URL由QQ下载转存，再发msg_type=7）

        Args:
            image_url: 图片公网URL
            content: 附带文字（QQ要求content非空）

        Returns:
            发送是否成功
        """
        import random

        try:
            if not self._get_access_token():
                logger.error("无法获取Access Token，发送图片失败")
                return False

            # 1. 提交富媒体URL（file_type=1 图片）
            upload_resp = requests.post(
                f"{self.base_url}/v2/groups/{self._current_group}/files",
                headers={
                    "Authorization": f"QQBot {self.access_token}",
                    "Content-Type": "application/json"
                },
                json={"file_type": 1, "url": image_url, "srv_send_msg": False},
                timeout=20,
                proxies={"http": None, "https": None}
            )

            if upload_resp.status_code != 200:
                logger.error(f"图片URL提交失败: {upload_resp.status_code} - {upload_resp.text[:300]}")
                return False

            file_info = upload_resp.json().get("file_info")
            if not file_info:
                logger.error(f"图片URL提交响应无file_info: {upload_resp.text[:300]}")
                return False

            # 2. 发送媒体消息
            send_resp = requests.post(
                f"{self.base_url}/v2/groups/{self._current_group}/messages",
                headers={
                    "Authorization": f"QQBot {self.access_token}",
                    "Content-Type": "application/json"
                },
                json={
                    "msg_type": 7,
                    "media": {"file_info": file_info},
                    "content": content or " ",
                    "msg_seq": random.randint(1, 1000000)
                },
                timeout=15,
                proxies={"http": None, "https": None}
            )

            if send_resp.status_code == 200:
                result = send_resp.json()
                if result.get("id") or result.get("code") == 0:
                    logger.info("✓ 图片消息发送成功")
                    return True

            logger.error(f"图片消息发送失败: {send_resp.status_code} - {send_resp.text[:300]}")
            return False
        except Exception as e:
            logger.error(f"发送图片异常: {e}")
            return False

    # ==================== Markdown 卡片消息 ====================

    def send_markdown_message(self, markdown_content: str, fallback_text: str = None) -> bool:
        """
        发送Markdown卡片消息（msg_type=2），失败时自动降级为纯文本

        Args:
            markdown_content: Markdown内容
            fallback_text: 降级纯文本（不提供则失败返回False）

        Returns:
            发送是否成功
        """
        try:
            if not self._get_access_token():
                logger.error("无法获取Access Token，发送失败")
                return False

            import random
            headers = {
                "Authorization": f"QQBot {self.access_token}",
                "Content-Type": "application/json"
            }
            message_data = {
                "msg_type": 2,
                "markdown": {"content": markdown_content},
                "msg_seq": random.randint(1, 1000000)
            }

            response = requests.post(
                f"{self.base_url}/v2/groups/{self._current_group}/messages",
                headers=headers,
                json=message_data,
                timeout=10,
                proxies={"http": None, "https": None}
            )

            if response.status_code == 200:
                result = response.json()
                if result.get("id") or result.get("code") == 0:
                    logger.info("✓ Markdown消息发送成功")
                    self._record_sent(result.get("id"), markdown_content)
                    return True

            logger.warning(f"Markdown消息发送失败: {response.status_code} - {response.text[:200]}")
            if fallback_text:
                logger.info("降级为纯文本发送")
                return self.send_group_message(fallback_text)
            return False
        except Exception as e:
            logger.error(f"发送Markdown消息异常: {e}")
            if fallback_text:
                return self.send_group_message(fallback_text)
            return False

    # ==================== 积分榜与个性化推送 ====================

    @staticmethod
    def _parse_driver_standings(standings: Dict[str, Any]) -> list:
        """解析Ergast格式车手积分榜"""
        try:
            lists = standings["MRData"]["StandingsTable"]["StandingsLists"]
            return lists[0].get("DriverStandings", []) if lists else []
        except (KeyError, IndexError, TypeError):
            return []

    @staticmethod
    def _parse_constructor_standings(standings: Dict[str, Any]) -> list:
        """解析Ergast格式车队积分榜"""
        try:
            lists = standings["MRData"]["StandingsTable"]["StandingsLists"]
            return lists[0].get("ConstructorStandings", []) if lists else []
        except (KeyError, IndexError, TypeError):
            return []

    def format_driver_standings(self, driver_entries: list, top_n: int = None) -> str:
        """格式化车手积分榜消息"""
        message = "🏆 【F1车手积分榜】\n\n"
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for e in (driver_entries[:top_n] if top_n else driver_entries):
            pos = int(e.get("position", 0))
            d = e.get("Driver", {})
            name = d.get("familyName", "Unknown")
            points = e.get("points", "0")
            wins = e.get("wins", "0")
            team = (e.get("Constructors") or [{}])[0].get("name", "")
            rank = medals.get(pos, f"{pos}.")
            win_str = f" ({wins}胜)" if wins != "0" else ""
            message += f"{rank} {name} - {points}分{win_str} [{team}]\n"
        message += "\n━━━━━━━━━━━━━━━\n#F1 #Formula1"
        return message

    def format_constructor_standings(self, team_entries: list, top_n: int = None) -> str:
        """格式化车队积分榜消息"""
        message = "🏎️ 【F1车队积分榜】\n\n"
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for e in (team_entries[:top_n] if top_n else team_entries):
            pos = int(e.get("position", 0))
            name = e.get("Constructor", {}).get("name", "Unknown")
            points = e.get("points", "0")
            wins = e.get("wins", "0")
            rank = medals.get(pos, f"{pos}.")
            win_str = f" ({wins}胜)" if wins != "0" else ""
            message += f"{rank} {name} - {points}分{win_str}\n"
        message += "\n━━━━━━━━━━━━━━━\n#F1 #Formula1"
        return message

    def send_driver_standings(self, standings: Dict[str, Any], top_n: int = None) -> bool:
        """发送车手积分榜（优先Markdown卡片）"""
        entries = self._parse_driver_standings(standings.get("drivers") or {})
        if not entries:
            logger.warning("无车手积分榜数据")
            return False
        return self.send_markdown_message(
            self.format_driver_standings_md(entries, top_n),
            fallback_text=self.format_driver_standings(entries, top_n)
        )

    def send_constructor_standings(self, standings: Dict[str, Any], top_n: int = None) -> bool:
        """发送车队积分榜（优先Markdown卡片）"""
        entries = self._parse_constructor_standings(standings.get("constructors") or {})
        if not entries:
            logger.warning("无车队积分榜数据")
            return False
        return self.send_markdown_message(
            self.format_constructor_standings_md(entries, top_n),
            fallback_text=self.format_constructor_standings(entries, top_n)
        )

    def format_driver_standings_md(self, driver_entries: list, top_n: int = None,
                                   title: str = "🏆 F1车手积分榜") -> str:
        """Markdown格式车手积分榜（车手头像 + 车队图标）"""
        from .openf1_api import get_avatar_md, get_team_icon_md
        lines = [f"## {title}\r\r"]
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for e in (driver_entries[:top_n] if top_n else driver_entries):
            pos = int(e.get("position", 0))
            d = e.get("Driver", {})
            name = d.get("familyName", "Unknown")
            points = e.get("points", "0")
            wins = e.get("wins", "0")
            team = (e.get("Constructors") or [{}])[0].get("name", "")
            rank = medals.get(pos, f"**{pos}.**")
            win_str = f" · {wins}胜" if wins != "0" else ""
            avatar = get_avatar_md(name)
            avatar = f"{avatar} " if avatar else ""
            ticon = get_team_icon_md(team)
            ticon = f"{ticon} " if ticon else ""
            lines.append(f"{rank} {avatar}**{name}** — {points}分{win_str}\r> {ticon}{team}")
        return "\r".join(lines)

    def format_constructor_standings_md(self, team_entries: list, top_n: int = None) -> str:
        """Markdown格式车队积分榜（车队图标）"""
        from .openf1_api import get_team_icon_md
        lines = ["## 🏎️ F1车队积分榜\r\r"]
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for e in (team_entries[:top_n] if top_n else team_entries):
            pos = int(e.get("position", 0))
            name = e.get("Constructor", {}).get("name", "Unknown")
            points = e.get("points", "0")
            wins = e.get("wins", "0")
            rank = medals.get(pos, f"**{pos}.**")
            win_str = f" · {wins}胜" if wins != "0" else ""
            icon = get_team_icon_md(name)
            icon = f"{icon} " if icon else ""
            lines.append(f"{rank} {icon}**{name}** — {points}分{win_str}")
        return "\r".join(lines)

    # ==================== 环节成绩 ====================

    @staticmethod
    def format_session_result(result: Dict[str, Any], watch_driver_ids: set = None, top_n: int = None) -> tuple:
        """
        格式化环节成绩为 (纯文本, Markdown)
        Args:
            result: get_session_results() 返回的数据
            watch_driver_ids: 群成员关注的车手driver_id集合（高亮标记）
        Returns:
            (text, markdown)
        """
        watch_driver_ids = watch_driver_ids or set()
        entries = result.get("entries", [])
        session_name = result.get("session_name", "")
        race_name = result.get("race_name", "F1大奖赛")

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        watched = [e for e in entries if e.get("driver_id") in watch_driver_ids]

        text = f"🏁 【{race_name} - {session_name}成绩】\n\n"
        md = f"## 🏁 {session_name}成绩\r> {race_name}\r\r"

        shown = entries[:top_n] if top_n else entries
        watched_below = [e for e in watched if top_n and e["position"] > top_n]

        from .openf1_api import get_avatar_md
        for e in shown:
            pos = e["position"]
            mark = " ⭐" if e.get("driver_id") in watch_driver_ids else ""
            time_str = f" - {e['time']}" if e.get("time") else ""
            pts = f" (+{e['points']}分)" if e.get("points") else ""
            rank = medals.get(pos, f"{pos}.")
            text += f"{rank} {e['driver_name']}{time_str}{pts}{mark}\n"
            rank_md = medals.get(pos, f"**{pos}.**")
            avatar = get_avatar_md(e["driver_name"])
            avatar = f"{avatar} " if avatar else ""
            md += f"{rank_md} {avatar}**{e['driver_name']}**{time_str}{pts}{mark}\r"

        for e in watched_below:
            time_str = f" - {e['time']}" if e.get("time") else ""
            pts = f" (+{e['points']}分)" if e.get("points") else ""
            text += f"⭐ {e['position']}. {e['driver_name']}{time_str}{pts}（你关注的车手）\n"
            md += f"\r⭐ **{e['position']}. {e['driver_name']}**{time_str}{pts}（你关注的车手）"

        text += "\n━━━━━━━━━━━━━━━\n#F1 #Formula1"
        return text, md

    def send_session_result(self, result: Dict[str, Any], watch_driver_ids: set = None) -> bool:
        """发送环节成绩（优先Markdown卡片）"""
        if not result or not result.get("entries"):
            return False
        text, md = self.format_session_result(result, watch_driver_ids)
        return self.send_markdown_message(md, fallback_text=text)

    @staticmethod
    def format_gp_summary(race: Dict[str, Any], year: int, session_results: List[tuple]) -> tuple:
        """
        格式化分站各环节成绩汇总为 (纯文本, Markdown)
        每个环节显示前三名

        Args:
            race: 分站数据
            year: 赛季年份
            session_results: [(session_name, top3_entries或None), ...]
        """
        race_name = race.get("raceName", "")
        circuit = race.get("Circuit", {}).get("circuitName", "")

        text = f"🏁 【{race_name}】({year})\n📍 {circuit}\n\n"
        md = f"## 🏁 {race_name}\r> 📍 {circuit} · {year}赛季\r\r"

        has_data = False
        for session_name, top3 in session_results:
            if not top3:
                continue
            has_data = True
            medals = ["🥇", "🥈", "🥉"]
            names = " ".join(f"{medals[i]}{e['driver_name']}" for i, e in enumerate(top3))
            text += f"{session_name}: {names}\n"
            md += f"**{session_name}**\r{names}\r"

        if not has_data:
            text += "暂无成绩数据\n"
            md += "\r暂无成绩数据"

        text += "\n查看完整成绩: /gp 地点 [年份] 环节\n如: /gp 蒙扎 race"
        md += "\r\r查看完整成绩: `/gp 地点 [年份] 环节`\r如 `/gp 蒙扎 race` · `/gp 斯帕 2022 qualy`"
        return text, md

    # ==================== 赛历 ====================

    @staticmethod
    def format_calendar(schedule: List[Dict[str, Any]]) -> tuple:
        """
        格式化全年赛历为 (纯文本, Markdown)
        Args:
            schedule: get_schedule() 返回的比赛列表
        """
        from pytz import timezone as _tz
        from datetime import datetime as _dt
        local_tz = _tz('Asia/Shanghai')
        now = _dt.now(local_tz)

        text = "📅 【F1全年赛历】\n\n"
        md = "## 📅 F1全年赛历\r\r"

        for race in schedule:
            round_num = race.get("round", "")
            name = race.get("raceName", "").replace("Formula 1 ", "")
            circuit = race.get("Circuit", {}).get("circuitName", "")
            try:
                race_dt = _dt.fromisoformat(f"{race['date']}T{race.get('time', '00:00:00Z').replace('Z', '')}")
                from datetime import timezone as _tzu
                race_dt = race_dt.replace(tzinfo=_tzu.utc).astimezone(local_tz)
                date_str = race_dt.strftime("%m月%d日")
                passed = race_dt < now
            except Exception:
                date_str = race.get("date", "")
                passed = False

            sprint = " ⚡" if "Sprint" in race else ""
            status = "✅" if passed else "🔜"
            text += f"{status} R{round_num} {name}{sprint} - {date_str}\n"
            md += f"{status} **R{round_num}** {name}{sprint}\r> {circuit} · {date_str}\r"

        text += "\n⚡=冲刺赛周末 ✅=已结束 🔜=未开始"
        md += "\r\r⚡冲刺赛周末 · ✅已结束 · 🔜未开始"
        return text, md

    @staticmethod
    def format_next_race(race: Dict[str, Any], sessions: List[Dict[str, Any]]) -> tuple:
        """
        格式化下一站详情为 (纯文本, Markdown)
        Args:
            race: 下一站比赛数据
            sessions: get_all_sessions() 返回的环节列表
        """
        from pytz import timezone as _tz
        local_tz = _tz('Asia/Shanghai')

        name = race.get("raceName", "")
        circuit = race.get("Circuit", {}).get("circuitName", "")
        location = race.get("Circuit", {}).get("Location", {})
        locality = location.get("locality", "")
        country = location.get("country", "")

        text = f"🏁 【下一站】{name}\n📍 {circuit} ({locality}, {country})\n\n"
        md = f"## 🏁 下一站：{name}\r> 📍 {circuit} · {locality}, {country}\r\r"

        icons = {"fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
                 "qualifying": "⏱️", "sprint": "⚡", "sprint_qualifying": "⏱️", "race": "🏁"}
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

        for s in sessions:
            local_time = s['datetime'].astimezone(local_tz)
            date_str = local_time.strftime("%m月%d日")
            time_str = local_time.strftime("%H:%M")
            wd = weekdays[local_time.weekday()]
            icon = icons.get(s['type'], "🏎️")
            text += f"{icon} {s['name']}: {date_str}({wd}) {time_str}\n"
            md += f"{icon} **{s['name']}** — {date_str} {wd} {time_str}\r"

        text += "\n以上均为北京时间 🏎️💨"
        md += "\r\r以上均为北京时间 🏎️💨"
        return text, md

    @staticmethod
    def _weather_line(s: Dict[str, Any], label: str = "") -> str:
        """单个时段天气摘要 -> 单行文本（气温/沥青/降水/风速风向）"""
        parts = []
        if s.get("weather_desc"):
            parts.append(s["weather_desc"])
        if s.get("air_temp"):
            parts.append(f"气温{s['air_temp']['min']}-{s['air_temp']['max']}°C")
        # 赛道温度优先级：实测沥青 > 模型估算 > 土壤代理
        if s.get("track_temp_measured"):
            tt = s["track_temp_measured"]
            parts.append(f"沥青实测{tt['min']}-{tt['max']}°C")
        elif s.get("track_temp_estimated"):
            tt = s["track_temp_estimated"]
            parts.append(f"沥青约{tt['min']}-{tt['max']}°C")
        elif s.get("track_temp_proxy"):
            tt = s["track_temp_proxy"]
            parts.append(f"地面约{tt['min']}-{tt['max']}°C")
        if s.get("precip_probability_max") is not None:
            rain = f"降水{s['precip_probability_max']}%"
            if s.get("rain_eta_minutes") is not None and s["rain_eta_minutes"] >= 0:
                rain += f"（开始后约{s['rain_eta_minutes']}分钟）"
            parts.append(rain)
        if s.get("wind_max_kmh") is not None:
            wd = s.get("wind_direction", "")
            parts.append(f"风 {wd} {s.get('wind_min_kmh', '?')}-{s['wind_max_kmh']}km/h".replace("  ", " "))
        return (f"{label}: " if label else "") + "，".join(parts)

    @staticmethod
    def format_race_weather(result: Dict[str, Any]) -> tuple:
        """分站天气分析包 -> (纯文本, Markdown)。各环节双时区时间 + 天气/沥青/风速风向"""
        from datetime import datetime as _dt
        from pytz import timezone as _tz
        local_tz = _tz('Asia/Shanghai')
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

        race = result.get("race", "")
        circuit = result.get("circuit", "")
        kind = result.get("data_kind", "")

        text = f"🌤️ 【{race} 天气】\n📍 {circuit} · {result.get('location', '')}\n（{kind}）\n\n"
        md = f"## 🌤️ {race} 天气\r> 📍 {circuit} · {result.get('location', '')} · {kind}\r\r"

        # 当前时刻（比赛周末期间）
        if result.get("current"):
            line = QQGroupBot._weather_line(result["current"], "🌡️ 当前")
            text += line + "\n\n"
            md += f"**{line}**\r\r"

        for s in result.get("sessions", []):
            when = ""
            if s.get("datetime"):
                try:
                    dt = _dt.fromisoformat(s["datetime"])
                    if dt.tzinfo is None:
                        dt = local_tz.localize(dt)
                    lt = dt.astimezone(local_tz)
                    when = f"（{lt.strftime('%m月%d日')}{weekdays[lt.weekday()]} {lt.strftime('%H:%M')}）"
                except Exception:
                    pass
            label = f"{s.get('session', '时段')}{when}"
            line = QQGroupBot._weather_line(s, label)
            text += line + "\n"
            md += line + "\r"

        note = "沥青温度为模型估算/代理值时以实际为准"
        text += f"\n{note}"
        md += f"\r> {note}"
        return text, md

    def send_standings_update(self, standings: Dict[str, Any], race_name: str = "") -> bool:
        """赛后积分榜更新（调度器接口，md卡片优先，全量车手+头像+车队图标）"""
        entries = self._parse_driver_standings(standings.get("drivers") or {})
        if not entries:
            return False
        title = f"📊 {race_name} 后车手积分榜" if race_name else "📊 车手积分榜更新"
        md = self.format_driver_standings_md(entries, title=title)
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        text = f"【{title}】\n\n"
        for e in entries:
            pos = int(e.get("position", 0))
            name = e.get("Driver", {}).get("familyName", "Unknown")
            points = e.get("points", "0")
            rank = medals.get(pos, f"{pos}.")
            text += f"{rank} {name} - {points}分\n"
        text += "\n━━━━━━━━━━━━━━━\n#F1 #Formula1"
        return self._send_card(md, text, at_all=False)

    def send_personalized_standings(self, standings: Dict[str, Any], group_prefs: Dict[str, Dict[str, Any]]) -> bool:
        """
        按群成员偏好聚合发送个性化排名（一条消息@多人，节省主动消息额度）
        
        Args:
            standings: get_current_standings() 返回的数据
            group_prefs: {member_openid: {"driver": id, "driver_name": str, "team": id, "team_name": str}}
        """
        if not group_prefs:
            return False

        driver_entries = self._parse_driver_standings(standings.get("drivers") or {})
        team_entries = self._parse_constructor_standings(standings.get("constructors") or {})

        driver_map = {e.get("Driver", {}).get("driverId"): e for e in driver_entries}
        team_map = {e.get("Constructor", {}).get("constructorId"): e for e in team_entries}

        sections = []
        mentions = []
        for member_openid, pref in group_prefs.items():
            lines = []
            driver_id = pref.get("driver")
            team_id = pref.get("team")

            if driver_id and driver_id in driver_map:
                e = driver_map[driver_id]
                d = e.get("Driver", {})
                name = pref.get("driver_name") or f"{d.get('givenName', '')} {d.get('familyName', '')}".strip()
                lines.append(f"🏎️ 你的车手 {name}: P{e.get('position')} - {e.get('points')}分")
            elif driver_id:
                lines.append(f"🏎️ 你的车手 {pref.get('driver_name', driver_id)}: 本赛季暂无积分数据")

            if team_id and team_id in team_map:
                e = team_map[team_id]
                name = pref.get("team_name") or e.get("Constructor", {}).get("name", team_id)
                lines.append(f"🏁 你的主队 {name}: P{e.get('position')} - {e.get('points')}分")
            elif team_id:
                lines.append(f"🏁 你的主队 {pref.get('team_name', team_id)}: 本赛季暂无积分数据")

            if lines:
                sections.append("\n".join(lines))
                mentions.append(member_openid)

        if not sections:
            return False

        message = "⭐ 【个性化积分播报】\n\n" + "\n\n".join(sections)
        message += "\n\n━━━━━━━━━━━━━━━\n#F1 #Formula1"
        return self.send_group_message(message, mentions=mentions)

    def send_weekly_schedule_preview(self, sessions: List[Dict[str, Any]]) -> bool:
        """
        发送本周赛程预览
        
        Args:
            sessions: 本周所有比赛环节
            
        Returns:
            发送是否成功
        """
        if not sessions:
            return False
        
        from pytz import timezone
        local_tz = timezone('Asia/Shanghai')
        
        race_name = sessions[0]['race_name']
        circuit = sessions[0]['circuit']
        
        text = f"""📅 【本周F1赛程预告】

🏆 {race_name}
📍 {circuit}

本周比赛安排：
"""
        md = f"## 📅 本周F1赛程预告\r\r🏆 **{race_name}**\r📍 {circuit}\r"

        icons = {
            "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
            "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"
        }

        for session in sessions:
            utc_time = session['datetime']
            local_time = utc_time.astimezone(local_tz)
            date_str = local_time.strftime("%m月%d日")
            time_str = local_time.strftime("%H:%M")
            weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][local_time.weekday()]
            icon = icons.get(session['type'], "🏎️")

            text += f"\n{icon} {session['name']}: {date_str}({weekday}) {time_str}"
            md += f"\r{icon} **{session['name']}**：{_dual_time_str(utc_time, circuit)}"

        text += "\n\n记得准时观看！🏎️💨"
        md += "\r\r记得准时观看！🏎️💨"

        return self._send_card(md, text, at_all=True,
                               ping_text=f"📅 本周F1赛程：{race_name}，详见上方卡片")

    # ==================== 私聊消息（C2C） ====================

    def send_dm_message(self, user_openid: str, content: str) -> bool:
        """
        发送私聊文本消息（主动单聊，需用户未关闭"允许主动发送"）

        Args:
            user_openid: 用户openid（私聊事件中获取）
            content: 消息内容
        """
        import random
        try:
            if not self._get_access_token():
                return False
            resp = requests.post(
                f"{self.base_url}/v2/users/{user_openid}/messages",
                headers={
                    "Authorization": f"QQBot {self.access_token}",
                    "Content-Type": "application/json"
                },
                json={"msg_type": 0, "content": content, "msg_seq": random.randint(1, 1000000)},
                timeout=10,
                proxies={"http": None, "https": None}
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("id") or result.get("code") == 0:
                    logger.info(f"✓ 私聊消息发送成功 (用户: {user_openid[:8]}...)")
                    return True
            logger.error(f"私聊消息发送失败: {resp.status_code} - {resp.text[:200]}")
            return False
        except Exception as e:
            logger.error(f"私聊消息发送异常: {e}")
            return False

    def send_dm_markdown(self, user_openid: str, markdown_content: str, fallback_text: str = None) -> bool:
        """发送私聊Markdown卡片，失败降级纯文本"""
        import random
        try:
            if not self._get_access_token():
                return False
            resp = requests.post(
                f"{self.base_url}/v2/users/{user_openid}/messages",
                headers={
                    "Authorization": f"QQBot {self.access_token}",
                    "Content-Type": "application/json"
                },
                json={
                    "msg_type": 2,
                    "markdown": {"content": markdown_content},
                    "msg_seq": random.randint(1, 1000000)
                },
                timeout=10,
                proxies={"http": None, "https": None}
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("id") or result.get("code") == 0:
                    logger.info(f"✓ 私聊Markdown发送成功 (用户: {user_openid[:8]}...)")
                    return True
            logger.warning(f"私聊Markdown发送失败: {resp.status_code} - {resp.text[:200]}")
            if fallback_text:
                return self.send_dm_message(user_openid, fallback_text)
            return False
        except Exception as e:
            logger.error(f"私聊Markdown发送异常: {e}")
            if fallback_text:
                return self.send_dm_message(user_openid, fallback_text)
            return False

    # ==================== 多群广播包装 ====================
    # 公开发送方法在多群配置下自动遍历所有群；广播上下文内嵌套调用保持单群

    send_group_message = _make_broadcast(send_group_message)
    send_markdown_message = _make_broadcast(send_markdown_message)
    send_image_url = _make_broadcast(send_image_url)
    send_session_reminder = _make_broadcast(send_session_reminder)
    send_race_result = _make_broadcast(send_race_result)
    send_daily_schedule = _make_broadcast(send_daily_schedule)
    send_startup_message = _make_broadcast(send_startup_message)
    send_test_message = _make_broadcast(send_test_message)
    send_driver_standings = _make_broadcast(send_driver_standings)
    send_constructor_standings = _make_broadcast(send_constructor_standings)
    send_standings_update = _make_broadcast(send_standings_update)
    send_personalized_standings = _make_broadcast(send_personalized_standings)
    send_session_result = _make_broadcast(send_session_result)
    send_weekly_schedule_preview = _make_broadcast(send_weekly_schedule_preview)
    send_weekly_preview = _make_broadcast(send_weekly_preview)  # 周一比赛周预告（此前遗漏，多群只发主群）


# ==================== 兼容性别名 ====================

class QQBot(QQGroupBot):
    """QQGroupBot的别名，用于兼容性"""
    pass
