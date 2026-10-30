# 更新日志（CHANGELOG）

> **维护约定（强制）**：每次向 main 推送功能/修复，必须在本文档顶部追加对应条目（日期 + 新增/修复/变更分类 + 一句话影响面）。条目面向用户可读，内部重构一句话带过。

格式：## YYYY-MM-DD · 简短标题 ＋ 【新增】/【修复】/【文档】/【杂项】 分组列表

---

## 2026-10-29 (v1.0.1) · 重构（零行为变化）+ Review 修复 + 英文文档

【杂项】
- 内部重构：`common/llm_assistant.py` 拆分为 `common/llm/` 包（七模块 mixin 组合，外部 import 面不变）；main.py 指令分发拆为根目录 `handlers/` 包（八领域模块 + COMMAND_HANDLERS 注册表，main.py 3359→1875 行）——可维护性提升，用户无感知

【修复】
- L1 原生 function calling 路径在"finish_reason=stop 且同时返回 tool_calls"的兼容端点上会丢弃工具调用
- kimi 推理档位 env 配错（如 LLM_L2_REASONING=off）导致 L2 成文 400 误判失败：白名单校验+回退默认
- 截断续写两轮后仍截断时，拼接结果未做前言/退化复检；轮次耗尽时截断前缀可能未拼回（答案前半段丢失）
- /bili_login 扫码中途异常会永久占用防重入标记（只能重启恢复）
- /last 赛季初（1-3 月）查不到"上一场"（当前赛季无已结束环节时未回退上一赛季）
- /rate /ratings 在 scheduler 并发写时偶发 RuntimeError（改快照读取）
- 供应商级错误信息在并发问答下可能串用户（改 qid 键控，兼容旧读取）

【文档】
- 英文版全套文档：README_EN.md（主页）、docs/LLM_GUIDE_EN.md、docs/ADMIN_GUIDE_EN.md、deploy/README_EN.md
- 新增 LLM 接入手册 docs/LLM_GUIDE.md（中英双语）：每个配置项 ↔ 功能/模型分层对照表
- deploy/.env.example 增补本地大模型与向量召回配置项

---

## 2026-10-29 · Discord 版上线 + 三层衰减会话记忆 + 管理员运行时配置

【新增】
- **Discord 版本地化**：消息内时间全部改用 Discord 原生时间戳——按每个用户设备的时区/语言自动渲染（多时区服务器零配置）；`/tz` 设置个人时区供 AI 问答换算环节时间；/next /calendar /weather /gp 四个指令改用原生格式（不再硬编码北京时间）
- **Discord 版本**（海外用户）：`discord_version/` 全功能机器人——定时推送（环节提醒/赛后成绩/积分榜/每周预告）、31 条斜杠指令（含图表直传，无需公网域名）、`/ask` AI 问答（可带图）、双语界面（BOT_LANGUAGE=en/zh）；根目录 `Dockerfile.discord` 一键构建，详见 discord_version/README.md
- **三层衰减会话记忆**（`common/memory/`，两平台共有）：近期对话清晰、久远历史模糊、关键事实长期锚定——召回按"时间衰减 × 话题相关性 × 显著性"打分，与当前话题无关的历史不再污染回答；`/clear` 改为归档式清空（LLM 总结进长期记忆，1h 超时自动归档）；新增 `/memory` 自查与 `/memory forget <id>`；可选向量召回（MEMORY_EMBEDDING=provider 兼容远程与本地 Ollama，local=fastembed 需 ≥2GB 内存）
- **管理员运行时配置**（私聊/DM 专属）：`/llm` 查看/切换 AI 服务商、配置 API Key/模型/自定义端点，热生效无需重启；`/model` 分层模型新增第 6 层 memory（记忆总结，可配便宜或本地模型省成本）
- **本地大模型供应商**：Ollama/llama.cpp/vLLM 等 OpenAI 兼容端点（免 key，`LOCAL_LLM_MODEL` 或 `/llm setmodel local <模型>` 启用），高配服务器可完全离线化 AI 链路
- **出网门禁**：所有出站消息与日志自动对注册的密钥脱敏；运行时密钥文件（data/llm_secrets.json）0o600 存储且永不进 git/镜像/部署包
- 管理员速查卡：启动时自动 DM 管理员（ADMIN_GUIDE_PUSH 可关），完整手册见 docs/ADMIN_GUIDE.md

【修复】
- 代词追问（"那他队友呢"）不再因零实体交集丢失上文（会话指代词强制召回最近轮次；纯时间词不算指代）
- 同话题反复讨论自动锚定时不再洪泛（强实体限定+指纹去重+分层驱逐，人工纠错锚点永不误删）

