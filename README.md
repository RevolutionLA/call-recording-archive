# 通话录音档案库（FunASR + Qwen3-ASR + 本地 LLM）

手机自动通话录音（`+86 1xx xxxx xxxx_yyyyMMddHHmmss.m4a`）→ 转写、区分「我/对方」、
联系人声纹归档、本地 LLM 摘要、音色文件导出（Qwen3-TTS 克隆）、关系图谱与驾驶舱。

## 环境（零下载复用本机已有资源）

| 用途 | 环境 | 说明 |
|---|---|---|
| 主流程（FunASR/SenseVoice/CAM++、Web、摘要调度） | `C:/ProgramData/anaconda3/envs/sensevoice/python.exe` | py3.8 + torch2.0.1+cu118 + funasr 1.1.9 |
| Qwen3-ASR-1.7B + Qwen3-ForcedAligner 精修 | `C:/ProgramData/anaconda3/envs/vllm/python.exe` | 由 `src/qwen_bridge.py` 以子进程+JSONL 桥接调用 |
| LLM 摘要 | Ollama `http://localhost:11434`（模型 qwen3.5:2b，深度分析 27B） | 需 `OLLAMA_MODELS=E:\AI\11Model` |

模型全部走 ModelScope 国内缓存（`~/.cache/modelscope/hub`），无需翻墙。
Web 前端 ECharts/Cytoscape 已本地化到 `web/vendor/`，无外网依赖。

## 使用

```bash
PY=C:/ProgramData/anaconda3/envs/sensevoice/python.exe

$PY pipeline.py scan                 # 扫描根目录录音入库（可重复执行，增量）
$PY pipeline.py run                  # 转写+VAD+SenseVoice+声纹聚类（断点续跑，可过夜）
$PY pipeline.py refine               # Qwen3-ASR 精修文本 + 字级时间戳（vllm 环境子进程）
$PY pipeline.py align                # 自动识别「我」+ 给所有段打 me/other/联系人标签
$PY pipeline.py summarize            # 本地 LLM 摘要/待办/事件/情绪/重要度
$PY pipeline.py voices               # 导出各联系人 6~18s 干净音色片段 -> data/voices/
$PY pipeline.py graph                # 构建关系图谱与事件时间线
$PY pipeline.py web                  # 驾驶舱 http://localhost:8760
$PY pipeline.py report               # 命令行进度
```

推荐全量顺序：`scan → run → refine → align → summarize → graph → voices`，
每步都可中断重跑（状态存在 `data/archive.db`，status: pending→transcribed→analyzed）。

## 关键设计

- **两方分离**：每通电话强制 k=2 层次聚类（CAM++ 192 维声纹，余弦距离）；
  两簇质心相似度 > `cluster_merge_sim` 判为单人。
- **「我」的识别**：每通电话必在场 → 跨通话双说话人质心贪心聚类，覆盖通话最多的簇=我；
  段级按与我声纹质心相似度判定 me/other（阈值 `me_threshold`）。
- **联系人**：文件名电话/姓名 hint 优先，其次声纹库匹配（`match_thr` 0.55），
  声纹库每人最多存 8 条参考。
- **精修桥接**：主环境 py3.8 无法 import qwen_asr，故 refine 把逐段切片任务写成
  `jobs.jsonl`，用 vllm 环境跑 `src/worker_qwen.py`，结果合并回
  `asr_outputs`/`segments`（text_zh、字级 start/end、align_source='qwen3-aligner'）。
- **摘要**：Ollama 原生 `/api/chat` + `think:false`（OpenAI 兼容端点对思考模型
  会把 token 耗在 reasoning 上导致 content 为空）。

## 配置

`config.yaml`：录音目录与排除项、模型 id、各阈值、LLM、代理开关
（默认 `proxy.enabled: false`，国内直连；下载海外资源时再开）。

## 目录

- `data/archive.db` — 全部结构化结果
- `data/work/` — 规范化 16k wav 中间产物
- `data/voices/<联系人>/` — ref_NN.wav + ref_NN.txt + meta.json（Qwen3-TTS 克隆直接可用）
