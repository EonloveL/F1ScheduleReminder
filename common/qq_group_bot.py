"""
F1赛程提醒机器人 - QQ官方群聊版本
基于QQ官方Bot API V2实现
支持：群聊消息、@全体成员、定时提醒

API文档：https://bot.q.qq.com/wiki/develop/api/
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import requests
import time

logger = logging.getLogger(__name__)


class QQGroupBot:
    """
    QQ官方群聊机器人封装类
    支持：群消息发送、@全体成员
    """
    
    # API基础URL
    API_BASE_URL = "https://api.sgroup.qq.com"
    SANDBOX_API_BASE_URL = "https://sandbox.api.sgroup.qq.com"
    TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
    
    def __init__(self, appid: str, secret: str, group_id: str, use_sandbox: bool = False):
        """
        初始化QQ官方群聊机器人
        
        Args:
            appid: QQ开放平台AppID
            secret: QQ开放平台AppSecret
            group_id: QQ群号（字符串格式）
            use_sandbox: 是否使用沙箱环境（新机器人默认只能在沙箱环境测试）
        """
        self.appid = appid
        self.secret = secret
        self.group_id = group_id
        self.use_sandbox = use_sandbox
        self.access_token = None
        self.token_expires_at = 0
        
        # 设置API基础URL
        self.base_url = self.SANDBOX_API_BASE_URL if use_sandbox else self.API_BASE_URL
        
        env_name = "沙箱环境" if use_sandbox else "正式环境"
        logger.info(f"✓ QQ官方群聊机器人初始化成功 [{env_name}]")
        logger.info(f"  - 目标群号: {self.group_id}")
        
        # 立即获取一次token测试
        if self._get_access_token():
            logger.info("✓ Access Token获取成功")
        else:
            logger.warning("⚠️ Access Token获取失败，将在发送消息时重试")
    
    def _get_access_token(self) -> bool:
        """获取QQ Bot Access Token"""
        try:
            # 检查token是否还有效（提前60秒过期）
            if self.access_token and time.time() < self.token_expires_at - 60:
                return True
            
            logger.debug("正在获取Access Token...")
            
            response = requests.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={
                    "appId": self.appid,
                    "clientSecret": self.secret
                },
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            
            logger.debug(f"Token响应状态: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                logger.debug(f"Token响应: {data}")
                
                if "access_token" in data:
                    self.access_token = data["access_token"]
                    expires_in = int(data.get("expires_in", 7200))
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
    
    def send_group_message(self, content: str, at_all: bool = False) -> bool:
        """
        发送消息到QQ群
        
        Args:
            content: 消息内容
            at_all: 是否@全体成员（需要机器人是群主或管理员）
            
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
            if at_all:
                message_content = f"@全体成员\n{content}"
            
            # 构建请求
            headers = {
                "Authorization": f"QQBot {self.access_token}",
                "Content-Type": "application/json"
            }
            
            # 构建消息体 - 简化版本
            # 注意：QQ群消息不支持 mentions 字段，只能通过文本@全体成员
            message_data = {
                "content": message_content,
                "msg_type": 0  # 文本消息
            }
            
            logger.debug(f"发送消息到群 {self.group_id}")
            logger.debug(f"请求体: {message_data}")
            
            # 发送群消息
            response = requests.post(
                f"{self.base_url}/v2/groups/{self.group_id}/messages",
                headers=headers,
                json=message_data,
                timeout=10
            )
            
            logger.debug(f"发送消息响应: {response.status_code} - {response.text}")
            
            if response.status_code == 200:
                result = response.json()
                if result.get("code") == 0:
                    logger.info(f"✓ 群消息发送成功 (群号: {self.group_id})")
                    return True
                else:
                    logger.error(f"发送消息API错误: {result}")
                    return False
            elif response.status_code == 401:
                logger.error("认证失败，请检查AppID和AppSecret")
                # 清除token以便下次重试
                self.access_token = None
                return False
            elif response.status_code == 403:
                logger.error("权限不足，请检查机器人是否已在群内，或是否有发送消息权限")
                return False
            else:
                logger.error(f"发送消息HTTP错误: {response.status_code} - {response.text}")
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
    
    # ==================== 业务方法（与企业微信版本保持一致接口） ====================
    
    def send_session_reminder(self, session: Dict[str, Any]) -> bool:
        """发送比赛环节提醒消息"""
        from pytz import timezone
        
        utc_time = session['datetime']
        local_tz = timezone('Asia/Shanghai')
        local_time = utc_time.astimezone(local_tz)
        
        date_str = local_time.strftime("%Y年%m月%d日")
        time_str = local_time.strftime("%H:%M")
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][local_time.weekday()]
        
        icons = {
            "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
            "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"
        }
        
        icon = icons.get(session['type'], "🏎️")
        
        message = f"""{icon} 【F1提醒】{icon}

🏆 {session['race_name']}
📍 {session['circuit']}

【{session['name']}】即将开始

⏰ {date_str} {weekday} {time_str} (北京时间)

敬请期待精彩比赛！🏎️💨

━━━━━━━━━━━━━━━
#F1 #Formula1"""
        
        # 发送消息并@全体成员
        return self.send_group_message(message, at_all=True)
    
    def send_race_result(self, race_data: Dict[str, Any], results: Dict[str, Any]) -> bool:
        """发送比赛结果"""
        race_name = race_data.get('raceName', 'F1大奖赛')
        
        message = f"""🏁 【{race_name} 比赛结果】

━━━━━━━━━━━━━━━
"""
        
        if "Results" in results:
            message += "🥇 前十名:\n\n"
            for i, result in enumerate(results["Results"][:10], 1):
                driver = result.get("Driver", {})
                family_name = driver.get("familyName", "Unknown")
                team = result.get("Constructor", {}).get("name", "Unknown")
                
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                message += f"{medal} {family_name} ({team})\n"
            
            fastest_lap = results["Results"][0].get("FastestLap", {})
            if fastest_lap and fastest_lap.get("rank") == "1":
                fastest_driver = results["Results"][0].get("Driver", {}).get("familyName", "")
                lap_time = fastest_lap.get("Time", {}).get("time", "")
                message += f"\n⚡ 最快圈速: {fastest_driver} - {lap_time}\n"
        
        message += """
━━━━━━━━━━━━━━━
#F1 #Formula1"""
        
        return self.send_group_message(message, at_all=False)
    
    def send_daily_schedule(self, sessions: List[Dict[str, Any]]) -> bool:
        """发送每日赛程汇总"""
        if not sessions:
            return False
        
        from pytz import timezone
        local_tz = timezone('Asia/Shanghai')
        
        race_name = sessions[0]['race_name']
        circuit = sessions[0]['circuit']
        
        message = f"""📅 【今日F1赛程】{race_name}

📍 {circuit}
━━━━━━━━━━━━━━━
"""
        
        icons = {
            "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
            "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"
        }
        
        for session in sessions:
            utc_time = session['datetime']
            local_time = utc_time.astimezone(local_tz)
            time_str = local_time.strftime("%H:%M")
            icon = icons.get(session['type'], "🏎️")
            message += f"\n{icon} {session['name']}: {time_str}"
        
        message += """

━━━━━━━━━━━━━━━
记得准时观看！🏎️💨"""
        
        return self.send_group_message(message, at_all=True)
    
    def send_startup_message(self) -> bool:
        """发送启动通知"""
        message = """✅ 【F1赛程提醒机器人已启动】

将为您自动推送：
🏎️ 练习赛提醒
⏱️ 排位赛提醒  
🏁 正赛提醒
📊 赛后结果

━━━━━━━━━━━━━━━
Formula 1 🏎️💨"""
        
        return self.send_group_message(message, at_all=False)
    
    def send_test_message(self) -> bool:
        """发送测试消息"""
        message = f"""🧪 【测试消息】

如果您收到这条消息，说明QQ官方群聊机器人配置成功！

当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
目标群号: {self.group_id}"""
        
        return self.send_group_message(message, at_all=False)
    
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
        
        message = f"""📅 【本周F1赛程预告】

🏆 {race_name}
📍 {circuit}

本周比赛安排：
"""
        
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
            
            message += f"\n{icon} {session['name']}: {date_str}({weekday}) {time_str}"
        
        message += """

记得准时观看！🏎️💨"""
        
        return self.send_group_message(message, at_all=True)


# ==================== 兼容性别名 ====================

class QQBot(QQGroupBot):
    """QQGroupBot的别名，用于兼容性"""
    pass
