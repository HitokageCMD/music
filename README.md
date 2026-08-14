# tunebox

本地音乐播放器 / Self-hosted personal music player。跑在你自己的设备上，搜索走 YouTube Music、播放走本地缓存，能后台播放，带逐行滚动歌词。

跑在你自己的电脑上，手机浏览器连过来就能听——锁屏封面、耳机线控、蓝牙上下一首都能用。

还能**按歌词找歌**：记得一句词但想不起歌名，切到「歌词」模式输进去就行。

---

## 先读这一段

**这违反 YouTube 的服务条款。** ToS 明确禁止在没有下载按钮的情况下下载内容，也禁止绕过广告，而广告和 Premium 是创作者的收入来源。自用不分发，现实风险基本为零，但你应该知道自己在做什么。

**它会时不时坏掉。** YouTube 隔三差五改签名算法、加 PO Token。坏了先跑：

```powershell
uv sync --upgrade-package yt-dlp
```

九成的问题这一条就解决了。

**别部署到云上。** Vercel、Railway、Render、AWS 全都不行——YouTube 封数据中心 IP，从云主机跑 yt-dlp 会直接撞上 "Sign in to confirm you're not a bot"。Invidious 和 Piped 的公共实例成批死亡就是这个原因。这东西必须跑在住宅 IP 上。想从外面访问，往下看 Cloudflare Tunnel 那节。

---

## 跑起来

