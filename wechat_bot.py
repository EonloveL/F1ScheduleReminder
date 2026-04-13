"""
F1赛程提醒机器人 - 微信消息模块
使用 itchat 库实现微信消息发送
"""

import itchat
from itchat.content import TEXT
import time
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class WeChatBot:
    """微信机器人封装类"""
    
    def __init__(self, group_name: str):
        """
        初始化微信机器人
        
        Args:
            group_name: 要发送消息的微信群名称
        """
        self.group_name = group_name
        self.group_id = None
        self.is_logged_in = False
        
    def login(self, use_qr_code_image: bool = True) -> bool:
        """
        登录微信
        
        Args:
            use_qr_code_image: 是否使用图片二维码，False则在命令行显示
            
        Returns:
            是否登录成功
        """
        try:
            if use_qr_code_image:
                # 使用图片二维码
                itchat.auto_login(
                    hotReload=True,
                    enableCmdQR=False,
                    picDir='qr_code.png'
                )
            else:
                # 在命令行显示二维码
                itchat.auto_login(
                    hotReload=True,
                    enableCmdQR=2  # 2表示在命令行显示二维码
                )
            
            self.is_logged_in = True
            logger.info("微信登录成功")
            
            # 查找目标群聊
            self._find_group()
            
            return True
            
        except Exception as e:
            logger.error(f"微信登录失败: {e}")
            return False
    
    def _find_group(self) -> bool:
        """
        查找目标微信群
        
        Returns:
            是否找到群聊
        """
        # 获取所有群聊
        chatrooms = itchat.get_chatrooms(update=True)
        
        for room in chatrooms:
            if room['NickName'] == self.group_name:
                self.group_id = room['UserName']
                logger.info(f"找到群聊: {self.group_name}")
                return True
        
        logger.warning(f"未找到群聊: {self.group_name}")
        logger.info("可用的群聊列表:")
        for room in chatrooms:
            logger.info(f"  - {room['NickName']}")
        
        return False
    
    def send_message(self, message: str) -> bool:
        """
        发送消息到微信群
        
        Args:
            message: 要发送的消息内容
            
        Returns:
            是否发送成功
        """
        if not self.is_logged_in:
            logger.error("未登录微信，无法发送消息")
            return False
        
        # 如果群聊ID未找到，尝试重新查找
        if not self.group_id:
            if not self._find_group():
                logger.error(f"无法找到群聊: {self.group_name}")
                return False
        
        try:
            itchat.send(message, toUserName=self.group_id)
            logger.info(f"消息发送成功: {message[:50]}...")
            return True
            
        except Exception as e:
            logger.error(f"消息发送失败: {e}")
            # 尝试重新查找群聊
            self._find_group()
            try:
                itchat.send(message, toUserName=self.group_id)
                return True
            except Exception as e2:
                logger.error(f"重试发送失败: {e2}")
                return False
    
    def send_session_reminder(self, session: Dict[str, Any]) -> bool:
        """
        发送比赛环节提醒消息
        
        Args:
            session: 比赛环节信息
            
        Returns:
            是否发送成功
        """
        from pytz import timezone
        
        # 时区转换
        utc_time = session['datetime']
        local_tz = timezone('Asia/Shanghai')
        local_time = utc_time.astimezone(local_tz)
        
        # 格式化时间
        time_str = local_time.strftime("%m月%d日 %H:%M")
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][local_time.weekday()]
        
        # 构建消息
        emojis = {
            "fp1": "🏎️",
            "fp2": "🏎️",
            "fp3": "🏎️",
            "qualifying": "⏱️",
            "sprint": "⚡",
            "race": "🏁"
        }
        
        emoji = emojis.get(session['type'], "🏎️")
        
        message = f"""
{emoji} F1提醒 {emoji}

📍 {session['race_name']}
🏟️ {session['circuit']}

🔥 {session['name']} 即将开始
⏰ {weekday} {time_str} (北京时间)

敬请期待！
        """.strip()
        
        return self.send_message(message)
    
    def send_race_result(self, race_data: Dict[str, Any], results: Dict[str, Any]) -> bool:
        """
        发送比赛结果
        
        Args:
            race_data: 比赛信息
            results: 比赛结果数据
            
        Returns:
            是否发送成功
        """
        race_name = race_data.get('raceName', 'F1大奖赛')
        
        message = f"🏁 {race_name} 比赛结果\n\n"
        
        if "Results" in results:
            message += "🥇 前十名:\n"
            for i, result in enumerate(results["Results"][:10], 1):
                position = result.get("position", "-")
                driver = result.get("Driver", {})
                family_name = driver.get("familyName", "Unknown")
                given_name = driver.get("givenName", "")
                team = result.get("Constructor", {}).get("name", "Unknown")
                
                # 奖牌emoji
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                
                message += f"{medal} {family_name} ({team})\n"
            
            # 最快圈速
            fastest_lap = results["Results"][0].get("FastestLap", {})
            if fastest_lap and fastest_lap.get("rank") == "1":
                fastest_driver = results["Results"][0].get("Driver", {}).get("familyName", "")
                lap_time = fastest_lap.get("Time", {}).get("time", "")
                message += f"\n⚡ 最快圈速: {fastest_driver} - {lap_time}\n"
        
        message += "\n#F1 #Formula1"
        
        return self.send_message(message)
    
    def send_daily_schedule(self, sessions: List[Dict[str, Any]]) -> bool:
        """
        发送每日赛程汇总
        
        Args:
            sessions: 今日所有比赛环节
            
        Returns:
            是否发送成功
        """
        if not sessions:
            return False
        
        from pytz import timezone
        local_tz = timezone('Asia/Shanghai')
        
        race_name = sessions[0]['race_name']
        circuit = sessions[0]['circuit']
        
        message = f"""
📅 今日F1赛程 - {race_name}
🏟️ {circuit}

        """.strip()
        
        for session in sessions:
            utc_time = session['datetime']
            local_time = utc_time.astimezone(local_tz)
            time_str = local_time.strftime("%H:%M")
            
            emojis = {
                "fp1": "🏎️",
                "fp2": "🏎️", 
                "fp3": "🏎️",
                "qualifying": "⏱️",
                "sprint": "⚡",
                "race": "🏁"
            }
            
            emoji = emojis.get(session['type'], "🏎️")
            message += f"{emoji} {session['name']}: {time_str}\n"
        
        message += "\n记得准时观看！🏎️💨"
        
        return self.send_message(message)
    
    def send_startup_message(self) -> bool:
        """发送启动通知"""
        message = "✅ F1赛程提醒机器人已启动\n\n将为您自动推送：\n🏎️ 练习赛提醒\n⏱️ 排位赛提醒\n🏁 正赛提醒\n📊 赛后结果\n\nFormula 1 🏎️💨"
        return self.send_message(message)
    
    def logout(self):
        """登出微信"""
        if self.is_logged_in:
            itchat.logout()
            self.is_logged_in = False
            logger.info("微信已登出")
    
    @staticmethod
    def keep_alive():
        """保持微信连接活跃（需要在主循环中定期调用）"""
        try:
            # itchat会自动维护心跳，这里可以添加额外的保活逻辑
            pass
        except Exception as e:
            logger.warning(f"保活检查异常: {e}")


# 使用示例
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # 注意：运行此示例会要求扫码登录微信
    bot = WeChatBot("测试群")
    
    if bot.login(use_qr_code_image=False):
        # 发送测试消息
        bot.send_message("🧪 F1提醒机器人测试消息")
        
        # 保持运行一段时间
        print("按 Ctrl+C 退出")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            bot.logout()
    else:
        print("登录失败")