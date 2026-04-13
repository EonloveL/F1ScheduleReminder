"""
F1赛程提醒机器人 - 企业微信机器人消息模块
使用企业微信Webhook实现消息推送
文档: https://developer.work.weixin.qq.com/document/path/91770
"""

import requests
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)

class WeComBot:
    """企业微信机器人封装类"""
    
    def __init__(self, webhook_url: str):
        """
        初始化企业微信机器人
        
        Args:
            webhook_url: 企业微信机器人的Webhook地址
                        格式: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx
        """
        self.webhook_url = webhook_url
        
    def send_text(self, content: str, mentioned_list: List[str] = None, 
                  mentioned_mobile_list: List[str] = None) -> bool:
        """
        发送文本消息
        
        Args:
            content: 消息内容，最长不超过2048个字节
            mentioned_list: @userid列表，@all表示所有人
            mentioned_mobile_list: @手机号列表
            
        Returns:
            是否发送成功
        """
        data = {
            "msgtype": "text",
            "text": {
                "content": content
            }
        }
        
        if mentioned_list:
            data["text"]["mentioned_list"] = mentioned_list
        if mentioned_mobile_list:
            data["text"]["mentioned_mobile_list"] = mentioned_mobile_list
            
        return self._send(data)
    
    def send_markdown(self, content: str) -> bool:
        """
        发送Markdown消息（格式更丰富）
        
        Args:
            content: Markdown格式的消息内容
            
        Returns:
            是否发送成功
        """
        data = {
            "msgtype": "markdown",
            "markdown": {
                "content": content
            }
        }
        return self._send(data)
    
    def _send(self, data: Dict) -> bool:
        """
        发送消息到企业微信
        
        Args:
            data: 消息数据
            
        Returns:
            是否发送成功
        """
        headers = {
            'Content-Type': 'application/json; charset=utf-8'
        }
        
        try:
            response = requests.post(
                self.webhook_url,
                headers=headers,
                data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                timeout=10
            )
            response.raise_for_status()
            
            result = response.json()
            if result.get('errcode') == 0:
                logger.info(f"消息发送成功: {data.get('msgtype', 'unknown')}")
                return True
            else:
                logger.error(f"消息发送失败: {result}")
                return False
                
        except Exception as e:
            logger.error(f"发送消息异常: {e}")
            return False
    
    def send_session_reminder(self, session: Dict[str, Any]) -> bool:
        """
        发送比赛环节提醒消息（使用Markdown格式更美观）
        
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
        date_str = local_time.strftime("%Y年%m月%d日")
        time_str = local_time.strftime("%H:%M")
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][local_time.weekday()]
        
        # 图标映射
        icons = {
            "fp1": "🏎️",
            "fp2": "🏎️",
            "fp3": "🏎️",
            "qualifying": "⏱️",
            "sprint": "⚡",
            "race": "🏁"
        }
        
        icon = icons.get(session['type'], "🏎️")
        
        # 构建Markdown消息
        message = f"""{icon} **F1提醒** {icon}

> **{session['race_name']}**
> 
> 📍 {session['circuit']}

**{session['name']}** 即将开始

⏰ **{date_str} {weekday} {time_str}** (北京时间)

敬请期待精彩比赛！🏎️💨

---
<font color='info'>Formula 1</font> | #{session['race_name'].replace(' ', '')}
        """
        
        return self.send_markdown(message)
    
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
        
        message = f"🏁 **{race_name} 比赛结果**\n\n"
        
        if "Results" in results:
            message += "🥇 **前十名**:\n\n"
            for i, result in enumerate(results["Results"][:10], 1):
                driver = result.get("Driver", {})
                family_name = driver.get("familyName", "Unknown")
                team = result.get("Constructor", {}).get("name", "Unknown")
                
                # 奖牌
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                message += f"{medal} **{family_name}** ({team})\n"
            
            # 最快圈速
            fastest_lap = results["Results"][0].get("FastestLap", {})
            if fastest_lap and fastest_lap.get("rank") == "1":
                fastest_driver = results["Results"][0].get("Driver", {}).get("familyName", "")
                lap_time = fastest_lap.get("Time", {}).get("time", "")
                message += f"\n⚡ **最快圈速**: {fastest_driver} - {lap_time}\n"
        
        message += "\n---\n#F1 #Formula1"
        
        return self.send_markdown(message)
    
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
        
        message = f"""📅 **今日F1赛程** - {race_name}

📍 {circuit}

"""
        
        icons = {
            "fp1": "🏎️",
            "fp2": "🏎️", 
            "fp3": "🏎️",
            "qualifying": "⏱️",
            "sprint": "⚡",
            "race": "🏁"
        }
        
        for session in sessions:
            utc_time = session['datetime']
            local_time = utc_time.astimezone(local_tz)
            time_str = local_time.strftime("%H:%M")
            icon = icons.get(session['type'], "🏎️")
            message += f"{icon} **{session['name']}**: {time_str}\n"
        
        message += "\n记得准时观看！🏎️💨"
        
        return self.send_markdown(message)
    
    def send_startup_message(self) -> bool:
        """发送启动通知"""
        message = """✅ **F1赛程提醒机器人已启动**

将为您自动推送：
- 🏎️ 练习赛提醒
- ⏱️ 排位赛提醒  
- 🏁 正赛提醒
- 📊 赛后结果

---
**Formula 1** 🏎️💨"""
        
        return self.send_markdown(message)
    
    def send_test_message(self) -> bool:
        """发送测试消息"""
        message = "🧪 **测试消息**\n\n如果您收到这条消息，说明企业微信机器人配置成功！"
        return self.send_markdown(message)


# 使用示例
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # 替换为你的Webhook地址
    WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY_HERE"
    
    bot = WeComBot(WEBHOOK_URL)
    
    # 发送测试消息
    bot.send_test_message()
    
    # 发送文本消息
    bot.send_text("这是一条普通文本消息")
    
    # 发送Markdown消息
    bot.send_markdown("""**加粗文字**
> 引用文字

- 列表项1
- 列表项2

<font color='info'>绿色文字</font>
<font color='warning'>橙色文字</font>
<font color='comment'>灰色文字</font>
    """)