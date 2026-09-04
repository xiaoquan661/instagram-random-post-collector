# Instagram 随机帖子发现与筛选采集器

这是一个面向小规模、合规采集的 Instagram 工具。默认的“随机发现”模式不要求填写目标账号、用户名或帖子 URL：选择随机范围和希望得到的数量后，即可发现公开帖子，再按日期、点赞数、文案关键词、Hashtag 和媒体类型筛选；也可以只下载最终命中帖子的图片与视频。

这里的“随机”有明确边界：程序从内置的多语言公共 Hashtag 主题池中抽取若干来源，依次读取近期候选，然后执行去重、条件筛选、随机打散和数量截断。它不是 Instagram 全站帖子库的均匀随机抽样，也不能代表整个平台的内容分布。

工具使用锁定版本的 `gallery-dl` 获取数据，输出稳定的 JSONL。它不采集评论，不接收账号密码，不自动处理验证码，也不绕过登录墙或平台风控。若 Instagram 要求登录，仍需使用你自己浏览器中的有效会话；这与填写“目标账号”是两回事。

GitHub 候选项目和采用理由见 [docs/research.md](docs/research.md)。

## 安装

需要 Python 3.10 或更高版本。在 PowerShell 中执行：

```powershell
cd C:\path\to\instagram-post-scraper
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## 推荐：本地网页界面

Windows 用户请先右键 ZIP 选择“全部解压”，再双击解压目录中的 `start-ui.cmd`（也提供中文别名 `启动界面.cmd`）。不要在 ZIP 预览窗口里直接运行。首次启动会在 `%LOCALAPPDATA%\InsPosts` 创建独立运行环境并联网安装依赖，不会把虚拟环境和运行结果混入项目源码目录。

页面默认进入“随机发现”。不需要输入账号、用户名或 URL，按需设置随机范围、结果数量和筛选条件，然后开始即可。每次运行都会重新抽取来源并打散结果；若想复现来源选择，可在命令行指定随机种子。

只有在你明确要采集某个公开账号、Hashtag 或帖子链接时，才切换到“指定目标（高级）”模式并填写目标。

也可以在已完成安装的终端中运行：

```powershell
ins-posts-ui
```

程序会在 `127.0.0.1` 的随机端口启动并自动打开浏览器。页面只在本机可访问，不上传 Cookie 或采集结果。双击启动器时，每次任务写入 `%LOCALAPPDATA%\InsPosts\data\ui-jobs\<任务ID>`；页面可以查看状态、命中结果和运行日志，并直接打开结果目录。手动执行 `ins-posts-ui` 时仍默认写入当前目录下的 `data/ui-jobs`，也可用 `--output-root` 指定位置。

## 项目结构

```text
instagram-post-scraper/
├─ src/ins_posts/
│  ├─ core/       # 随机发现、筛选和数据标准化规则
│  ├─ collector/  # gallery-dl 调度、下载与命令行入口
│  ├─ webui/      # 本地 HTTP 服务及 static 前端资源
│  └─ *.py        # 兼容旧导入路径的轻量入口
├─ tests/         # 与上述分层对应的自动化测试
├─ docs/          # 调研和设计说明
├─ README.md
└─ start-ui.cmd
```

如果双击后被 Windows 拦截，可右键 ZIP 或启动器，打开“属性”，勾选“解除锁定”后重新解压。新版启动器遇到未解压、缺少 Python、安装失败等情况时会保留窗口并显示具体原因。

随机模式中的“希望得到多少条”是筛选后的上限，不是命中保证。若抽到的近期候选较少、筛选较严，最终结果可能少于该数量。“随机范围”越大，程序抽取的公共主题来源越多，耗时和请求量也会相应增加。

指定目标模式中的“每类最多扫描”是候选数量，同样不是保证命中的数量。例如扫描 50 条后命中 6 条，只表示最近这 50 条候选里有 6 条满足条件。

Instagram 通常要求有效登录会话。先在自己的浏览器中正常登录，再在页面选择该浏览器；Windows 上优先使用 Firefox。工具只请求读取 Instagram 域名下的 Cookie，不接收或保存账号密码。

## 筛选条件

支持以下结构化条件；不同维度之间同时满足才会命中：

- 发布日期起止，两端都包含；当前按帖子 UTC 日期判断；
- 最低和最高点赞数，两端都包含；
- 标题/正文关键词，支持“任一”或“全部”，忽略大小写并做 Unicode 归一化；
- Hashtag，支持“任一”或“全部”，`#` 可省略，按完整标签忽略大小写匹配；
- 媒体类型：全部、包含图片、包含视频；混合轮播可以同时命中图片和视频；
- 筛选后最多保留的结果数；随机模式会先筛选再打散并截断，指定目标模式会优先保留发布时间较新的结果。