需要 Python 3.13+ 和 [uv](https://docs.astral.sh/uv/)。

```powershell
uv sync
.\run.ps1
```

打开 http://localhost:8730 。手机连同一个 WiFi 的话，用脚本打印出来的那个局域网地址。

冒烟测试（服务起来之后跑）：

```powershell
uv run python smoke.py
```

---

## 它是怎么工作的

```
搜索     ytmusicapi  ──>  歌名/歌手/专辑/方形封面
播放     yt-dlp      ──>  音频直链  ──>  边下边播 + 落盘
歌词     LRCLIB ──> YouTube Music（兜底）
搜歌词   本地曲库 ──> QQ 音乐 ──> 桥接回 YouTube 才能播
推荐     你听过/收藏的歌当种子 → 每首生成电台 → 共现排序
界面     <audio> + MediaSession API
```

**场景快搜:** 搜索栏下一排标签(派对/开车/打扫/洗澡/emo/专注…),点一下出对应氛围的一列歌,自动电台接着往下放。关键——**场景是心情不是直译**:「洗澡」直接搜是儿童洗澡歌,所以映射成 feel-good 跟唱金曲;「打扫」映射成 upbeat pop 而不是电梯背景乐。映射表在 `web/app.js` 的 `SCENES`,一行一个,想改想加随手改。

**下拉/锁屏控制:**
- **PWA**:MediaSession(`web/app.js`)——播放/暂停/上一首/下一首/进度,直接出现在通知栏和锁屏。
- **APK**:WebView 的 MediaSession 不出控制,所以用**原生 MediaSession + MediaStyle 通知**(`PlaybackService.java`)。JS 通过 `window.tunebox.updateMedia` 把状态推给安卓,安卓的按钮回调再 `evaluateJavascript` 驱动 WebView 的 `<audio>`(`window.__tuneboxNext/Prev/Seek`)。前台服务保活 + 媒体通知一体。

**自建歌单:** 每首歌右边「＋」→ 选/新建歌单加进去(`/api/playlist` 增删、`/api/playlist/{id}/add`、`.../track/{id}` 移除)。导入的合集和自建歌单都在「音乐库 › 歌单」。删除已下载:「音乐库 › 已下载」里点每首的 ✓。

**移动端:** 场景/发现的标签在手机上**换行**而不是横向滚动(否则一直「要左右拉」);循环键(单曲/列表)在手机上也显示。

**推荐是照你的口味长出来的，不靠账号。** 种子取自你真实听过/收藏的歌(`db.seed_tracks`：liked → 播放次数 → 最近)，给每首拉一个 YouTube Music 电台，然后按**共现**排序——一首歌同时出现在你好几个种子的电台里，就是更强的推荐(`app/recommend.py`)。已经在库里/已收藏/当种子的都排除掉，只推新的。冷启动(没有任何历史)就空着，提示你先听几首。

**为什么不用 YouTube 官方的 IFrame Player？** 因为它做不到你要的两件事：广告是播放器自己插的，屏蔽不了；手机上一切后台或锁屏，iframe 播放器立刻暂停——后台播放是 Premium 的卖点，Google 是故意封的。绕开整个 YouTube 播放器、直接喂给浏览器原生的 `<audio>`，这两个问题就都不存在了。

**「边下边播」是怎么做的。** 后台任务把整首歌顺序写进 `<id>.part`，播放请求从这个还在长大的文件里读；读到写入位置就挂起在一个 event 上，有新字节了再继续吐。一次下载喂所有听众，第一次点开 1-2 秒出声，听完文件也留下了。见 `app/cache.py`。

**几个踩过的坑**（都写在代码注释里了，改之前先看）：

| 坑 | 后果 |
|---|---|
| `Range: bytes=0-` 必须带 | 不带的话 googlevideo 限速到 31 KB/s，带上 12 MB/s。**328 倍** |
| `.part` → `.m4a` 要引用计数 | Windows 不让重命名正被打开的文件（POSIX 可以） |
| 必须转发 yt-dlp 的 `http_headers` | 否则 googlevideo 直接 403 |
| Service Worker 不能碰 `/api/` | 拦截 Range 请求会让拖进度条失效甚至卡死 |
| 封面只信 ytmusicapi | yt-dlp 给的 42 张缩略图全是 16:9 视频截图，一张方的都没有 |
| 优先选 m4a 而不是更高码率的 opus | Safari 和 iOS 不能解 opus，而这东西要在手机上用 |
| 歌词同步不能只靠 rAF | 页面一隐藏 rAF 就是 0 帧/秒，得用 `timeupdate` 兜底 |
| QQ 的 `<em>` 标签不能信 | 它标的是「某个词」命中，一句乱码也能让 5 条结果全带 `<em>` |
| **SW 必须把 5xx 当成失败** | 代理后面的死后端返回 502，`fetch()` 是**成功**的，`.catch()` 不触发 → 缓存永远用不上 |
| 代码资源别设长 max-age | `app.js` 设了 `max-age=3600` 会让改动传不到浏览器——连 SW 的 network-first 都被 HTTP 缓存喂旧文件。代码用 `no-cache`，图片才 immutable |
| `hidden` 属性会被 `display` 干掉 | 作者样式优先级高于浏览器默认样式，得显式写 `[hidden] { display: none }` |
| 离线音频用 IndexedDB 不用 Cache API | blob URL 天生支持拖进度条；Cache API 得让 SW 自己合成 206 |

**离线那条最阴险，而且只在代理后面出现。** 服务器直接死在 localhost 上时，`fetch()` 抛连接错误 → `.catch()` 触发 → 缓存生效，一切正常。但隔着 Tailscale / Cloudflare / nginx，死掉的后端会变成一个**语法上完全成功的 502 响应**，`.catch()` 根本不会跑，SW 把 502 原样递给浏览器 → 白屏，尽管整个外壳就躺在缓存里。而代理正是手机的连接方式，所以这个 bug 只在真正要用的场景下发作。

**元数据的三个写入路径**，优先级从高到低：搜索结果（ytmusicapi，最好）→ 客户端播放时 POST（同一份数据）→ yt-dlp 解析（最差，只在前两个都没有时兜底）。曲库里删歌只清音频文件、不删元数据行——否则重播时会用 yt-dlp 那份烂数据重建。

**按歌词搜歌为什么绕这么远。** LRCLIB 不支持歌词全文搜索（试过，0 命中）；网易云对海外 IP 返回 AES 加密结果（`abroad: true`）。所以用 QQ 音乐的 `t=7` 歌词索引。但 QQ 只告诉你「是哪首歌」，播不了，得再拿歌名+歌手回 YouTube Music 搜一遍——而这一步天真地取第一条会**静默返回错的歌**：翻唱和 Live 版都会被匹配成录音室原版。`app/lyrics.py` 里用了两道独立校验（歌名前缀匹配 + 时长差 ≤12 秒）把它们挡掉，实测正确的匹配时长差约 1 秒，错的是 16 秒和 51 秒。

歌名用**前缀**匹配而不是子串——因为真实的变体都是加后缀（`(Live)`、`- From THE FIRST TAKE`），而巧合都在中间：`在人间` 恰好是 `我在人间贩卖黄昏` 的一段，子串判断会把两首完全不同的歌认成同一首。

---

## 装成手机 App

打开 HTTPS 地址 → 「添加到主屏幕」。出来的是独立窗口、有图标、没有地址栏。

**下过的歌电脑关机也能听。** 每首歌右边的 `⤓` 存到本机（音频 + 封面 + 歌词一起），「本机」那一栏就是你这台设备上的曲库。断网时 app 会自动跳过去。实测把服务器杀掉之后：页面照常加载、blob 播放、拖进度条、逐行歌词、封面全部正常。

**HTTPS 不是必需的，但它决定你能得到多少。** 实测（`http://192.168.0.135:8730`，非安全上下文）：

| | 纯 HTTP 局域网 | HTTPS |
|---|---|---|
| 听歌、后台播放 | 能 | 能 |
| **MediaSession**（锁屏封面、耳机线控） | **能** | 能 |
| Service Worker / PWA 安装 / **离线下载** | **不能**（API 直接不存在） | 能 |
| 出门在外能听 | 不能 | 能 |

MediaSession 不要求安全上下文，所以**局域网 HTTP 下锁屏控制照样能用**。只有 PWA 和离线那套需要 HTTPS。

图标改了就跑 `uv run --with pillow python tools/make_icons.py` 重新生成 PNG。**必须是 PNG**——iOS 会直接忽略 SVG 的 apple-touch-icon，然后把网页截图糊到你主屏上。

### 方案 A：Tailscale Serve（本机已配好）

私有、免费、真证书、不用碰防火墙、不用域名、出门用流量也能听。

```powershell
tailscale serve --bg --https=8443 8730
```

本机跑的就是这条，地址：**https://test.tail43e49a.ts.net:8443**

手机装 Tailscale、登录同一账号，浏览器打开上面这个地址即可。实测 Range 请求、边下边播、拖进度条都能正常穿过这个代理（冷启动出声 1.6 秒，和本机直连一样）。

**`serve` 不是 `funnel`。** `serve` 只有你自己 Tailnet 里的设备能访问；`funnel` 是暴露到整个公网。别把音乐库挂 Funnel 上——那等于给全世界开了个免费的 yt-dlp 代理，还烧你的带宽。

这台机器上 443 已经有一个 Funnel 指向 `localhost:5000`（dashboard），是公网可达的。tunebox 特意用 8443 + serve，两者互不干扰。

```powershell
tailscale serve status              # 看当前配置
tailscale serve --https=8443 off    # 撤销 tunebox 这条
```

注意 serve 配置会持久化，但 tunebox 本身不会自启——`run.ps1` 没跑的话代理会 502。

### 方案 B：局域网

`http://<你的局域网IP>:8730`，只在家里能用，手机不用装东西。需要放行防火墙（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName "tunebox" -Direction Inbound -Protocol TCP -LocalPort 8730 -Action Allow -Profile Any
```

如果连不上，先确认你的网络类别不是「公用」（`Get-NetConnectionProfile`），公用配置文件严格得多。

### 方案 C：Cloudflare Tunnel

要一个正经公网域名才用这个。**注意它是公网可达的**，任何人拿到网址都能听你的曲库、消耗你的带宽。真要用就套一层 Cloudflare Access，别自己写登录。

## 独立 Android App（不用电脑）

`android/` 是一个 Chaquopy 工程:把整套纯 Python 后端 + Python 3.12 运行时打进 APK,用 WebView 加载 `127.0.0.1` 上本地起的服务器。手机自己跑 yt-dlp,彻底不需要电脑。**这是纯 Python 重构的意义**——pydantic 的 Rust 核心进不了 APK,换成 stdlib `http.server` 后依赖闭包全是纯 Python,`pip install` 干净打包。

构建(机器上要有 Android SDK + JDK 17):

```bash
cd android
# 用缓存的 gradle，或 Android Studio 打开
gradle :app:assembleDebug
# 产物: app/build/outputs/apk/debug/app-debug.apk
```

关键点(踩过的坑,都写在 `android/app/build.gradle` 注释里):

| 坑 | 解 |
|---|---|
| pydantic Rust 核心打不进 APK | 后端改 stdlib，运行时零编译依赖 |
| 只装了 android-36，Chaquopy 的 AGP 配不上 | 补装 android-34 + build-tools 34 |
| Gradle 8.14 要求显式任务依赖 | `merge*PythonSources` dependsOn 我的 stagePython |
| Kotlin stdlib-jdk7/8 版本冲突 | kotlin-bom 1.8.22 对齐 |
| WebView 后台播放会被系统挂起 | 一个前台 Service 保活进程 |
| WebView 要开 DomStorage | 否则 localStorage / IndexedDB(离线下载)不可用 |

**版本组合**(实测能构建):Chaquopy 16.1.0 · AGP 8.7.2 · compileSdk 34 · Gradle 8.14.3 · 只打 arm64-v8a。

App 代码和 web 资源不复制,由 gradle 的 `stagePython`/`stageWeb` 任务从上一层的 `app/`、`web/` 现场取——单一真相源。

**已知未验证:** yt-dlp 在真机 CPU 上的表现、WebView 里 MediaSession 的锁屏控制,要装到真机才知道。

## 界面

单色为主,靠明暗分层级而不是靠颜色,没有 emoji。三个标签(发现为默认落地页):

- **发现** — 顶部分类可切:`为你推荐`(个性化,10 分钟缓存) · `华语新歌` · `欧美新歌` · `粤语` · `日语` · `韩语` · `抖音`。分类按语言偏好排前;新歌类年份用 `new Date()` 动态取。冷启动没听歌记录时,个性化空但分类立刻有内容
- **搜索** — 歌名/歌词两种模式,下面一排场景标签(空态可见)
- **音乐库** — 子筛选 `全部 · 收藏 · 已下载 · 歌单`。「已下载」是本机 IndexedDB,离线可用,断网自动切到这里;「歌单」是导入的清单

## 设置 / 偏好

右上角「设置」,首次打开自动引导。存在 localStorage(`tunebox_prefs`),前端应用:

- **不想听(最强)** — `DJ/Remix · Live现场 · 翻唱 · 纯音乐/伴奏 · 儿童歌`,做成标题/歌手关键词**硬过滤**,套在搜索、场景、发现、自动电台所有结果上(`applyPrefs`)。让自动播放变干净的主力
- **语言偏好** — 单选一种时拼进场景查询;发现分类按它排前;**为你推荐按语言脚本倾斜**(`biasByLang`,带兜底不筛空)。局限:kpop/罗马字标题会被当成欧美,华语/日语(原生文字)准
- **喜欢的类型(软提示)** — 如 R&B,拼进查询做倾向,不保证
- **自动电台开关** — 从标签栏挪进了设置

## 导入合集(清单)

**顶部主搜索框直接粘链接就导入**(`isImportUrl` 检测 youtube/youtu.be/bilibili/b23.tv → 走导入而不是搜索,导完跳进清单)。或者「音乐库 › 歌单」里也有专门的导入框。粘 **YouTube 播放列表** 或 **B站合集/视频** 链接,导成一个命名清单——**不下载,按需流播**(单曲仍可点下载键存离线)。

原理:`ytdl.list_collection` 用 yt-dlp `extract_flat` 快速列条目 → 存进 `playlists`/`playlist_tracks` 表,曲目带 `source`(yt/bili)。播放时 `resolve` 按来源走对应网址,B站的音频流也一样能拉(它需要 Referer 头,而缓存层本来就转发 yt-dlp 给的头)。实测 B站导入→流播全通。

**b23.tv 短链**(分享出来的那种没有 BV 号的)会先跟着 302 跳转拿到真实 `bilibili.com/video/BV...` 再导入。

**B站分P合集**(一个 BV 下 95 集那种歌单)走 B站自己的 `view` API 列出全部分P——因为 yt-dlp 的 `extract_flat` 对 B站分P 返回的是空条目(id 全 None)。API 一次拿到全部,还把「序号. 歌手 - 歌名」拆成歌手/歌名。粘链接时带不带 `?p=95` 都行,会导入整个合集。

**已知限制:** 部分 B站内容对海外 IP 有**地域限制**(这台服务器在海外就会被挡,导入会提示);YouTube「合集」歌单里有时装的是几小时的串烧长视频而不是单曲——那取决于你导入什么链接。

## 自动更新（APK）

APK 通过 **GitHub Releases 自更新**——因为里面的 yt-dlp 是打包死的,YouTube 一改签名它就失效,自更新一次把 UI 和 yt-dlp 都刷新。`app/src/main/java/net/tunebox/Updater.java`:每天启动时(和设置里「检查更新」)查 GitHub 最新 Release,版本号(tag 里的整数)比本地 `versionCode` 新就下载 APK、拉起系统安装器。网页版不需要这个(刷新即最新),所以设置里那个按钮在 PWA 下是灰的。

**一次性配置:**

1. 建一个 GitHub 仓库,把 `Updater.GITHUB_REPO` 改成 `"你的用户名/仓库名"`
2. `winget install GitHub.cli` 然后 `gh auth login`
3. 以后每次发版:`tools\release.ps1` —— 自动 bump versionCode、构建、打 tag(=versionCode)、上传 APK 到 Release。手机一天内自动提示更新
4. **首个自更新版**要手动装一次(最后一次手动),之后就自动了

Release 的 tag 必须是版本号整数(脚本用 `v{versionCode}`,App 解析里面的数字比对)。

## 快捷键

`空格` 播放/暂停 · `←` `→` 快退/快进 5 秒 · `Shift+←` `Shift+→` 上/下一首 · `L` 开关歌词 · `Esc` 收起歌词 · `/` 跳到搜索框

歌词页里点任意一句可以跳到那一句。手动滚动后自动跟随会暂停 4 秒，免得跟你抢。

## 结构

```
app/
  main.py     FastAPI 路由
  cache.py    边下边播引擎（最核心，也最容易改坏）
  ytdl.py     yt-dlp + ytmusicapi 封装
  lyrics.py   歌词抓取、LRC 解析、按歌词搜歌
  db.py       SQLite
  config.py   路径
web/
  app.js      前端（原生 JS，无构建步骤）
  idb.js      设备本地存储（IndexedDB blob）
  sw.js       Service Worker（应用外壳缓存，绕开 /api/）
tools/
  make_icons.py   生成 PNG 图标
data/         音频、封面、SQLite（.gitignore 掉了）
smoke.py      端到端测试（40 项）
```

服务器状态在 `data/`，设备状态在 IndexedDB（**按源隔离**——换个地址访问，之前下载的歌不会跟过去）。
