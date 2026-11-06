# TrustMRR · GitHub Actions 研究生产者

独立、开源的 TrustMRR 每周商业研究项目，仅产出 **Notion READY** 原稿。**没有 Docker、systemd、SQLite、本地历史持久化、微信公众号或 CMS 发布代码。**

- GitHub Actions：每周日 17:00 UTC（北京时间**周一 01:00**）运行。
- 数据源：仅 TrustMRR 官方 API 的 fastestGrowingStartups Top25，不抓网页或其它榜单。
- 研究：OmniRoute Chat Completions；对原始产品字段防提示注入，不推断未提供的利润、获客成本、留存或真实收入。
- 存储：Notion 专栏「09 · 潜界 AI｜TrustMRR 增长情报」是文章和历史的唯一持久存储。每篇写入成功后回读全文、校验 SHA-256，并在末尾标记机器可验证 READY。
- 月报：当月第一个周一，连续四期 Notion 周报均为有效 READY 且包含完整 Top25 排名时生成；否则明确跳过。
- 下游：现有的 qjai-text-publisher 周一**03:00**只读取 Notion 的 READY 周报或月报，分别创建微信公众号与 CMS 草稿。此仓库绝不直写微信/CMS。

## 安全启动：分阶段切换

生产调度的代码已提交，但 GitHub Actions **默认不生产**。此设计专门避免与旧 qjai-TrustMRR-mp-publisher 在周一 01:00 同时采集、AI 生成、写 Notion。

1. 在新仓库 Settings → Secrets and variables → Actions → **Repository secrets** 配置：NOTION_API_KEY、OMNIROUTE_API_KEY。
2. 在 **Repository variables** 配置：OMNIROUTE_BASE_URL、OMNIROUTE_MODEL、TRUSTMRR_RESEARCH_AUTHORIZED=true、TRUSTMRR_AI_AUTHORIZED=true。授权开关仅表示用户已取得对应用途的书面许可，不意味着由本项目自行取得。
3. 先在 Actions 中手动运行「TrustMRR Weekly Notion Producer」的 workflow_dispatch：只执行离线测试与 Notion **只读**检查；不会请求 TrustMRR 数据、调用 AI 或创建文章。
4. **正式切换前**先禁用硅谷旧 qjai-trustmrr-weekly.timer，确认 inactive/disabled；保留旧项目回滚材料及渠道幂等记录。确保新旧上游不会同时运行。
5. 之后才将新仓库 Repository variable **TRUSTMRR_PRODUCTION_ENABLED=true**，由下一个周一 01:00 GitHub Actions 自然运行。Notion 必须完整 READY；03:00 下游再投递双草稿。确认 Notion/微信/CMS/定时日志均通过后才允许删除旧上游项目服务器目录、旧定时器、旧仓库和安全确认的专属备份。
6. 若首次执行失败，保持旧项目停用以防重复投递；检查失败原因并协调人工恢复，不自动重放/补投或恢复两条链路并行。

> 2026-11-09 是下一次周一。GitHub schedule 可能延迟，03:00 前未 READY 时统一发布器安全跳过，不会改用旧报告补发。生产开关未打开时 schedule 只跑离线测试，不创建文章。

## 操作命令

Python 3.12+，仅标准库：

    python -m unittest discover -s tests -v
    python -m trustmrr.main check
    python -m trustmrr.main produce

check 只验证 Notion 页面访问与已经存在的 READY；produce 仅允许北京时间周一执行，且要求 TRUSTMRR_PRODUCTION_ENABLED=true、TRUSTMRR_RESEARCH_AUTHORIZED=true、TRUSTMRR_AI_AUTHORIZED=true、NOTION_ENABLE_SYNC=true、NOTION_AUTOSYNC_AFTER_GENERATION=true。Notion 父页面固定为 4109e605-ccf8-81ed-a46c-f9ab95538f5b，不允许运行时意外指向其它页面。

## 运行协议

| 类型 | Notion 页面标题格式 | source_key |
|---|---|---|
| 周报 | YYYY-MM-DD｜TrustMRR 周报｜独立产品增长观察｜MM-DD | trustmrr-weekly:YYYY-MM-DD |
| 月报 | YYYY-MM｜TrustMRR 月报｜独立产品增长趋势｜YYYY-MM 四周观察 | trustmrr-monthly:YYYY-MM |

Notion 文章正文以最终机器标记 QJAI_PUBLISH_READY v=1 source_key=... content_sha256=... 结束，内容 Hash 使用 Notion 实际回读文本（不包含最后 READY 行），与统一发布器保持一致。

本项目不用 GitHub Artifacts、Cache 或提交公开 JSON 保存原始产品快照：每期从 Notion 的 READY 周报来源链接恢复前一周排名，并用连续四期计算月报入榜频次。历史不完整、文章被修改或来源行无法唯一对应时 fail-closed，不补造排名。定时为固定一次，没有自动追赶（catch-up）、重复 AI 生产或直接发布 API。

## 项目范围

不要向公开仓库写入任何密钥、账号信息、生成的含敏感内容的报告、手工导出的完整历史数据库。GitHub Runner 是短生命周期执行环境。旧项目为迁移期参照，**不再修改旧项目代码**。
