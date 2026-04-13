"""
F1赛道数据自动更新脚本
支持从多个数据源获取最新赛道信息并更新JSON文件

使用方法：
1. 手动更新：python update_data.py
2. 查看当前数据状态：python update_data.py --check
3. 测试API连接：python update_data.py --test-api
4. 强制更新所有数据：python update_data.py --force

作者：AI Assistant
日期：2024
"""

import requests
import json
import os
import sys
from datetime import datetime
from typing import Dict, Optional, List
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 配置文件路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, '..', 'common', 'circuits_data.json')


class F1DataUpdater:
    """F1数据更新器"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'F1-Reminder-Bot/1.0'
        })
        
    def load_current_data(self) -> Dict:
        """加载当前的JSON数据"""
        try:
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"数据文件未找到: {DATA_FILE}")
            return {}
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析错误: {e}")
            return {}
    
    def save_data(self, data: Dict):
        """保存数据到JSON文件"""
        try:
            # 创建备份
            if os.path.exists(DATA_FILE):
                backup_file = f"{DATA_FILE}.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                with open(DATA_FILE, 'r', encoding='utf-8') as f:
                    with open(backup_file, 'w', encoding='utf-8') as bf:
                        bf.write(f.read())
                logger.info(f"已创建备份: {backup_file}")
            
            # 保存新数据
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"数据已保存到: {DATA_FILE}")
        except Exception as e:
            logger.error(f"保存数据失败: {e}")
    
    def get_data_from_f1api(self, circuit_id: str) -> Optional[Dict]:
        """
        从f1api.dev获取赛道数据
        
        Args:
            circuit_id: 赛道ID
            
        Returns:
            格式化后的赛道数据
        """
        url = f"https://f1api.dev/api/circuits/{circuit_id}"
        
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if not data:
                return None
            
            return self._format_f1api_data(data)
            
        except requests.RequestException as e:
            logger.error(f"f1api.dev请求失败 {circuit_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"处理f1api.dev数据失败 {circuit_id}: {e}")
            return None
    
    def _format_f1api_data(self, api_data: Dict) -> Dict:
        """格式化f1api.dev数据为统一格式"""
        # 长度转换（米到公里）
        length_m = api_data.get('circuitLength', 0)
        length_km = length_m / 1000 if length_m else 0
        
        # 计算正赛距离
        laps = api_data.get('numberOfLaps', 0)
        race_distance = length_km * laps if length_km and laps else 0
        
        return {
            'name': self._translate_circuit_name(api_data.get('circuitName', '')),
            'name_en': api_data.get('circuitName', ''),
            'country': api_data.get('country', '未知'),
            'country_code': api_data.get('countryCode', ''),
            'flag': self._get_country_flag(api_data.get('countryCode', '')),
            'first_race': api_data.get('firstParticipationYear', '未知'),
            'lap_length_km': round(length_km, 3),
            'laps': laps,
            'race_distance_km': round(race_distance, 3),
            'lap_record': {
                'time': api_data.get('lapRecord', '未知'),
                'driver': self._translate_driver_name(api_data.get('fastestLapDriverId', '')),
                'driver_en': api_data.get('fastestLapDriverId', '未知'),
                'year': api_data.get('fastestLapYear', '未知')
            },
            'corners': api_data.get('numberOfCorners', '未知'),
            'location': api_data.get('city', '未知'),
            'url': api_data.get('url', ''),
            'last_updated': datetime.now().isoformat()
        }
    
    def _get_country_flag(self, country_code: str) -> str:
        """根据国家代码获取国旗emoji"""
        flag_map = {
            'JP': '🇯🇵', 'GB': '🇬🇧', 'IT': '🇮🇹', 'BE': '🇧🇪',
            'MC': '🇲🇨', 'AT': '🇦🇹', 'ES': '🇪🇸', 'HU': '🇭🇺',
            'FR': '🇫🇷', 'NL': '🇳🇱', 'US': '🇺🇸', 'BR': '🇧🇷',
            'AE': '🇦🇪', 'SA': '🇸🇦', 'AU': '🇦🇺', 'CN': '🇨🇳',
            'BH': '🇧🇭', 'QA': '🇶🇦', 'CA': '🇨🇦', 'MX': '🇲🇽',
            'AZ': '🇦🇿', 'SG': '🇸🇬', 'PT': '🇵🇹', 'TR': '🇹🇷',
            'DE': '🇩🇪', 'RU': '🇷🇺', 'AR': '🇦🇷', 'AT': '🇦🇹'
        }
        return flag_map.get(country_code, '🏁')
    
    def _translate_circuit_name(self, name_en: str) -> str:
        """翻译赛道名称"""
        translations = {
            'Suzuka Circuit': '铃鹿赛道',
            'Silverstone Circuit': '银石赛道',
            'Autodromo Nazionale Monza': '蒙扎赛道',
            'Circuit de Spa-Francorchamps': '斯帕-弗朗科尔尚赛道',
            'Circuit de Monaco': '摩纳哥赛道',
            'Red Bull Ring': '红牛环赛道',
            'Circuit de Barcelona-Catalunya': '巴塞罗那-加泰罗尼亚赛道',
            'Hungaroring': '亨格罗林赛道',
            'Circuit Zandvoort': '赞德福特赛道',
            'Circuit of the Americas': '美洲赛道',
            'Autódromo José Carlos Pace': '英特拉格斯赛道',
            'Yas Marina Circuit': '亚斯码头赛道',
            'Jeddah Corniche Circuit': '吉达滨海赛道',
            'Albert Park Circuit': '阿尔伯特公园赛道',
            'Shanghai International Circuit': '上海国际赛车场',
            'Bahrain International Circuit': '巴林国际赛道',
            'Miami International Autodrome': '迈阿密国际赛道',
            'Las Vegas Strip Circuit': '拉斯维加斯街道赛道',
            'Losail International Circuit': '罗赛尔国际赛道',
            'Autodromo Internazionale Enzo e Dino Ferrari': '恩佐与迪诺·法拉利赛道',
            'Autódromo Internacional do Algarve': '阿尔加维国际赛道',
            'Autodromo Internazionale del Mugello': '穆杰罗赛道',
            'Intercity Istanbul Park': '伊斯坦布尔赛道',
            'Nürburgring': '纽博格林赛道',
            'Sochi Autodrom': '索契赛道',
            'Circuit Gilles Villeneuve': '吉尔·维伦纽夫赛道',
            'Autódromo Hermanos Rodríguez': '罗德里格斯兄弟赛道',
            'Baku City Circuit': '巴库城市赛道',
            'Marina Bay Street Circuit': '滨海湾街道赛道',
            'Circuit Paul Ricard': '保罗·里卡尔赛道'
        }
        return translations.get(name_en, name_en)
    
    def _translate_driver_name(self, driver_id: str) -> str:
        """翻译车手名字"""
        translations = {
            'max_verstappen': '马克斯·维斯塔潘',
            'lewis_hamilton': '刘易斯·汉密尔顿',
            'valtteri_bottas': '瓦尔特里·博塔斯',
            'charles_leclerc': '夏尔·勒克莱尔',
            'carlos_sainz': '小卡洛斯·塞恩斯',
            'sergio_perez': '塞尔吉奥·佩雷兹',
            'lando_norris': '兰多·诺里斯',
            'george_russell': '乔治·拉塞尔',
            'fernando_alonso': '费尔南多·阿隆索',
            'sebastian_vettel': '塞巴斯蒂安·维特尔',
            'kimi_raikkonen': '基米·莱科宁',
            'rubens_barrichello': '鲁本斯·巴里切罗',
            'michael_schumacher': '迈克尔·舒马赫',
            'pedro_de_la_rosa': '佩德罗·德拉罗萨',
            'kevin_magnussen': '凯文·马格努森',
            'oscar_piastri': '奥斯卡·皮亚斯特里',
            'juan_pablo_montoya': '胡安·巴布罗·蒙托亚',
            'kimi_antonelli': '基米·安东内利',
            'alexander_albon': '亚历山大·阿尔本',
            'pierre_gasly': '皮埃尔·加斯利',
            'esteban_ocon': '埃斯特班·奥康',
            'lance_stroll': '兰斯·斯托尔',
            'yuki_tsunoda': '角田裕毅',
            'guanyu_zhou': '周冠宇',
            'nico_hulkenberg': '尼科·霍肯博格'
        }
        return translations.get(driver_id, driver_id.replace('_', ' ').title())
    
    def update_all_circuits(self, force: bool = False):
        """
        更新所有赛道数据
        
        Args:
            force: 是否强制更新所有数据，否则只更新缺失或过时的数据
        """
        current_data = self.load_current_data()
        updated_count = 0
        failed_count = 0
        
        logger.info("开始更新赛道数据...")
        logger.info(f"当前共有 {len(current_data)} 条赛道记录")
        
        # 定义所有赛道ID映射（Ergast API ID -> f1api.dev ID）
        circuit_mappings = {
            'suzuka': 'suzuka',
            'silverstone': 'silverstone',
            'monza': 'monza',
            'spa': 'spa',
            'monaco': 'monaco',
            'red_bull_ring': 'red_bull_ring',
            'catalunya': 'catalunya',
            'hungaroring': 'hungaroring',
            'paul_ricard': 'ricard',
            'zandvoort': 'zandvoort',
            'americas': 'americas',
            'interlagos': 'interlagos',
            'yas_marina': 'yas_marina',
            'jeddah': 'jeddah',
            'albert_park': 'albert_park',
            'shanghai': 'shanghai',
            'bahrain': 'bahrain',
            'miami': 'miami',
            'las_vegas': 'las_vegas',
            'losail': 'losail',
            'imola': 'imola',
            'portimao': 'portimao',
            'mugello': 'mugello',
            'istanbul': 'istanbul',
            'nurburgring': 'nurburgring',
            'sochi': 'sochi',
            'villeneuve': 'villeneuve',
            'rodriguez': 'rodriguez',
            'baku': 'baku',
            'marina_bay': 'marina_bay'
        }
        
        for circuit_id, f1api_id in circuit_mappings.items():
            logger.info(f"正在更新 {circuit_id}...")
            
            # 检查是否需要更新
            if not force and circuit_id in current_data:
                existing_data = current_data[circuit_id]
                # 检查是否有last_updated字段且数据不太旧（30天内）
                last_updated = existing_data.get('last_updated', '')
                if last_updated:
                    try:
                        last_date = datetime.fromisoformat(last_updated)
                        days_since_update = (datetime.now() - last_date).days
                        if days_since_update < 30:
                            logger.info(f"  {circuit_id} 数据较新（{days_since_update}天前），跳过")
                            continue
                    except:
                        pass
            
            # 获取新数据
            new_data = self.get_data_from_f1api(f1api_id)
            
            if new_data:
                # 保留原有的location字段（如果API没有提供）
                if circuit_id in current_data and 'location' in current_data[circuit_id]:
                    if not new_data.get('location') or new_data['location'] == '未知':
                        new_data['location'] = current_data[circuit_id]['location']
                
                current_data[circuit_id] = new_data
                updated_count += 1
                logger.info(f"  ✓ {circuit_id} 更新成功")
            else:
                failed_count += 1
                logger.warning(f"  ✗ {circuit_id} 更新失败")
        
        # 保存数据
        self.save_data(current_data)
        
        logger.info(f"\n更新完成！")
        logger.info(f"成功: {updated_count} 条")
        logger.info(f"失败: {failed_count} 条")
        logger.info(f"总计: {len(current_data)} 条")
    
    def check_data_status(self):
        """检查当前数据状态"""
        data = self.load_current_data()
        
        print("\n" + "="*60)
        print("F1赛道数据状态报告")
        print("="*60)
        print(f"\n数据文件: {DATA_FILE}")
        print(f"赛道总数: {len(data)}")
        
        # 检查数据新鲜度
        outdated = []
        future_records = []
        
        for circuit_id, circuit_data in data.items():
            # 检查更新时间
            last_updated = circuit_data.get('last_updated', '')
            if last_updated:
                try:
                    last_date = datetime.fromisoformat(last_updated)
                    days_since = (datetime.now() - last_date).days
                    if days_since > 30:
                        outdated.append((circuit_id, days_since))
                except:
                    pass
            
            # 检查是否有未来年份的记录
            lap_record = circuit_data.get('lap_record', {})
            year = lap_record.get('year', 0)
            if isinstance(year, int) and year > datetime.now().year:
                future_records.append((circuit_id, year))
        
        print(f"\n数据新鲜度:")
        print(f"  - 需要更新（超过30天）: {len(outdated)} 条")
        if outdated:
            for cid, days in outdated[:5]:  # 只显示前5个
                print(f"    {cid}: {days}天前")
        
        print(f"\n数据质量检查:")
        print(f"  - 未来年份记录: {len(future_records)} 条")
        if future_records:
            print(f"  ⚠️  警告: 以下赛道有未来年份的圈速记录:")
            for cid, year in future_records:
                print(f"    - {cid}: {year}年")
                print(f"      建议: 请检查并更新为实际的历史记录")
        
        print("\n" + "="*60)
    
    def test_api_connection(self):
        """测试API连接"""
        print("\n测试API连接...")
        
        test_circuits = ['suzuka', 'silverstone', 'monza']
        
        for circuit_id in test_circuits:
            url = f"https://f1api.dev/api/circuits/{circuit_id}"
            try:
                response = self.session.get(url, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    print(f"✓ {circuit_id}: 连接成功")
                    print(f"  赛道名: {data.get('circuitName', 'N/A')}")
                    print(f"  国家: {data.get('country', 'N/A')}")
                else:
                    print(f"✗ {circuit_id}: HTTP {response.status_code}")
            except Exception as e:
                print(f"✗ {circuit_id}: {e}")
        
        print("\nAPI测试完成")


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='F1赛道数据自动更新脚本')
    parser.add_argument('--check', action='store_true', help='检查当前数据状态')
    parser.add_argument('--test-api', action='store_true', help='测试API连接')
    parser.add_argument('--force', action='store_true', help='强制更新所有数据')
    
    args = parser.parse_args()
    
    updater = F1DataUpdater()
    
    if args.check:
        updater.check_data_status()
    elif args.test_api:
        updater.test_api_connection()
    else:
        # 默认执行更新
        updater.update_all_circuits(force=args.force)


if __name__ == "__main__":
    main()
