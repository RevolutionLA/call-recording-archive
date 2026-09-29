<div align="center">

# 📞 CallRec · 通话录音档案库

**把散落的手机通话录音，变成一台能全文检索、按人按时间统计、看关系图谱、还能导出音色克隆的本地话务台。**

[![license: MIT](https://img.shields.io/badge/license-MIT-63b3a5?style=flat-square)](LICENSE)
![全部本地](https://img.shields.io/badge/全部本地-e8a33d?style=flat-square)
![零上云](https://img.shields.io/badge/零上云-63b3a5?style=flat-square)
[![engine: FunASR](https://img.shields.io/badge/engine-FunASR%20%C2%B7%20SenseVoice-7f8ea3?style=flat-square)](https://github.com/modelscope/FunASR)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-63b3a5?style=flat-square)
[![locks](https://github.com/RevolutionLA/call-recording-archive/actions/workflows/locks.yml/badge.svg)](https://github.com/RevolutionLA/call-recording-archive/actions/workflows/locks.yml)

**Local-first — nothing leaves your machine once the models are prepared and the summarization endpoint is local (the default).**

觉得有用？点个 ⭐ 是对作者最直接的鼓励。

</div>

---

## 目录

- [为什么做这个](#为什么做这个)
- [它能做什么](#它能做什么)
- [快速开始](#快速开始)
- [架构](#架构)
- [技术栈](#技术栈)
- [设计取向](#设计取向)
- [Roadmap](#roadmap)
- [已知限制](#已知限制)
- [截图](#截图)
- [致谢](#致谢)
- [引用](#引用)
- [许可与免责](#许可与免责)
- [联系与贡献](#联系与贡献)

## 为什么做这个

手机会自动录下大量通话（`+86 1xx xxxx xxxx_20240101120000.m4a`，占位示例），但它们只是躺在文件夹里的音频——不能搜、不能统计、不能回忆「上次和谁说了什么」。

现有开源工具要么面向**视频字幕/播客笔记**（FunClip、SmartSub、AudioNotes），要么只做**转写+摘要**这一步。没有一个把「通话」当专属场景做完整闭环。

**CallRec 补上这一段**：转写 → 区分「我/对方」→ 声纹归并联系人 → 本地大模型摘要 → 关系图谱 → 音色导出。模型和 LLM 都跑在你自己的机器上（边界见[设计取向](#设计取向)：首次加载模型会下载权重；摘要端点若配置为远程地址，对应转写文本会发送到该端点）。

## 它能做什么

- **转写 + 说话人分离**：FunASR（fsmn-vad → SenseVoice → ct-punc）逐段转写；CAM++ 声纹 + 层次聚类，把每通话分成「我」和「对方」。
- **跨通话认识你**：用「每通电话你都必然在场」这一先验，跨通话贪心聚类自动锁定「我」的声纹，其余按号码/声纹归并成**联系人档案**。
- **本地大模型摘要**：默认走本机 Ollama（`localhost`），产出摘要、待办、事件、情绪、重要度；端点配置为远程服务时，外发的仅是对应段的转写提示文本。
- **全文检索**：中文/英文全文入库，一句「说过的话」就能定位到具体通话、具体秒。
- **深夜话务台驾驶舱**：通话长河、星期×小时热力图、联系人排行、时长分布——零外网依赖，字体图表全本地。
- **关系图谱**：自绘 Canvas 力导向图，我=中央信号源，跳线粗细=话务量，悬停点亮邻域。
- **音色导出**：每人 6–18 秒干净人声片段 + 对应文本，直接喂给 Qwen3-TTS 做声音克隆。
- **外部录音库 / NAS**：Web 上增删改、扫描入库、按库统计，支持数万通规模。
- **查重（同一录音的几份备份）**：同一通电话被你拷到几个文件夹时，只留一份进转写/精修/摘要，其余标成副本并从排队里摘掉——省的是显卡时间。先用库里已有的「大小+时长+转写文本」找候选，再对候选抽读文件头尾各 64KB 核验内容；不整文件哈希、**不删你的文件**，正本可改、标记可解除。
- **企业专线分人**：一个号码后面换着好几个人接话（大厂总机、银行客服、快递站点），按对方声纹把同一总机拆成坐席子档案，名字优先取他们自报的工号/姓名；拆错了可在页面上并回总机。
- **全程鼠标操作**：双击启动 → 「工作台」点按钮，每步显示"还有几通没做"、可中途停止、实时日志跟随；不用记 `pipeline.py` 的任何子命令。

## 快速开始

### 方式一：双击 + 点按钮（推荐，不用记任何命令）

需要一台带 NVIDIA GPU 的机器（显存 ≥ 4GB），已装 conda，并且已经按下面「装环境」跑过一次。

1. 把手机通话录音拷进项目文件夹（或在网页「录音库」里加一个 NAS 路径）；
2. 双击 **`双击启动 CallRec.bat`** —— 不弹黑窗口，浏览器自动打开驾驶舱；
3. 点顶部导航的 **「工作台」**，剩下的事全在那一页：

| 你想干什么 | 点哪里 |
|---|---|
| 新录音进库并全部处理完 | 「一键更新全部」→ 跑一轮 |
| 通宵无人值守，边录边收 | 「🌙 整夜挂机」（每轮之间等 5 分钟，点停止才停） |
| 只想干其中一步 | 该步骤卡片上的「开始」；卡片右侧数字就是**还有几通没做** |
| 有重复备份 / 一个号码好几个人接话 | 导航「查重与专线」页：候选组、判重依据、改正本、解除标记、把坐席并回总机 |
| 不想等了 / 要腾显卡给别人 | 「■ 停止」——进度按通话保存，下次接着跑 |
| 想知道它到底在干什么 | 页面底部「实时日志」，自动跟随正在跑的那一步 |

命令行时代的每一条 `pipeline.py xxx`，在这页上都有一个按钮，且带一句人话解释它值不值这个算力。
收工双击 **`停止 CallRec.bat`**（会先问你是否确认）。

> **端口被占用会自动让路**：默认 `8760`。Windows 的 Hyper-V/WSL 偶尔会开机保留一整段端口
> （本机就遇到过 8710–8809 全被占，`bind` 直接 10013）。启动器发现绑不上会自动换一个能用的端口
> （如 `18760`）并用新地址打开浏览器，不用改 `config.yaml`。

### 方式二：命令行（适合接入 cron / 远程 / 想看清每一步）

第一次用先装环境（SenseVoice 环境含 torch+cu118 + funasr 1.1.9）：

```bash
pip install torch==2.0.1+cu118 funasr==1.1.9 fastapi uvicorn scipy numpy \
    -f https://download.pytorch.org/whl/cu118
cp config.example.yaml config.yaml    # 按本机改路径（模型走 ModelScope 国内缓存，无需翻墙）
```

```bash
python pipeline.py scan        # 扫描入库（增量，可反复）
python pipeline.py report      # 看进度/统计
python pipeline.py dedup       # 查重：同一录音的几份备份只留一份进队列（不删文件，可解除）
python pipeline.py refresh     # 解析规则升级后回填 姓名/号码/时间（不重跑音频）
python pipeline.py run         # 转写 + 声纹分离（断点续跑，可过夜）
python pipeline.py refine      # Qwen3-ASR 精修文字 + 字级时间戳（可选，最贵的一步）
python pipeline.py align       # 自动识别「我」+ 打标签 + 归并联系人
python pipeline.py lines       # 专线分人：同一总机号码下按声纹拆坐席并自动起名（不重跑音频）
python pipeline.py summarize   # 本地 LLM 摘要（Ollama 或任意 OpenAI 兼容端点）
python pipeline.py graph       # 关系图谱与事件时间线
python pipeline.py voices      # 导出音色片段（供 Qwen3-TTS 克隆）
python pipeline.py web         # 打开驾驶舱 http://localhost:8760
```

网页的「工作台」就是把这些命令替你起了：它用你 `config.yaml` 里的 `python_env` 跑同一个 `pipeline.py`，日志落在 `logs/ui_<步骤>.log`，所以两边永远看到同一份数据、同一套锁。

想整夜无人值守：`scripts/auto_keepalive.bat` 会按 scan→run→refine→align→summarize→graph 循环续跑（日志 `logs/auto.log`），和页面上的「整夜挂机」同效，但**刻意不含 dedup 与 lines**——没人看着的半夜不该自动改写联系人档案，这两步留给你自己点。**二选一**，别同时开。每个阶段都有单实例锁，手动再开一个同名命令会被挡住并以 rc=76 退出。Linux/macOS 用 `fcntl.flock`，同样互斥（三种操作系统的锁行为由 GitHub Actions 用真实函数持续验证，见 `scripts/test_lock.py`；「工作台」的按钮表是否还和 `pipeline.py` 的子命令对得上，由 `scripts/test_jobs.py` 在同一份 CI 里守着）。

想更准的字 + 字级时间戳？`python pipeline.py refine` 用 Qwen3-ASR + ForcedAligner 在已切分的语音段上「重听」一遍：中文专名、数字和方言口音（四川话/河南话）明显更稳，原文保留在 `segments.text_sv` 可回溯。它会用第二个 Python 环境的独立进程跑，内存不足时自动跳过本轮，也可排进自动摄取循环。

差别长这样（示意，非真实录音）：噪声+口音段 `那个方案我我这边呃看哈，时间大概念是下个有五左又` → `那个方案我这边看一下哈，时间大概是下个月五号左右`。代价也要说清楚：约 1–2 秒算力/秒音频，一张 6G 显存的老卡跑 85 小时音频要按天算，所以 `qwen.max_minutes` 会按轮让位、`align: false` 可跳过字级时间戳换速度；想让摘要等精修全跑完再做，把 `llm.wait_for_refine` 设为 `true`。

文件名格式不用先改名：手机号、`010 6234 5678` 这类分组座机、95xxx/10086/400 特服热线、`2024-03-15 14:30`、`20240315_201530`、`2024年1月5日`、`REC_`/`CallRecording_`/`通话录音` 前缀、中文姓名与英文名都直接认（示例号码均为占位）；改规则后跑一次 `refresh` 回填历史行。

## 架构

```
录音 .m4a/.mp3
   │  scan：文件名解析(姓名/号码/时间) + 时长探测 → SQLite
   ▼
run：normalize 16k → fsmn-vad → SenseVoice 逐段 → ct-punc
      → CAM++ 声纹 → 层次聚类(强制 k=2) → segments
   ▼
refine(可选)：Qwen3-ASR + ForcedAligner 逐段重听 → 更准文字 + 字级时间戳
   ▼
align：跨通话质心聚类锁定「我」→ me/other/联系人标签（同人不同号自动并档）
   ▼
summarize：本地 LLM（Ollama 原生 / OpenAI 兼容自动识别）→ 摘要/待办/事件/情绪
   ▼
graph + voices + web 驾驶舱（FastAPI + 自绘 Canvas / ECharts，全本地）
```

## 技术栈

| 层 | 选型 |
|---|---|
| ASR / VAD / 标点 / 声纹 | FunASR 1.1.9：SenseVoiceSmall、fsmn-vad、ct-punc、CAM++ |
| 精修（可选） | Qwen3-ASR-1.7B + Qwen3-ForcedAligner-0.6B |
| 摘要 LLM | Ollama 或任意 OpenAI 兼容端点（LM Studio / vLLM）；思考模型走原生 `/api/chat`+`think:false` |
| 存储 | SQLite（WAL 并发）；音频与数据永不入库到 git |
| Web | FastAPI + 单页前端，ECharts/字体本地 vendor，零 CDN |
| 图谱 | 自绘 Canvas 2D 力导向（无第三方图库） |

## 设计取向

- **复用本地优先**：模型只从 ModelScope 国内缓存加载，检测到已有就绝不重复下载。
- **隐私是默认项，不是选项**：模型已准备完成、且摘要端点保持默认本机地址（`localhost`）时，没有任何一行数据出机器。两点边界如实说明：首次加载模型会从 ModelScope 下载权重文件；`llm.base_url` 配置为远程端点时，含转写内容的提示会发送到该端点——支持可配置端点是为了灵活，但请自己确认端点归属。
- **断点续跑**：每通话独立状态，一通失败不拖垮整批，可 kill 可重跑。

## Roadmap

- [x] 多 worker GPU 并行转写（`--workers N`）：**前提是显存和提交内存够**，建议 12G 以上显存的机器；单张 6G 卡请保持 `--workers 1`，实测两个 SenseVoice 实例会把页面文件撑爆并直接失败。早期记的"2 worker 提速 2.3 倍"测于锁逻辑变更之前，尚未在新代码上复测，因此这里不再给倍数。
- [ ] 增量图谱与时间滑窗
- [ ] 声纹冲突人工校正 UI
- [ ] 打包为 pip 包 / 一键安装

## 已知限制

- **Qwen3 精修的长段切分可能在接缝处丢字（已实测，见下）**。一段超过 `qwen3.refine.chunk_sec`（默认 20 秒）的录音会被切成若干块分别识别再拼回文本，块与块之间不留重叠，正好压在切点上的那个字有可能丢失。对库里已精修的 77 通（647 段、111 个跨块段、188 处接缝）做时间戳空洞审计：17.2% 的段会被切，接缝处出现 >600ms 空洞的概率约为随机字间隙的 3 倍——切点确实更容易留下空洞，但绝对量很小（13 处），且空洞本身可能只是自然停顿而非丢字。`python scripts/seam_audit.py` 随时可复算并列出可疑时间点供人工试听。跑批日志会打印"几批 / 几块 / 几处接缝"及每处接缝两端的字（含空块标记）。在人工确认丢字率之前维持无重叠切分；只要文本不要字级时间戳时可用 `--align 0`。
- **单张 6G 卡上转写、精修、摘要三者互斥**，靠 `data/lock_gpu.lock` 排队；被挡住的一方以退出码 76 跳过并在日志里留一行，守护脚本会隔一小段时间再试。同时开两个守护脚本属于设计内的互斥，不是故障。
- **摘要依赖本机 LLM 后端**。Ollama 走原生 `/api/chat`（思考模型必须带 `think:false`，否则返回空正文），LM Studio / vLLM / llama.cpp 走 `/v1/chat/completions`；后端没起来时 `base_url` 里有没有 `/v1` 会被用来猜类型，猜错就在日志里报空正文失败而不是静默写一条空摘要。
- **查重不信「文件大小 + 时长」**。m4a 是常量码率，同大小就等于同时长，两通不同的电话凑一起并不稀奇：本机 1965 通里能刮出 **112 组「同指纹」候选**，逐条抽读文件头尾核验内容之后**一组都不成立**——这批备份其实一个都不存在。所以「大小+时长」只用来找候选，判定必须有转写文本相似度或抽样内容指纹当证据；反过来说，如果你的备份是转码过的（容器、码率都变了），靠的是「同一分钟 + 时长相差 2 秒内 + 文件名相同」这组近似规则。
- **专线分人在你这套数据上是"预防性"的**。按号码能认出 52 通总机通话，但默认阈值下拆出 0 个坐席——实测那条大厂总机线的对面基本是同一个人。阈值放宽才会拆（13 组 / 28 个坐席）；命令行会打印每个簇的「自比」和整组的「簇间最像的一对」（这个数才是这一刀该不该切的依据），页面上的坐席卡片显示它是由哪条总机拆出、挂了几通、簇内自比多少，并带一键「并回总机」。
- **「工作台」的任务由 Web 服务进程托管**：双击启动器拉起的那个进程活着，任务才活着。关机、`停止 CallRec.bat`、或把那个 PID 杀掉，都会中断当前阶段（进度按通话保存，下次点继续跑）；想让它在没人管的情况下跑一整晚，用页面上的「整夜挂机」或 `scripts/auto_keepalive.bat`，别用系统的"休眠"。
- **驾驶舱端口可能被 Windows 保留**。实测本机 8710–8809 整段被 Hyper-V/WSL 保留时，`bind(8760)` 直接 WSAEACCES(10013)，uvicorn 起不来。启动器（`scripts/launcher.py`）会先探测可绑定端口再拉服务，绑不上就自动换端口并写入 `data/web_server.json`；手动跑 `pipeline.py web` 时可用环境变量 `CALLREC_PORT` 覆盖。

## 截图

> 深夜话务台驾驶舱 · 关系图谱 · 通话详情逐段高亮（`docs/screenshots/`）

## 致谢

- [FunASR](https://github.com/modelscope/FunASR) / [ModelScope](https://modelscope.cn)：SenseVoiceSmall、fsmn-vad、ct-punc、CAM++ 全链路模型。
- [Ollama](https://ollama.com)：让摘要这一步也不必离开本机。
- [Apache ECharts](https://echarts.apache.org) 与 [Space Grotesk](https://fontsource.org/fonts/space-grotesk)：驾驶舱的图表与数字排印（均随仓库本地分发，零 CDN）。

## 引用

在论文或项目中引用 CallRec：

```bibtex
@software{callrec2026,
  title  = {CallRec: An Fully Offline Archive for Phone Call Recordings},
  author = {RevolutionLA},
  year   = {2026},
  url    = {https://github.com/RevolutionLA/call-recording-archive},
  license = {MIT}
}
```

## 许可与免责

- 本项目以 **MIT** 协议开源（见 [LICENSE](LICENSE)）。
- 仅用于处理**你本人有权访问的通话录音**。请遵守你所在司法区关于通话录音与隐私的法律（多方同意地区务必先取得授权）。
- 本项目按「现状」提供，不构成任何法律建议。

## 联系与贡献

Issues 和 PR 欢迎。改流水线请保持「断点续跑」和「默认配置下数据不出机器」两条底线。

本项目经过一轮公开的三方代码评审（评审 → 复核 → 回复），真 bug、误判更正与防回归清单都整理在 [docs/review-summary.md](docs/review-summary.md)。

> 如果这个项目帮你找回了某通忘了说过什么的话，欢迎在 Discussions 里讲你的故事——匿名也行，这里本来就不联网。😊

---

<div align="center">
<sub>Made for people who'd rather keep their conversations at home. 🌙</sub>
</div>
