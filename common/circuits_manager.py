"""
F1赛程提醒机器人 - 赛道数据管理器
管理 common/circuits_data.json：
1. 赛道信息查询（按ID/名称）
2. 圈速纪录追踪（赛后检测新纪录并更新）
3. LLM核查结果回写（写前自动备份）
"""

import json
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# circuit_id -> formula1.com 赛道页 slug（2026赛季赛历，取自官网 /en/racing/2026）
F1_OFFICIAL_SLUGS = {
    "albert_park": "australia",
    "shanghai": "china",
    "suzuka": "japan",
    "bahrain": "bahrain",
    "miami": "miami",
    "villeneuve": "canada",
    "monaco": "monaco",
    "catalunya": "barcelona-catalunya",
    "red_bull_ring": "austria",
    "silverstone": "great-britain",
    "spa": "belgium",
    "hungaroring": "hungary",
    "zandvoort": "netherlands",
    "monza": "italy",
    "baku": "azerbaijan",
    "marina_bay": "singapore",
    "americas": "united-states",
    "rodriguez": "mexico",
    "interlagos": "brazil",
    "las_vegas": "las-vegas",
    "losail": "qatar",
    "yas_marina": "united-arab-emirates",
}


def _default_data_file() -> str:
    """数据文件默认路径：data/circuits_data.json（Docker卷挂载目录，容器重建不丢）

    旧路径 common/circuits_data.json 存在时自动迁移。
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    new_path = os.path.join(project_root, "data", "circuits_data.json")
    old_path = os.path.join(current_dir, "circuits_data.json")
    if not os.path.exists(new_path) and os.path.exists(old_path):
        try:
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            shutil.copy2(old_path, new_path)
            logger.info(f"赛道数据已从旧路径迁移: {old_path} -> {new_path}")
        except Exception as e:
            logger.error(f"赛道数据迁移失败，继续使用旧路径: {e}")
            return old_path
    return new_path


def _default_backup_dir() -> str:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    backup_dir = os.path.join(project_root, "data", "backup")
    os.makedirs(backup_dir, exist_ok=True)
    return backup_dir


class CircuitsManager:
    """赛道数据管理器"""

    def __init__(self, data_file: str = None, backup_dir: str = None):
        self.data_file = data_file or _default_data_file()
        self.backup_dir = backup_dir or _default_backup_dir()
        self.circuits_data = self._load_data()

    def _load_data(self) -> Dict:
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"加载赛道数据失败: {e}")
        return {}

    def reload(self):
        """重新加载数据文件"""
        self.circuits_data = self._load_data()

    def _save_data(self) -> bool:
        """保存数据（写前备份原文件）"""
        try:
            if os.path.exists(self.data_file):
                os.makedirs(self.backup_dir, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(self.backup_dir, f"circuits_data_{stamp}.json")
                shutil.copy2(self.data_file, backup_path)
                logger.info(f"原赛道数据已备份: {backup_path}")

            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.circuits_data, f, ensure_ascii=False, indent=2)
            logger.info("✓ 赛道数据已保存")
            return True
        except Exception as e:
            logger.error(f"保存赛道数据失败: {e}")
            return False

    # ==================== 查询 ====================

    def get_circuit(self, circuit_id: str) -> Optional[Dict]:
        """按赛道ID查询（如 'suzuka'）"""
        if not circuit_id:
            return None
        data = self.circuits_data.get(circuit_id.lower())
        return data.copy() if data else None

    def search_circuit(self, query: str) -> Optional[Tuple[str, Dict]]:
        """
        按名称搜索赛道

        Returns:
            (circuit_id, circuit_data) 或 None
        """
        if not query:
            return None
        query_lower = query.lower()
        for circuit_id, data in self.circuits_data.items():
            if (query_lower in data.get('name_en', '').lower()
                    or query_lower in data.get('name', '').lower()
                    or query_lower in circuit_id.lower()):
                return circuit_id, data.copy()
        return None

    def get_circuit_timezone(self, query: str) -> Optional[str]:
        """按名称模糊查询赛道当地时间（IANA 时区名，如 'Asia/Tokyo'）；未配置/未命中返回 None"""
        hit = self.search_circuit(query)
        if hit:
            return hit[1].get('timezone')
        return None

    @staticmethod
    def format_dual_time(utc_dt, track_tz: str = None) -> str:
        """双时区时间显示：有赛道时区时输出 '赛道当地… / 北京时间…'，否则仅北京时间

        Args:
            utc_dt: aware datetime（UTC 或任意时区）
            track_tz: IANA 时区名（来自 circuits_data.json 的 timezone 字段）
        """
        from pytz import timezone as _tz
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        bj = utc_dt.astimezone(_tz('Asia/Shanghai'))
        bj_str = f"{bj.strftime('%m月%d日')} {weekday[bj.weekday()]} {bj.strftime('%H:%M')}"
        if not track_tz or track_tz == 'Asia/Shanghai':
            return f"{bj_str}（北京时间）"
        try:
            local = utc_dt.astimezone(_tz(track_tz))
            local_str = f"{local.strftime('%m月%d日')} {weekday[local.weekday()]} {local.strftime('%H:%M')}"
            return f"赛道当地 {local_str} / 北京时间 {bj_str}"
        except Exception:
            return f"{bj_str}（北京时间）"

    def get_circuit_record(self, circuit_id: str) -> Optional[Dict]:
        """获取赛道当前圈速纪录"""
        circuit = self.circuits_data.get(circuit_id)
        if circuit:
            return circuit.get('lap_record')
        return None

    # ==================== 圈速纪录追踪 ====================

    @staticmethod
    def time_to_seconds(time_str: str) -> Optional[float]:
        """将圈速时间字符串转换为秒数"""
        if not time_str or time_str == '暂无记录':
            return None
        try:
            parts = str(time_str).strip().split(':')
            if len(parts) == 3:
                # f1api.dev 历史排位格式 "1:29:179"（分:秒:毫秒，毫秒用冒号而非点）
                return float(parts[0]) * 60 + float(parts[1]) + float(parts[2]) / 1000
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            return float(parts[0])
        except (ValueError, IndexError):
            return None

    def check_and_update_record(self, circuit_id: str, new_time: str,
                                driver_name: str, driver_id: str,
                                year: int) -> Tuple[bool, str]:
        """
        检查新圈速是否打破赛道纪录，打破则更新本地数据

        Returns:
            (是否产生新纪录, 描述消息)
        """
        if circuit_id not in self.circuits_data:
            return False, f"赛道 {circuit_id} 不在本地数据中"

        circuit = self.circuits_data[circuit_id]
        circuit_name = circuit.get('name', circuit_id)

        current_record = circuit.get('lap_record', {})
        current_time_str = current_record.get('time', '')

        current_seconds = self.time_to_seconds(current_time_str)
        new_seconds = self.time_to_seconds(new_time)

        if new_seconds is None:
            return False, f"无法解析新圈速时间: {new_time}"

        if current_seconds is None or new_seconds < current_seconds:
            old_record_str = current_time_str or "暂无记录"

            circuit['lap_record'] = {
                'time': new_time,
                'driver': driver_name,
                'driver_en': driver_id,
                'year': year
            }
            circuit['last_updated'] = datetime.now().strftime("%Y-%m-%d")
            circuit['updated_by'] = 'race_result'

            if self._save_data():
                improvement = ""
                if current_seconds:
                    diff = current_seconds - new_seconds
                    improvement = f"，提升了 {diff:.3f} 秒"
                msg = (f"🎉 新圈速纪录！\n"
                       f"⏱️ {new_time} ({driver_name}，{year})\n"
                       f"旧纪录: {old_record_str}{improvement}")
                logger.info(f"✓ 圈速纪录已更新: {circuit_name} - {new_time}")
                return True, msg
            return False, "保存数据失败"

        diff = new_seconds - current_seconds
        msg = (f"📊 {circuit_name} 圈速对比: 本次 {new_time} vs "
               f"纪录 {current_time_str} (+{diff:.3f}s)")
        return False, msg

    def apply_llm_verified_record(self, circuit_id: str, llm_record: Dict) -> bool:
        """
        将LLM联网核查确认的圈速纪录回写本地数据

        Args:
            circuit_id: 赛道ID
            llm_record: verify_lap_record() 返回的纪录

        Returns:
            是否更新成功
        """
        if circuit_id not in self.circuits_data:
            logger.warning(f"赛道 {circuit_id} 不在本地数据中，无法回写")
            return False

        circuit = self.circuits_data[circuit_id]
        old = circuit.get('lap_record', {})

        circuit['lap_record'] = {
            'time': llm_record['time'],
            'driver': llm_record.get('driver') or llm_record.get('driver_en', ''),
            'driver_en': llm_record.get('driver_en', ''),
            'year': llm_record.get('year', '')
        }
        circuit['last_updated'] = datetime.now().strftime("%Y-%m-%d")
        circuit['updated_by'] = 'llm_verified'

        if self._save_data():
            logger.info(
                f"✓ 已按LLM核查结果更新 {circuit.get('name', circuit_id)} 圈速纪录: "
                f"{old.get('time', '无')} -> {llm_record['time']}"
            )
            return True
        return False

    # ==================== F1官网权威源 ====================

    def fetch_official_lap_record(self, circuit_id: str, season: int = None) -> Optional[Dict]:
        """
        从F1官网赛道页抓取圈速纪录（权威源，SSR直出HTML可正则提取）
        页面结构: <dt>Fastest lap time</dt><dd>1:20.901</dd><span>Lando Norris (2025)</span>

        Returns:
            {"time": "1:20.901", "driver_en": "Lando Norris", "driver": "Lando Norris", "year": 2025}
            抓取失败/无映射slug返回None
        """
        slug = F1_OFFICIAL_SLUGS.get((circuit_id or "").lower())
        if not slug:
            logger.warning(f"赛道 {circuit_id} 无官网slug映射，跳过官网核查")
            return None
        season = season or datetime.now().year
        url = f"https://www.formula1.com/en/racing/{season}/{slug}/circuit"

        import requests
        try:
            resp = requests.get(
                url, timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                proxies={"http": None, "https": None},
            )
            if resp.status_code != 200:
                logger.warning(f"F1官网赛道页请求失败: HTTP {resp.status_code} - {url}")
                return None
        except Exception as e:
            logger.warning(f"F1官网赛道页请求异常: {e} - {url}")
            return None

        m = re.search(
            r"Fastest lap time</dt>\s*<dd[^>]*>([^<]+)</dd>\s*<span[^>]*>([^<]+)</span>",
            resp.text,
        )
        if not m:
            logger.warning(f"F1官网赛道页未解析到圈速纪录: {url}")
            return None

        lap_time = m.group(1).strip()
        driver_text = m.group(2).strip()  # "Lando Norris (2025)"
        dm = re.match(r"^(.*?)\s*\((\d{4})\)\s*$", driver_text)
        driver_en = dm.group(1).strip() if dm else driver_text
        year = int(dm.group(2)) if dm else ""

        record = {"time": lap_time, "driver_en": driver_en, "driver": driver_en, "year": year}

        # 尝试从车手别名表补充中文名
        try:
            from .user_prefs import DRIVER_ALIASES
            last = driver_en.split()[-1].lower() if driver_en else ""
            for cn, did in DRIVER_ALIASES.items():
                if did == last or did.split("_")[-1] == last:
                    record["driver"] = cn
                    break
        except Exception:
            pass

        logger.info(f"✓ F1官网圈速纪录[{circuit_id}]: {lap_time} ({driver_en}, {year})")
        return record

    def apply_official_record(self, circuit_id: str, record: Dict) -> bool:
        """将F1官网圈速纪录回写本地数据（updated_by=f1_official）"""
        if not record or not record.get("time"):
            return False
        if circuit_id not in self.circuits_data:
            logger.warning(f"赛道 {circuit_id} 不在本地数据中，无法回写")
            return False

        circuit = self.circuits_data[circuit_id]
        old = circuit.get('lap_record', {})
        circuit['lap_record'] = {
            'time': record['time'],
            'driver': record.get('driver') or record.get('driver_en', ''),
            'driver_en': record.get('driver_en', ''),
            'year': record.get('year', ''),
        }
        circuit['last_updated'] = datetime.now().strftime("%Y-%m-%d")
        circuit['updated_by'] = 'f1_official'

        if self._save_data():
            logger.info(
                f"✓ 已按F1官网数据更新 {circuit.get('name', circuit_id)} 圈速纪录: "
                f"{old.get('time', '无')} -> {record['time']}"
            )
            return True
        return False


def extract_fastest_lap_from_results(results: Dict) -> Optional[Dict]:
    """从比赛结果中提取最快圈速信息"""
    try:
        if not results or "Results" not in results:
            return None

        for result in results["Results"]:
            fastest_lap = result.get("FastestLap", {})
            if fastest_lap and fastest_lap.get("rank") == "1":
                driver = result.get("Driver", {})
                return {
                    "time": fastest_lap.get("Time", {}).get("time", ""),
                    "driver_name": f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip(),
                    "driver_id": driver.get("driverId", ""),
                    "year": datetime.now().year
                }

        for result in results["Results"]:
            fastest_lap = result.get("FastestLap", {})
            if fastest_lap:
                driver = result.get("Driver", {})
                return {
                    "time": fastest_lap.get("Time", {}).get("time", ""),
                    "driver_name": f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip(),
                    "driver_id": driver.get("driverId", ""),
                    "year": datetime.now().year
                }
    except Exception as e:
        logger.error(f"提取最快圈速失败: {e}")

    return None