关键词和 Hashtag 在命令行中使用英文逗号分隔，按普通文本处理，不接受正则表达式或 Python 表达式。

点赞数有一个上游限制：Instagram 隐藏或没有返回点赞数时，当前 `gallery-dl` 会把它和真实的 0 赞都表示为 `0`。启用点赞筛选时，本工具会保守地把 `0` 当作未知并排除，避免隐藏点赞数被错误纳入最高点赞条件。

## 命令行用法

不提供目标时，命令行默认执行随机发现。下面从内置主题池随机抽取来源，最多保留 20 条结果：

```powershell
ins-posts `
  --cookies-from-browser firefox `
  --max-results 20 `
  --output .\data\random
```

也可以显式写出 `--random`，设置抽取的主题来源数量，并用随机种子复现同一组来源；候选集合相同时，本地打散顺序也会一致：

```powershell
ins-posts --random `
  --cookies-from-browser firefox `
  --random-sources 6 `
  --random-seed 20260831 `
  --max-posts 25 `
  --max-results 30 `
  --output .\data\random-seeded
```

随机模式下，`--random-sources` 控制本次抽取的 Hashtag 来源数量，`--max-posts` 控制每个来源最多读取多少条近期候选。多个来源去重后才会执行筛选、打散和 `--max-results` 截断。程序会把本次使用的随机种子和来源记录到 `run.json`。

以下是指定目标的高级用法。采集某个账号最近候选中的 2026 年帖子，要求至少 1000 赞，文案含“航天”或“火箭”：

```powershell
ins-posts nasa `
  --cookies-from-browser firefox `
  --include posts,reels `
  --max-posts 100 `
  --since 2026-01-01 `
  --min-likes 1000 `
  --keywords "航天,火箭" `
  --keyword-mode any `
  --max-results 20 `
  --output .\data\nasa
```

指定某个账号，筛选同时带 `travel` 和 `上海` 标签、且包含视频的帖子，并只下载命中媒体：

```powershell
ins-posts some_account `
  --cookies-from-browser firefox `
  --include posts,reels `
  --max-posts 50 `
  --hashtags "travel,上海" `
  --hashtag-mode all `
  --media-type video `
  --download-media
```

采集指定帖子链接：

```powershell
ins-posts "https://www.instagram.com/p/SHORTCODE/" `
  --cookies-from-browser firefox `
  --download-media