【文档】
- 项目结构新增 discord_version/ 说明；管理员手册 docs/ADMIN_GUIDE.md

---

## 2026-10-27 · Team Radio 卡片 DIY + ICS 日历订阅

【新增】
- `/tr 车手 引语`：F1 转播风格 Team Radio 静态卡片 DIY（队色斜切条/车手头像/号码/队标/九区块伪波形；视觉规范参照 [wertewrt/F1_team_radio](https://github.com/wertewrt/F1_team_radio)，致谢该项目）
- `/calendar` 附 ICS 订阅链接：手机日历订阅后全年赛程自动同步、本地时区显示（新增 `/f1_calendar.ics` 端点；双源全挂返回 503 防订阅端清空已同步赛事）

【修复】
- ICS 路由的 503 有意响应不再被通用异常处理吞成 500
- matplotlib 3.10+ boxplot `vert` 弃用兼容（orientation/vert 双写，新旧版本均可）

## 2026-10-26 · 分层模型偏好 + 多供应商扩展 + 运营观测体系

【新增】
- `/models` `/model` 管理员指令：LLM 分层模型偏好（l0 改写/l1 取数/l2 成文/deep 深度分析/vision 识图逐层可换供应商+模型，`/models` 拉供应商实时清单校验），配置立即生效、可一键回默认
- LLM 供应商扩展：新增 OpenAI 兼容通用端点（自建网关/三方服务）、OpenRouter、Gemini 三个可选供应商
- 每日 9:30 管理员自检：数据源连通性（f1api.dev/Jolpica/OpenF1/F1Cosmos/FIA）+ LLM 可用性 + 指标计数，异常才 DM 打扰（20h 去重）
- 进程内指标计数（`common/metrics.py`）：指令/LLM/API 失败计数终身累计、定期落盘，自检按增量告警防疲劳
- 赛后成绩卡/积分榜卡附内联键盘（一键出三联图/Pace/进站对比/积分榜/走势）
- `/calendar` 附 ICS 订阅链接（手机日历自动同步全年赛程，双源全挂时返回 503 防订阅端清空）
- 指令打错字时 difflib 建议最接近的指令
- 视频导读/圈速纪录更新自动沉淀为知识候选（走同一两层闸门）
- FIA 文档解析为空的版式变更告警（管理员 DM，12h 去重）

【修复】
- 环节日期缺失时下游裸比较全线 TypeError（parse_session_datetime 改 None-safe + get_all_sessions 统一过滤）
- PU 用量赛前查询叠加 FIA 新申报文档（累计表周末口径滞后一站的实证修复）
- PU 信息 PDF 解析锚定标签行 + 列数校验 + 实质字段校验（防错位跨套与空壳缓存）
- matplotlib 3.10+ boxplot `vert` 弃用兼容（orientation 双写）
- L1 原生 function calling 在 finish_reason=stop 且带 tool_calls 时误丢弃工具调用

## 2026-10-26 · B站视频导读

【新增】
- B站 F1 视频链接导读：@机器人 发链接即可，字幕 → LLM 成文（链接解析 BV/av/b23；元数据三级回退抗风控；F1 关键词门禁防滥用）
- 字幕接口登录态：管理员 `/bili_login` 扫码（二维码私聊本人），Cookie 自动落盘复用
- 无字幕视频可选 ASR 本地转写兜底（`BILI_VIDEO_ASR_ENABLED=true` 开启，默认关）
- 登录态失效自动告警管理员重新扫码（12h 去重）
- AI 工具数 20 → 21（`get_video_summary`）

## 2026-10-23 · 投影状态机与图表缓存修复

【修复】
- 积分榜预估投影：基线改为赛季键控且只在无待入账窗口时推进，修复官方更新后 72h 内双计分的严重缺陷；积分按名次索引（DSQ 空位不再错分）；赛中不再误投影（校验环节实际结束时间）
- 统计图缓存：赛中渲染一律写日期戳文件名，不再把半成品图永久定格；positions/排位差距/进站/安全车四类图补齐文件名版本号（修图代码可破缓存）
- 图表渲染并发保护不再误杀其他模块在画的图
- 天气图在 OpenF1 不可达时回退 livetiming 实测归档（此前错误地给已完赛分站出预报图）
- 积分走势图某站拉取失败宁可不出图（此前残缺图会被缓存一周）
- OpenF1 认证加锁防 token 并发刷新；livetiming 干净比赛不再误报"数据源不可用"

## 2026-10-12 · 积分榜预估投影 + 升级件大包聚合

【新增】
- 官方积分榜更新滞后空窗期（24-48h）内，`/drivers` `/teams` 与 AI 积分榜查询返回按完赛位次本地预估的积分并明确标注"预估"口径，官方发布后自动恢复
- AI 问赛季升级汇总时返回"大升级清单"（核心性能部件/多件套装），不再只有干巴巴的条数

## 2026-10-10 · 安全车事件 + 统一图表工具 + 共享认证

【新增】
- `/sc` 指令与 AI 工具 `get_safety_car_events`：安全车/VSC/红旗事件时间线（含圈数区间与原因）
- AI 工具 `get_race_charts`：7 类统计图统一入口；`get_destructors`：事故成本榜（工具数 17 → 20）
- `/qualigap` 指令：排位差距图，支持冲刺排位（`sq` 后缀）
- 排位成绩推送附差距图（含冲刺排位）
- livetiming 归档缓存分级 TTL（正赛日+1 天前 30min，之后永久）

【修复】
- OpenF1 OAuth2 双实现漂移统一为共享提供器（1h 刷新 + 失败冷却 5min）
- Python 3.8 兼容（removeprefix → 切片）

## 2026-10-10 · livetiming 兜底源 + OpenF1 认证

【新增】
- F1 官方 livetiming 静态归档客户端：OpenF1 锁定窗口期的免费兜底源（stint 配方/赛会消息/天气），curl_cffi 指纹伪装绕机房封锁
- stint 分析支持 OpenF1 OAuth2（`OPENF1_USERNAME/PASSWORD`），锁定窗口内认证用户照常取数
- 兜底渲染写 `_fb` 日期戳文件名，OpenF1 恢复后自动升级正式版

## 2026-10-09 · 天气图 + 私聊图片路由修复 + README 预览

【新增】
- 天气时间序列图：已完赛=实测，未赛=逐小时预报；`/weather` 与 AI 天气查询自动附图
- README 增加效果预览画廊（6 张真实渲染图）

【修复】
- 私聊发图片指令不再串到群聊（图片按消息来源路由 + 被动回复绑定）
- OpenF1 429 限流按 Retry-After 退避；锁定窗口期 stint 数据回退 Jolpica；Jolpica 圈速分页拉全

## 2026-10-09 · 数据可视化大版本

【新增】
- 11 种统计图（pace 分布箱线/轮胎策略时间线/名次变化/排位差距/积分走势/赛后三联图/车队实力雷达/PU 用量堆叠/事故成本柱图/进站时长对比/安全车时间线）
- 新指令：`/pace` `/racechart` `/trend` `/pitstops`；`/pu` `/car` 自动附图
- 赛后自动推送三联统计图+安全车时间线，排位成绩附差距图
- AI 工具 `get_race_tech_params`：分站 FIA 动力单元官方技术参数（逐站 PDF 解析）
- F1Cosmos 事故维修成本数据源上线（页面 RSC 流抓取）
- 评分系统：预创建条目随赛程变更同步开启时刻、跨赛季自动清扫

【修复】
- `render_pace_chart` 函数定义丢失导致 pace 图全线崩溃（死代码嵌套）
- matplotlib 线程安全（进程级 Agg 初始化 + 渲染锁 + 原子出图）

## 2026-10-01 · 全量代码审查修复批

【修复】19 项确认的运行 bug，含 4 项高危：
- openf1_api 十个方法因缩进错误成为死代码（实时面板/天气格式化静默失效）
- 局部 import 遮蔽 datetime 导致 /last /gp 必崩
- 主数据源成绩转换丢失 Results 键（赛后成绩卡空卡 + 积分榜新鲜度门槛失效 + 7 天缓存污染）
- 预测模型 stint 特征键不匹配（维斯塔潘长距离特征静默失效）
- 另含：文件锁误删、积分榜门槛、年度清理回滚、缓存无界增长等

## 2026-10-01 · 文档与分析工具

【新增】analysis/ 预测模型回测与校准工具（改权重前必跑）
【文档】README 全面重写（面向初次接触用户：是什么/怎么部署/怎么用/FAQ）
【修复】实时面板容器 whisper 模型下载镜像配置透传

## 2026-09-29 · Docker 一键部署

【新增】deploy/ 一键部署：docker-compose（机器人+评分+实时面板+Caddy 自动 HTTPS）+ 部署脚本（含配置预检）+ 完整环境变量模板
【杂项】统一 LF 行尾（防 Windows clone 后脚本无法执行）

## 2026-09-28 · 开源首发

【新增】QQ 官方 Bot API V2 实现的 F1 群机器人开源版本：定时推送（赛前提醒/成绩/积分榜/新闻）、群指令、@机器人 AI 问答（联网 LLM + F1 数据工具）、车手评分（DOTD）、实时计时面板。代码去敏，MIT 协议
