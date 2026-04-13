"""
F1赛程提醒机器人 - QQ机器人核心模块
基于 go-cqhttp 实现
文档: https://docs.go-cqhttp.org/
"""

import requests
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)


class QQBot:
    """
    QQ 机器人封装类
    通过 go-cqhttp 的 HTTP API 发送消息
    """
    
    def __init__(self, base_url: str, group_id: int):
        """
        初始化 QQ 机器人
        
        Args:
            base_url: go-cqhttp HTTP API 地址，如 http://localhost:5700
            group_id: 目标 QQ 群号
        """
        self.base_url = base_url.rstrip('/')
        self.group_id = group_id
        self.session = requests.Session()
        
        # 测试连接
        try:
            self._test_connection()
            logger.info(f"✓ QQ 机器人初始化成功 (群号: {group_id})")
        except Exception as e:
            logger.warning(f"⚠️ 连接 go-cqhttp 测试失败: {e}")
            logger.warning("请确保 go-cqhttp 已启动并配置正确")
    
    def _test_connection(self):
        """测试与 go-cqhttp 的连接"""
        url = f"{self.base_url}/get_version_info"
        response = self.session.get(url, timeout=5)
        data = response.json()
        if data.get("retcode") == 0:
            version = data.get("data", {}).get("app_version", "unknown")
            logger.info(f"go-cqhttp 版本: {version}")
        else:
            raise Exception("无法获取 go-cqhttp 版本信息")
    
    def send_group_message(self, message: str, auto_escape: bool = False) -> bool:
        """
        发送群消息
        
        Args:
            message: 消息内容（支持CQ码）
            auto_escape: 是否转义消息内容
            
        Returns:
            发送是否成功
        """
        url = f"{self.base_url}/send_group_msg"
        data = {
            "group_id": self.group_id,
            "message": message,
            "auto_escape": auto_escape
        }
        
        try:
            response = self.session.post(url, json=data, timeout=10)
            result = response.json()
            
            if result.get("status") == "ok" and result.get("retcode") == 0:
                logger.info(f"✓ QQ群消息发送成功 (群号: {self.group_id})")
                return True
            else:
                logger.error(f"✗ QQ群消息发送失败: {result}")
                return False
                
        except requests.exceptions.ConnectionError:
            logger.error("✗ 连接 go-cqhttp 失败，请检查服务是否启动")
            return False
        except Exception as e:
            logger.error(f"✗ 发送QQ消息异常: {e}")
            return False
    
    def send_group_forward_message(self, messages: List[Dict[str, Any]]) -> bool:
        """
        发送合并转发消息（长消息推荐）
        
        Args:
            messages: 消息节点列表
            
        Returns:
            发送是否成功
        """
        url = f"{self.base_url}/send_group_forward_msg"
        data = {
            "group_id": self.group_id,
            "messages": messages
        }
        
        try:
            response = self.session.post(url, json=data, timeout=10)
            result = response.json()
            
            if result.get("status") == "ok" and result.get("retcode") == 0:
                logger.info(f"✓ QQ群合并转发消息发送成功")
                return True
            else:
                logger.error(f"✗ QQ群合并转发消息发送失败: {result}")
                return False
                
        except Exception as e:
            logger.error(f"✗ 发送合并转发消息异常: {e}")
            return False
    
    def send_session_reminder(self, session: Dict[str, Any]) -> bool:
        """
        发送比赛环节提醒消息
        
        Args:
            session: 比赛环节信息
            
        Returns:
            发送是否成功
        """
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
        
        # 构建消息（使用CQ码加粗）
        message = f"""{icon} 【F1提醒】 {icon}

[CQ:at,qq=all]
━━━━━━━━━━━━━━━
🏆 {session['race_name']}
📍 {session['circuit']}

【{session['name']}】即将开始

⏰ {date_str} {weekday} {time_str} (北京时间)

敬请期待精彩比赛！🏎️💨
━━━━━━━━━━━━━━━
#F1 #Formula1"""
        
        return self.send_group_message(message)
    
    def send_race_result(self, race_data: Dict[str, Any], results: Dict[str, Any]) -> bool:
        """
        发送比赛结果
        
        Args:
            race_data: 比赛信息
            results: 比赛结果数据
            
        Returns:
            发送是否成功
        """
        race_name = race_data.get('raceName', 'F1大奖赛')
        
        message = f"🏁 【{race_name} 比赛结果】\n\n"
        message += "━━━━━━━━━━━━━━━\n"
        
        if "Results" in results:
            message += "🥇 前十名:\n\n"
            for i, result in enumerate(results["Results"][:10], 1):
                driver = result.get("Driver", {})
                family_name = driver.get("familyName", "Unknown")
                team = result.get("Constructor", {}).get("name", "Unknown")
                
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                message += f"{medal} {family_name} ({team})\n"
            
            # 最快圈速
            fastest_lap = results["Results"][0].get("FastestLap", {})
            if fastest_lap and fastest_lap.get("rank") == "1":
                fastest_driver = results["Results"][0].get("Driver", {}).get("familyName", "")
                lap_time = fastest_lap.get("Time", {}).get("time", "")
                message += f"\n⚡ 最快圈速: {fastest_driver} - {lap_time}\n"
        
        message += "━━━━━━━━━━━━━━━\n"
        message += "#F1 #Formula1"
        
        return self.send_group_message(message)
    
    def send_daily_schedule(self, sessions: List[Dict[str, Any]]) -> bool:
        """
        发送每日赛程汇总
        
        Args:
            sessions: 今日比赛环节列表
            
        Returns:
            发送是否成功
        """
        if not sessions:
            return False
        
        from pytz import timezone
        local_tz = timezone('Asia/Shanghai')
        
        race_name = sessions[0]['race_name']
        circuit = sessions[0]['circuit']
        
        message = f"📅 【今日F1赛程】{race_name}\n\n"
        message += f"📍 {circuit}\n"
        message += "━━━━━━━━━━━━━━━\n\n"
        
        icons = {
            "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
            "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"
        }
        
        for session in sessions:
            utc_time = session['datetime']
            local_time = utc_time.astimezone(local_tz)
            time_str = local_time.strftime("%H:%M")
            icon = icons.get(session['type'], "🏎️")
            message += f"{icon} {session['name']}: {time_str}\n"
        
        message += "\n━━━━━━━━━━━━━━━\n"
        message += "记得准时观看！🏎️💨"
        
        return self.send_group_message(message)
    
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
        
        return self.send_group_message(message)
    
    def send_test_message(self) -> bool:
        """发送测试消息"""
        message = """🧪 【测试消息】

如果您收到这条消息，说明QQ机器人配置成功！

当前时间: {}""".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
        return self.send_group_message(message)
    
    def get_group_info(self) -> Optional[Dict[str, Any]]:
        """
        获取群信息
        
        Returns:
            群信息字典
        """
        url = f"{self.base_url}/get_group_info"
        params = {"group_id": self.group_id}
        
        try:
            response = self.session.get(url, params=params, timeout=10)
            result = response.json()
            
            if result.get("status") == "ok" and result.get("retcode") == 0:
                return result.get("data")
            else:
                logger.error(f"获取群信息失败: {result}")
                return None
                
        except Exception as e:
            logger.error(f"获取群信息异常: {e}")
            return None


# 兼容旧版本
class GoCQHttpBot(QQBot):
    """go-cqhttp 机器人的别名"""
    pass


# 测试代码
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print("=" * 60)
    print("QQ 机器人模块测试")
    print("=" * 60)
    
    # 测试配置（请替换为实际值）
    BASE_URL = "http://localhost:5700"
    GROUP_ID = 123456789
    
    print(f"\n连接地址: {BASE_URL}")
    print(f"目标群号: {GROUP_ID}")
    
    try:
        bot = QQBot(BASE_URL, GROUP_ID)
        
        print("\n发送测试消息...")
        if bot.send_test_message():
            print("✓ 测试消息发送成功")
        else:
            print("✗ 测试消息发送失败")
            
    except Exception as e:
        print(f"✗ 测试失败: {e}")
        print("\n请确保:")
        print("1. go-cqhttp 已启动")
        print("2. 配置文件中 HTTP 监听端口为 5700")
        print("3. QQ 已登录")
    
    print("\n" + "=" * 60)