```

随机模式和指定目标模式都可以显式提供自己导出的 Netscape 格式 Cookie 文件：

```powershell
ins-posts --random --cookies .\private\instagram-cookies.txt --max-results 20
```

查看完整参数：

```powershell
ins-posts --help
```

不要把 Cookie 文件提交到 Git、发给他人或写进脚本。工具关闭了上游 Cookie 回写，不会修改提供的 Cookie 文件。Chrome/Edge 的 App-Bound Cookie 可能无法由当前依赖解密；优先使用 Firefox，浏览器数据库被占用时可关闭对应浏览器后重试。

## 输出

每个输出目录包含：

- `extracted.jsonl`：本次扫描到的全部候选，筛选前；
- `current.jsonl`：本次筛选命中的结果，最适合页面展示或后续处理；
- `current.csv`：本次筛选结果的表格版，带 UTF-8 BOM，可在 Windows 上双击查看“标题”和“正文”列；
- `posts.jsonl`：该目录下历次命中结果的增量库，按帖子 ID 更新并保留历史；
- `posts.csv`：历次命中结果的表格版，同样适合 Excel、WPS 或记事本直接查看；
- `run.json`：本次模式、筛选配置、扫描/命中数量、认证方式和错误摘要，不含 Cookie；随机模式还记录抽样策略、种子和实际使用的来源；
- `media/`：使用 `--download-media` 时生成；始终只下载本次 `current.jsonl` 中帖子的全部媒体；
- `media-archive.txt`：已下载媒体 ID，用于避免重复下载；
- `gallery-dl-cache.sqlite3`：当前输出目录专用的上游缓存；
- `raw-events.json`：仅在使用 `--keep-raw` 时生成，用于排错。

如果只是人工查看，请优先打开 `current.csv`；它使用带 BOM 的 UTF-8 编码，可避免部分 Windows 软件把中文识别为乱码。`jsonl` 文件保留标准 UTF-8 编码，供程序读取。若在同一个命令行输出目录更换筛选条件，`posts.jsonl` 和 `posts.csv` 仍会保留此前条件命中的历史；本次精确结果请读取 `current.jsonl` 或 `current.csv`。网页界面每次使用独立任务目录，不会混淆条件。

`current.jsonl` 每行的核心结构：

```json
{
  "schema_version": 2,
  "platform": "instagram",
  "post_id": "...",
  "shortcode": "...",
  "post_url": "https://www.instagram.com/p/.../",
  "username": "...",
  "published_at": "2026-08-30T12:34:56Z",
  "title": "从上游标题或正文首句生成的标题",
  "body": "帖子完整正文，保留换行",
  "caption": "...",
  "accessibility_text": "上游返回的图片辅助说明或 null",
  "hashtags": ["#example"],
  "source_tags": ["photography"],
  "like_count": 123,
  "comment_count": 12,
  "view_count": 345,
  "play_count": 456,
  "media": [
    {
      "media_id": "...",
      "index": 1,
      "media_type": "image",
      "display_url": "https://...",
      "video_url": null
    }
  ]
}
```

Instagram 通常没有独立标题字段：上游若返回 `title`/`headline` 就直接使用，否则程序会从正文第一行的首句生成最多 120 字的 `title`。`body` 保存完整正文，`caption` 与正文保持一致以兼容旧版读取程序。评论、浏览和播放计数仅在 Instagram 实际返回时有值，否则为 `null`。

媒体 CDN URL 通常会过期；需要长期保存时，请在取得授权的前提下使用 `--download-media`。

## 重要限制

- 随机发现是“内置公共 Hashtag 主题池抽样”，不是 Instagram 全站均匀随机。结果会受到主题池、Hashtag 近期内容、登录状态、地区和平台排序的共同影响，不能据此推断整个平台的统计分布。
- “公开可见”不自动等于允许批量采集或再利用。请确认账号授权、Instagram/Meta 条款、隐私和版权要求。
- 对自有或已授权的 Business/Creator 账号，生产环境优先使用官方 Instagram API；这个项目定位为受控的小规模采集或 PoC。
- 筛选只作用于本次取得的近期候选。随机模式下，`--max-posts` 是每个随机来源的候选上限；指定目标模式下，它是每种目标内容类型的候选上限。两者都不代表 Instagram 全量历史中不存在更多命中项。
- 为避免 Windows 命令行过长，媒体阶段单次最多精确选择 500 个帖子；更大的任务请设置 `--max-results` 分批运行。
- 匿名请求常被重定向到登录页。工具不会自动化密码登录，也不会自动解决 challenge/CAPTCHA。
- 默认请求间隔为随机 6–12 秒；随机来源也会串行处理。不要为了提速而缩短间隔或并发运行多个任务，以免触发 429、登录挑战或账号限制。
- 随机模式遇到单个普通来源失败时会保留其他已取得的候选；若出现登录、429、checkpoint 或 challenge 等风控错误，则停止继续请求。指定目标的 `posts,reels` 按类型顺序处理；部分结果会保守合并，不会清空旧的完整字段或媒体。
- `gallery-dl` 使用 GPL-2.0；本包装代码为 MIT。分发成品或嵌入闭源产品前请评估依赖许可证义务。
- Instagram 适配可能随平台变化失效。优先升级锁定依赖并回归测试，不要复制上游 extractor 源码长期分叉。

## 测试

```powershell
pytest
python -m ins_posts --help
```

单元测试不访问 Instagram。实时采集通常需要你在自己的浏览器中保持有效登录会话；界面和命令行都不会要求你把账号密码交给本工具。
