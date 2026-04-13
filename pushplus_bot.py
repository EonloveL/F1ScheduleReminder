"""
F1赛程提醒机器人 - PushPlus消息推送模块
通过PushPlus将消息推送到个人微信
文档: http://www.pushplus.plus/
"""

import requests
import logging
from typing import Optional, Dict, Any, List
from urllib.parse import quote

logger = logging.getLogger(__name__)

class PushPlusBot:
    """PushPlus推送机器人封装类"""
    
    BASE_URL = "http://www.pushplus.plus/send"
    
    def __init__(self, token: str, topic: str = None):
        """
        初始化PushPlus机器人
        
        Args:
            token: PushPlus的Token（一对一消息）
            topic: 群组编码（一对多消息），可选
        """
        self.token = token
        self.topic = topic
        
    def send_message(self, title: str, content: str, template: str = "html") -> bool:
        """
        发送消息
        
        Args:
            title: 消息标题
            content: 消息内容（支持HTML）
            template: 模板类型
                     - html: HTML格式（默认，最丰富）
                     - txt: 纯文本
                     - json: JSON格式
                     - markdown: Markdown格式
                     
        Returns:
            是否发送成功
        """
        data = {
            "token": self.token,
            "title": title,
            "content": content,
            "template": template
        }
        
        if self.topic:
            data["topic"] = self.topic
            
        try:
            response = requests.post(
                self.BASE_URL,
                data=data,
                timeout=10
            )
            response.raise_for_status()
            
            result = response.json()
            if result.get("code") == 200:
                logger.info(f"消息发送成功: {title}")
                return True
            else:
                logger.error(f"消息发送失败: {result}")
                return False
                
        except Exception as e:
            logger.error(f"发送消息异常: {e}")
            return False
    
    def send_simple_message(self, content: str) -> bool:
        """
        发送简单消息（无标题，使用txt模板）
        
        Args:
            content: 消息内容
            
        Returns:
            是否发送成功
        """
        return self.send_message("F1提醒", content, template="txt")
    
    def send_session_reminder(self, session: Dict[str, Any]) -> bool:
        """
        发送比赛环节提醒消息
        
        Args:
            session: 比赛环节信息
            
        Returns:
            是否发送成功
        """
        from pytz import timezone
        from datetime import datetime
        
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
        
        title = f"{icon} F1提醒 - {session['name']}"
        
        # HTML格式内容
        content = f"""
        <h3>{icon} F1提醒 {icon}</h3>
        <hr/>
        <p><strong>📍 {session['race_name']}</strong></p>
        <p>🏟️ {session['circuit']}</p>
        <hr/>
        <h4>🔥 {session['name']} 即将开始</h4>
        <p><strong>⏰ {date_str} {weekday} {time_str} (北京时间)</strong></p>
        <p>敬请期待精彩比赛！🏎️💨</p>
        <hr/>
        <p><small>Formula 1 | {datetime.now().strftime('%Y-%m-%d %H:%M')}</small></p>
        """
        
        return self.send_message(title, content, template="html")
    
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
        title = f"🏁 {race_name} 比赛结果"
        
        content = f"<h3>🏁 {race_name} 比赛结果</h3><hr/>"
        
        if "Results" in results:
            content += "<h4>🥇 前十名:</h4><ol>"
            for i, result in enumerate(results["Results"][:10], 1):
                driver = result.get("Driver", {})
                family_name = driver.get("familyName", "Unknown")
                team = result.get("Constructor", {}).get("name", "Unknown")
                
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                content += f"<li><strong>{medal} {family_name}</strong> ({team})</li>"
            
            content += "</ol>"
            
            # 最快圈速
            fastest_lap = results["Results"][0].get("FastestLap", {})
            if fastest_lap and fastest_lap.get("rank") == "1":
                fastest_driver = results["Results"][0].get("Driver", {}).get("familyName", "")
                lap_time = fastest_lap.get("Time", {}).get("time", "")
                content += f"<p><strong>⚡ 最快圈速:</strong> {fastest_driver} - {lap_time}</p>"
        
        content += "<hr/><p><small>#F1 #Formula1</small></p>"
        
        return self.send_message(title, content, template="html")
    
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
        
        title = f"📅 今日F1赛程 - {race_name}"
        
        content = f"""
        <h3>📅 今日F1赛程</h3>
        <hr/>
        <p><strong>{race_name}</strong></p>
        <p>📍 {circuit}</p>
        <hr/>
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
            content += f"<p>{icon} <strong>{session['name']}</strong>: {time_str}</p>"
        
        content += "<hr/><p>记得准时观看！🏎️💨</p>"
        
        return self.send_message(title, content, template="html")
    
    def send_startup_message(self) -> bool:
        """发送启动通知"""
        title = "✅ F1赛程提醒机器人已启动"
        content = """
        <h3>✅ F1赛程提醒机器人已启动</h3>
        <hr/>
        <p>将为您自动推送：</p>
        <ul>
            <li>🏎️ 练习赛提醒</li>
            <li>⏱️ 排位赛提醒</li>
            <li>🏁 正赛提醒</li>
            <li>📊 赛后结果</li>
        </ul>
        <hr/>
        <p><strong>Formula 1 🏎️💨</strong></p>
        """
        return self.send_message(title, content, template="html")
    
    def send_test_message(self) -> bool:
        """发送测试消息"""
        return self.send_message(
            "🧪 测试消息", 
            "<p>如果您收到这条消息，说明PushPlus配置成功！</p>",
            template="html"
        )


# 使用示例
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # 替换为你的Token
    TOKEN = "你的PushPlus Token"
    
    bot = PushPlusBot(TOKEN)
    
    # 发送测试消息
    bot.send_test_message()
    
    # 发送HTML消息
    bot.send_message(
        "测试标题",
        "<h1>大标题</h1><p>这是<b>粗体</b>和<i>斜体</i>文字</p>",
        template="html"
    )
    
    # 发送Markdown消息
    bot.send_message(
        "Markdown测试",
        "# 标题\n\n- 列表项1\n- 列表项2",
        template="markdown"
    )