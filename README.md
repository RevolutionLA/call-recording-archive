<div align="center">

# 📞 CallRec · 通话录音档案库

**把散落的手机通话录音，变成一台能全文检索、按人按时间统计、看关系图谱、还能导出音色克隆的本地话务台。**

[![license: MIT](https://img.shields.io/badge/license-MIT-63b3a5?style=flat-square)](LICENSE)
![全部本地](https://img.shields.io/badge/全部本地-e8a33d?style=flat-square)
![零上云](https://img.shields.io/badge/零上云-63b3a5?style=flat-square)
[![engine: FunASR](https://img.shields.io/badge/engine-FunASR%20%C2%B7%20SenseVoice-7f8ea3?style=flat-square)](https://github.com/modelscope/FunASR)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-63b3a5?style=flat-square)

**100% offline — your calls never leave your machine.**

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
- [截图](#截图)
- [致谢](#致谢)
- [引用](#引用)
- [许可与免责](#许可与免责)
- [联系与贡献](#联系与贡献)

## 为什么做这个

手机会自动录下大量通话（`+86 1xx xxxx xxxx_20240724231822.m4a`），但它们只是躺在文件夹里的音频——不能搜、不能统计、不能回忆「上次和谁说了什么」。

现有开源工具要么面向**视频字幕/播客笔记**（FunClip、SmartSub、AudioNotes），要么只做**转写+摘要**这一步。没有一个把「通话」当专属场景做完整闭环。

**CallRec 补上这一段**：转写 → 区分「我/对方」→ 声纹归并联系人 → 本地大模型摘要 → 关系图谱 → 音色导出。全程离线，模型和 LLM 都在你自己的机器上。

## 它能做什么

- **转写 + 说话人分离**：FunASR（fsmn-vad → SenseVoice → ct-punc）逐段转写；CAM++ 声纹 + 层次聚类，把每通话分成「我」和「对方」。
- **跨通话认识你**：用「每通电话你都必然在场」这一先验，跨通话贪心聚类自动锁定「我」的声纹，其余按号码/声纹归并成**联系人档案**。
- **本地大模型摘要**：Ollama 离线跑，产出摘要、待办、事件、情绪、重要度，绝不联网。
- **全文检索**：中文/英文全文入库，一句「说过的话」就能定位到具体通话、具体秒。
- **深夜话务台驾驶舱**：通话长河、星期×小时热力图、联系人排行、时长分布——零外网依赖，字体图表全本地。
- **关系图谱**：自绘 Canvas 力导向图，我=中央信号源，跳线粗细=话务量，悬停点亮邻域。
- **音色导出**：每人 6–18 秒干净人声片段 + 对应文本，直接喂给 Qwen3-TTS 做声音克隆。
- **外部录音库 / NAS**：Web 上增删改、扫描入库、按库统计，支持数万通规模。

## 快速开始

需要一台带 NVIDIA GPU 的机器（显存 ≥ 4GB），已装 conda。

```bash
# 1) 复用/创建环境（SenseVoice 环境含 torch+cu118 + funasr 1.1.9）
pip install torch==2.0.1+cu118 funasr==1.1.9 fastapi uvicorn scipy numpy \
    -f https://download.pytorch.org/whl/cu118

# 2) 配置：复制样例后按本机改路径（模型走 ModelScope 国内缓存，无需翻墙）
cp config.example.yaml config.yaml

# 3) 把手机通话录音放进项目目录，然后：
python pipeline.py scan        # 扫描入库（增量，可反复）
python pipeline.py run         # 转写 + 声纹分离（断点续跑，可过夜）
python pipeline.py align       # 自动识别「我」+ 打标签 + 归并联系人
python pipeline.py summarize   # 本地 LLM 摘要（需 Ollama）
python pipeline.py graph       # 关系图谱与事件时间线
python pipeline.py web         # 打开驾驶舱 http://localhost:8760
```

想更准的字 + 字级时间戳？`python pipeline.py refine` 用 Qwen3-ASR + ForcedAligner 在已切分的语音段上「重听」一遍：中文专名、数字和方言口音（四川话/河南话）明显更稳，原文保留在 `segments.text_sv` 可回溯。它会用第二个 Python 环境的独立进程跑，内存不足时自动跳过本轮，也可排进自动摄取循环。

差别长这样（示意，非真实录音）：噪声+口音段 `那个方案我我这边呃看哈，时间大概念是下个有五左又` → `那个方案我这边看一下哈，时间大概是下个月五号左右`。代价也要说清楚：约 1–2 秒算力/秒音频，一张 6G 显存的老卡跑 85 小时音频要按天算，所以 `qwen.max_minutes` 会按轮让位给摘要，`align: false` 可跳过字级时间戳换速度。

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
summarize：Ollama /api/chat → 摘要/待办/事件/情绪
   ▼
graph + voices + web 驾驶舱（FastAPI + 自绘 Canvas / ECharts，全本地）
```

## 技术栈

| 层 | 选型 |
|---|---|
| ASR / VAD / 标点 / 声纹 | FunASR 1.1.9：SenseVoiceSmall、fsmn-vad、ct-punc、CAM++ |
| 精修（可选） | Qwen3-ASR-1.7B + Qwen3-ForcedAligner-0.6B |
| 摘要 LLM | Ollama（qwen3.5:2b 主力，思考模型用原生 `/api/chat`+`think:false`） |
| 存储 | SQLite（WAL 并发）；音频与数据永不入库到 git |
| Web | FastAPI + 单页前端，ECharts/字体本地 vendor，零 CDN |
| 图谱 | 自绘 Canvas 2D 力导向（无第三方图库） |

## 设计取向

- **复用本地优先**：模型只从 ModelScope 国内缓存加载，检测到已有就绝不重复下载。
- **隐私是默认项，不是选项**：没有任何一行数据出机器。
- **断点续跑**：每通话独立状态，一通失败不拖垮整批，可 kill 可重跑。

## Roadmap

- [x] 多 worker GPU 并行转写（`--workers N`，按显存/内存自行调节）
- [ ] 增量图谱与时间滑窗
- [ ] 声纹冲突人工校正 UI
- [ ] 打包为 pip 包 / 一键安装

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

Issues 和 PR 欢迎。改流水线请保持「断点续跑」和「数据不出机器」两条底线。

> 如果这个项目帮你找回了某通忘了说过什么的话，欢迎在 Discussions 里讲你的故事——匿名也行，这里本来就不联网。😊

---

<div align="center">
<sub>Made for people who'd rather keep their conversations at home. 🌙</sub>
</div>
