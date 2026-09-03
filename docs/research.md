# GitHub 方案调研

快照日期：2026-08-31。Stars 会变化，以下数字只用于判断社区与维护活跃度。

| 项目 | 快照 | 优点 | 主要问题 | 判断 |
|---|---|---|---|---|
| [gallery-dl](https://github.com/mikf/gallery-dl) | 19.3k stars；v1.32.10；GPL-2.0 | 帖子、Reels、carousel、媒体和元数据；下载 archive、命名、Cookie 与限速成熟 | 不以评论为重点；活跃开发已迁往 [Codeberg](https://codeberg.org/mikf/gallery-dl)；GPL 分发需留意 | 本项目采用：锁版本、作为独立 CLI 薄封装 |
| [Instaloader](https://github.com/instaloader/instaloader) | 13.3k stars；v4.15.3；MIT | Python API 清晰；帖子、Reels、评论、过滤与 session 支持较好 | 近期仍持续修复 GraphQL/profile 变动；匿名访问和 429 不稳定 | 若后续需要评论或更细业务对象，作为第二后端 |
| [instagrapi](https://github.com/subzeroid/instagrapi) | 6.7k stars；v2.18.18；MIT | Private API 能力完整，数据结构丰富 | challenge、设备/IP 信任和封号风险最高，生产稳定性弱 | 不采用 |
| [Osintgram](https://github.com/Datalux/Osintgram) | 14.2k stars；GPL-3.0 | OSINT 功能多 | 更像交互式调查工具，非稳定采集后端；正式版较旧 | 不采用 |
| [instascrape](https://github.com/chris-greening/instascrape) | 663 stars；MIT | 代码简单 | 2023 年已归档 | 排除 |

## 路线判断

1. 自有或已授权的专业账号：优先使用 [Meta 官方 Instagram API 文档](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api)，稳定性和合规性最好。
2. 任意公开账号的小规模、已确认授权的采集：非官方工具只能作为易失效的技术适配层；当前选 `gallery-dl` 是因为媒体归档能力和维护频率更合适。
3. 不自建 HTML/GraphQL crawler：要重复维护 CSRF、接口版本、carousel、Reels、分页和限流，且并不会改善平台授权问题。
4. 不直接 fork：只有上游确有阻塞 bug 且配置无法解决时，才做最小补丁，并尽量回馈上游。

## 随机发现的实现边界

Instagram 和本次评估的开源采集器都没有提供“从全站所有公开帖子中均匀随机取一条”的接口。直接把 Hashtag 的“近期”结果叫作全站随机，会产生错误的统计含义，因此本项目采用可解释的分层抽样：

1. 从内置的多语言公共 Hashtag 主题池中随机选择若干来源；
2. 串行读取各来源的近期候选，并按帖子 ID 去重；
3. 在候选集合上执行用户筛选；
4. 使用本次随机种子打散命中结果，再按数量上限截断；
5. 在 `run.json` 中记录种子和来源，便于复现和审计。

这种方法适合“随便发现一批公开帖子”的产品需求，但样本仍受主题池、平台排序、地区、时间和登录状态影响，不能用作 Instagram 全站无偏统计样本。指定账号、Hashtag 或帖子 URL 的采集保留为高级模式。

## 已验证事实

- GitHub 浅克隆的 `gallery-dl` 提交为 `4f0abb2`（v1.32.10，2026-08-29）。
- GitHub 浅克隆的 `Instaloader` 提交为 `5434692`（v4.15.3，2026-07-26）。
- 当前环境中的匿名 Instagram profile/post 请求均被重定向至登录页；因此实时测试必须由使用者明确提供 Cookie。
- `gallery-dl` 默认给 Instagram 配置 6–12 秒请求间隔；本包装保留该默认值，并对 429 使用更长等待。
