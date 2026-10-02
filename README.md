# 中国象棋 AI 分析助手（最小可运行版）

通过 Python 子进程调用 [Pikafish（皮卡鱼）](https://github.com/official-pikafish/Pikafish) 开源象棋引擎，
输入一个 FEN 棋面，返回引擎推荐的最佳走法、评分和候选变例。

- 技术栈：Python 3.10+（代码里用到了 `str | None` 类型标注）
- 引擎封装与命令行演示**纯标准库**，不依赖 `python-chess` 等任何第三方包；Web 界面额外需要 Flask
- 跨平台：Windows / macOS / Linux 均可运行

## 目录结构

```
xiangqi_ai/
├── engine/                # 引擎目录（可执行文件 + NNUE 权重放这里）
│   └── README.txt         # 引擎下载说明
├── data/
│   └── chess.db           # 棋局库（SQLite，首次运行自动建表；可在「我的棋局」里删掉重建）
├── templates/
│   ├── index.html         # 单页前端：CSS grid 棋盘 + 原生 JS，无框架
│   └── studies.html       # 我的棋局：列表 / 复盘 / 评语 / 导入导出
├── chess_engine.py        # ChessEngine 类：Pikafish 引擎的 UCI 封装
├── coord_utils.py         # 坐标与记谱工具：ICCS ↔ 棋盘行列、ICCS → 中文着法
├── explainer.py           # LLM 二次筛选：从引擎候选里挑一条并给理由（未配置/失败则降级为引擎首选）
├── rules.py               # 纯 Python 棋规：FEN 校验、合法着法生成、自将/照面检测、落子
├── storage.py             # 棋局存储层：sqlite3 建表 + 增删改查 + 事务（无 ORM）
├── pgn_io.py              # 棋谱导入导出：fen / fenmoves / iccs / pgn / json + 中文着法反查
├── games_routes.py        # 棋局接口蓝图：保存 / 续下 / 列表 / 复盘 / 评语 / 导出 / 导入
├── test_coord_utils.py    # coord_utils 的单元测试（python test_coord_utils.py）
├── test_rules.py          # 棋规的单元测试（含 perft(3) 校验）
├── test_storage.py        # 存储层的单元测试
├── test_pgn_io.py         # 棋谱导入导出的单元测试
├── test_games_routes.py   # 棋局接口的单元测试（用临时库，不动 data/chess.db）
├── demo.py                # 命令行演示：分析棋面 + ICCS 坐标转中文着法
├── app.py                 # Flask Web 界面：只做转发，页面上点击走子/输入 FEN/分析/AI 提示
├── requirements.txt       # Web 界面需要 Flask
└── README.md
```

## 一、准备引擎文件

引擎文件体积较大且需要手动解压，请自行下载放进 `engine/` 目录：

1. 打开 <https://github.com/official-pikafish/Pikafish/releases>，进入最新 release 的 **Assets**。
   当前最新为 `Pikafish-2026-09-06`，资产只有一个 `Pikafish.2026-09-06.7z`（约 51 MB）。
2. 下载该 `.7z` 并用 [7-Zip](https://www.7-zip.org/) 解压。
   新版是**通用二进制包**，里面按平台分目录（Windows / Linux / macOS / RISC-V 等），
   不再需要像旧版那样按 AVX2/AVX-512 挑文件。
3. 把对应平台的可执行文件复制到 `engine/` 目录，并重命名：

   | 系统          | 文件名          |
   | ------------- | --------------- |
   | Windows       | `pikafish.exe`  |
   | macOS / Linux | `pikafish`      |

   macOS / Linux 还需要加执行权限：`chmod +x engine/pikafish`
4. 把包里的权重文件 `pikafish.nnue` 也复制到 `engine/` 目录。

最终应该是：

```
engine/pikafish.exe      # Windows
engine/pikafish.nnue
```

> 引擎会先从自己所在目录（工作目录）自动加载 `pikafish.nnue`，
> 程序里的 `set_nnue()` 也会显式发一次 `setoption name EvalFile`，两者不冲突。

**不想放在 `engine/` 也可以**，用环境变量或命令行参数指定路径：

```bash
# 环境变量
set PIKAFISH_PATH=D:\engines\pikafish.exe
set PIKAFISH_NNUE=D:\engines\pikafish.nnue

# 或命令行参数
python demo.py --engine D:\engines\pikafish.exe --nnue D:\engines\pikafish.nnue
```

如果引擎文件不存在，程序不会崩溃，而是打印一段带下载链接的指引后退出。

## 二、运行

```bash
python demo.py                          # 分析标准开局（默认 FEN）
python demo.py "<自定义 FEN>"           # 分析指定棋面
python demo.py "<FEN>" --movetime 5000  # 引擎思考 5000 毫秒（默认 2000）
python demo.py --verbose                # 打印与引擎的原始 UCI 通信
```

示例（标准开局）：

```bash
python demo.py
```

输出格式示意如下（分数、深度、PV 会随引擎版本和思考时间变化，不代表固定结果）：

```text
============================================================
Pikafish 中国象棋分析结果
============================================================
棋面 FEN   : rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1
走子方     : 红方
引擎       : D:\Traeprojects\xiangqi_ai\engine\pikafish.exe
思考时间   : 2000 毫秒

最佳着法   : h2e2   [炮二平五]
评分       : +38 厘兵 (走子方视角, 正数表示走子方占优)
搜索深度   : 22

候选变例 (PV):
  ICCS: h2e2 b9c7 h0g2 ...
  中文: 炮二平五 马2进3 马二进三 ...
============================================================
```

## 三、Web 界面

```bash
pip install -r requirements.txt
python app.py
# 浏览器打开 http://127.0.0.1:5000
```

页面分两块：左边是 CSS grid 画的 9×10 棋盘（黑方在上），右边是 FEN 输入框、思考时间和「分析」按钮。

- **点击走子**：先点自己的棋子（蓝色高亮），再点目标格。走子后 FEN 自动同步到输入框、走子方自动切换。
  只允许基本合法的着法（蹩马腿、塞象眼、炮架、路径阻挡、九宫、相不过河、兵过河横走、
  不能吃自己的子、不能自将或将帅照面）。
- **悔棋**：棋盘下方的「↩ 悔棋」按钮回退一步（连该谁走棋一起回退），按钮上的提示会写明还能再悔几步。
  每走一步前都会压一份棋局快照，所以可以连续点、一路退到起始局面；退到底后按钮自动变灰，
  状态栏会显示「已悔棋，还可以再悔 N 步」。悔棋会把上一步的分析高亮、箭头和「AI 教练」卡片一起作废
  （勾了「自动提示」的话，会按当前局面重新提示一次）。
  **手动改动 FEN 文本框会清空悔棋栈**（手动输入不算“走棋”，历史局面已经接不上了），此时按钮变灰。
- **手动输入 FEN**：直接在文本框里改，边输边更新棋盘；FEN 还没写完整时输入框边框变红，但不打断输入。
- **思考时间**：默认「自动」，由后端按剩余子力数决定（子多 3 秒 / 10~20 子 5 秒 / 残局 8 秒）；
  切到「手动」才用输入框里的毫秒数。
- **分析**：点「分析」→ 前端 `POST /api/analyze` → 后端调用 `ChessEngine.analyze()` →
  结果面板显示最佳着法（ICCS + 中文）、评分、搜索深度和候选变例，
  同时在棋盘上把最佳走法的起点和终点高亮出来。
- **AI 提示下一步**：点「🤖 AI 提示下一步」→ 前端 `POST /api/ai_hint` →
  后端先用 MultiPV 拿回前几路候选，再（可选）让 LLM 从候选里做**二次筛选**，
  棋盘上用金色箭头画出最终推荐的那一步（起点黄框、终点绿框），右侧「AI 教练」卡片显示
  中文着法、评分（红优/黑优，`|cp| ≥ 200` 时高亮）、搜索深度、挑选理由和后续变化；
  卡片下方列出全部引擎候选（点击用蓝色虚线箭头预览，不会落子），并标出「引擎首选」「LLM 选中」；
  如果 LLM 挑的不是引擎首选，卡片里会多一行「引擎首选：xxx」方便对照。
  勾选「自动提示」后，每走完一步会自动请求一次（不阻塞界面，也不会自动落子）。
- **引擎参数**：面板最下方「引擎参数（Threads / Hash / MultiPV）」可以改线程数、哈希表大小、
  候选路数和默认思考时间，点「应用」立即生效（`POST /api/engine_config`，不用重启）。
- **换边**：棋盘下方的「⇅ 换边」按钮把棋盘转 180° 显示（黑方换到下方），
  方便执黑时从自己那一侧看棋盘。**只改显示方向**：棋子、FEN、引擎分析结果都不变，
  已经画好的箭头会跟着一起翻转；按钮旁的标签会显示当前视角。
  它和 `GRID_ORIENTATION` 无关 —— `GRID_ORIENTATION` 决定「棋盘数据怎么对应到行列」，
  换边只是在此基础上再对显示做一次 180° 旋转（`viewRow` / `viewCol`）。
- **保存棋局（💾）**：把当前这盘棋存进本机 SQLite（`data/chess.db`）。弹窗里可填棋局名、分类
  （对局/开局/残局/研究）、日期、红黑方、赛事、标签、备注，外加两个勾选项：
  「顺便记录引擎评分」让后端给每一步打分（一次最多 20 步，会慢一些）、
  「只存当前局面（不存历史着法）」不写 `moves` 表、只留一张局面快照（残局/摆设局面常用）。
  存过之后当前页面就**绑定**了这一局（棋盘下方标签显示「已绑定棋局 #N · 已走 M 步」），
  之后再点「保存」是存回同一局、新走的着法增量追加，不会另外建一局。
- **我的棋局（📂）**：打开 `/studies` 页面 —— 上边是按分类/标签/关键字筛选的棋局列表
  （卡片上显示步数、最后一步、结果、标签、更新时间），每一局可以「续下」「复盘」「改名」「删除」，
  下边是把 FEN / ICCS / PGN / JSON 贴进来导入。
  「续下」会跳回下棋页并带上 `?game=<id>`，下棋页据此自动把这一局摆到棋盘上，接着走子再保存就存回它。
- **导出导入（⇅）**：导出 `fenmoves / pgn / iccs / json / fen`（可复制文本框里的内容，也可点「下载文件」）；
  导入支持同样这几种格式，格式自动识别，也可以在下拉框里手动指定；导入成功后会新建一局并直接摆到棋盘上。
  注意**导入不需要先保存**（它会自己建一局），但**导出的是库里已保存的那一局**，
  没保存过要先点「💾 保存」。

前端零依赖、不用任何框架；后端只做转发，ICCS→中文着法的翻译统一由 `coord_utils.py` 提供。

### 棋盘行列与 ICCS 的映射（校准点）

棋盘有两套坐标系：

| 坐标系 | 说明 |
| ------ | ---- |
| ICCS   | `a`~`i` 表示第 1~9 路（`a` 在最左），数字 `0`~`9` 表示横线，`0` 是**红方底线**、`9` 是**黑方底线** |
| 棋盘行列 | `grid[行][列]`，行 `0` 在**顶部**（黑方底线），行 `9` 在**底部**（红方底线），列 `0` = `a` 列 |

换算只有一份实现，就在 `coord_utils.py`：`iccs_to_grid()` / `grid_to_iccs()`，
朝向由 `GRID_ORIENTATION` 一个开关决定：

- `'red_bottom'`（默认，行 0 在顶部 = 黑方底线）：`行 = 9 - 横线数字`
- `'black_bottom'`（行 0 在顶部 = 红方底线）：`行 = 横线数字`

`/api/analyze` 和 `/api/ai_hint` 都会把算好的 `from` / `to`（棋盘行列）直接发给前端，
前端不再自己解析 ICCS。所以**要校准朝向只改 `coord_utils.py` 这一处**，刷新页面即生效
（前端只剩「换边」那层显示用的 180° 旋转）。
改完可以跑 `python test_coord_utils.py` 验证（含 `h2e2` → 红炮所在格、与前端列号一致性等 17 个用例）。

### 接口

`POST /api/analyze`

```json
// 请求（movetime 可省略，省略时用后端当前配置的默认思考时间，默认 3000）
{"fen": "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1", "movetime": 2000}

// 响应
{
  "bestmove": "h2e2", "bestmove_cn": "炮二平五",
  "from": [7, 7], "to": [7, 4],
  "score": 26, "mate": null, "depth": 20,
  "pv": ["h2e2", "h9g7"], "pv_cn": ["炮二平五", "马8进7"],
  "moves": [{"rank": 1, "bestmove": "h2e2", "score": 26, "mate": null,
             "depth": 20, "pv": ["h2e2", "h9g7"]}],
  "primary": {"rank": 1, "bestmove": "h2e2", "score": 26, "mate": null,
              "depth": 20, "pv": ["h2e2", "h9g7"]},
  "side": "红方", "movetime": 2000
}
```

`from` / `to` 是**棋盘行列**（`[行, 列]`，行 0 在顶部），前端直接用它把最佳走法的起点终点高亮出来，
不用自己解析 ICCS。

这个接口固定用 `MultiPV=1`（时间全投在一路上，不给候选）——
`moves` / `primary` 与顶层 `bestmove` / `score` / `pv` / `depth` / `mate` 是同一份数据的两种写法，
顶层那几个是给老调用方（如 `demo.py`）留的兼容字段。

出错时返回 `{"error": "..."}`：FEN 为空或不合法返回 400，引擎缺失或崩溃返回 503。
`movetime` 会被限制在 100 ~ 60000 毫秒之间。

引擎进程全局复用一份，并用锁把分析请求串行化（UCI 会话是单进程的）；
引擎一旦崩溃，下一次分析会自动把它拉起来。

`POST /api/ai_hint`

```json
// 请求（movetime 省略 = 自动档，由后端按子力数决定；use_llm 默认 true）
{"fen": "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1",
 "movetime": 2000, "use_llm": true}

// 响应
{
  "ok": true,
  "primary": {                        // 引擎首选（MultiPV 第 1 路）
    "rank": 1, "iccs": "b2e2", "chinese": "炮八平五",
    "from": [7, 1], "to": [7, 4],
    "score": 30, "mate": null, "depth": 19,
    "pv_iccs": ["b2e2", "b9c7"], "pv_chinese": ["炮八平五", "马2进3"]
  },
  "candidates": [                     // 前 MultiPV 路候选，含 primary
    {"rank": 1, "iccs": "b2e2", "chinese": "炮八平五", "score": 30, "depth": 19, "from": [7, 1], "to": [7, 4], "pv_iccs": [], "pv_chinese": []},
    {"rank": 2, "iccs": "c3c4", "chinese": "兵七进一", "score": 28, "depth": 19, "from": [6, 2], "to": [5, 2], "pv_iccs": [], "pv_chinese": []},
    {"rank": 3, "iccs": "h2e2", "chinese": "炮二平五", "score": 27, "depth": 19, "from": [7, 7], "to": [7, 4], "pv_iccs": [], "pv_chinese": []}
  ],
  "llm_pick": {                       // 最终推荐（LLM 二次筛选的结果，降级时等于 primary）
    "rank": 1, "iccs": "b2e2", "chinese": "炮八平五",
    "from": [7, 1], "to": [7, 4],
    "score": 30, "mate": null, "depth": 19,
    "pv_iccs": [], "pv_chinese": [],
    "reason": "引擎首选（未配置 LLM，没做二次筛选）",
    "llm_used": false,
    "engine_first": true
  },
  "movetime": 3000,
  "movetime_mode": "auto",
  "side": "红方"
}
```

- `from` / `to` 是**前端棋盘行列**（`[行, 列]`，行 0 在顶部），前端拿到后直接画箭头，不用再换算。
- `score` 是**走子方视角**的厘兵分，正数表示走子方占优；前端再按走子方换算成「红优/黑优」。
  `mate` 不为 `null` 时表示有杀棋（正数 = 走子方 N 步内将死）。
- `candidates` 来自 MultiPV（默认 3 路），都是当前局面下可走的着法，可以直接画箭头预览；
  路数由 `POST /api/engine_config` 的 `multipv` 控制。
- `llm_pick` 是最终要显示给人看的那一步。`llm_used = false` 表示这次没走 LLM 二次筛选
  （没配 `LLM_API_KEY`、前端 `use_llm=false`，或调用失败降级），此时它就是引擎首选，
  `reason` 会写明原因。`engine_first` 表示它是否恰好等于引擎首选。
- `movetime` 省略时是**自动档**（`movetime_mode = "auto"`），由 `dynamic_movetime()` 按剩余子力数决定：
  多于 20 子 3000 毫秒、10~20 子 5000 毫秒、少于 10 子 8000 毫秒；手动档会把值夹在 100 ~ 60000 之间。

**容错约定**：除了参数错误（空 FEN / 非法 FEN 返回 HTTP 400），
其余情况一律返回 HTTP 200 + `{"ok": false, "error": "可读的错误原因"}`，
所以引擎没装好、NNUE 缺失、引擎崩溃、没算出候选着法时，前端只会看到一句提示，不会拿到 500。

`POST /api/engine_config`

```json
// 请求：四个字段都可选，只传要改的
{"threads": 4, "hash_mb": 256, "multipv": 3, "movetime": 3000}

// 响应
{"ok": true, "changed": true,
 "engine": {"threads": 4, "hash_mb": 256, "multipv": 3, "movetime": 3000}}
```

- `threads` / `hash_mb` / `multipv` 立刻以 `setoption` 发给一直在跑的那个引擎进程（**不用重启**）；
  `movetime` 是手动档没给毫秒数时的兜底思考时间。
- 取值会被夹到合法区间（线程 1~64、Hash 1~4096 MB、MultiPV 1~8、思考时间 100~60000 毫秒），
  非整数返回 400 `{"ok": false, "error": "..."}`。
- 改引擎参数失败（例如引擎启动不了）时不会记住这次改动，返回 `ok: false` 并带上当前仍然生效的配置。
- 启动时的默认值来自 `chess_engine.py` 里的 `ENGINE_THREADS` / `ENGINE_HASH_MB` / `ENGINE_MULTIPV`，
  也可以用环境变量 `PIKAFISH_THREADS` / `PIKAFISH_HASH` / `PIKAFISH_MULTIPV` 覆盖。

### AI 二次筛选（可选）

`explainer.py` 里的 `pick_best_move(fen, candidates)` 把引擎已经算好的几条候选交给 LLM，
让它**只能从中挑一条**并给理由（只做“挑选/翻译”不做“计算”，提示词里明确禁止自行推演走法、
禁止编造候选之外的着法）。配置走环境变量：

```bash
set LLM_API_KEY=sk-xxxx            # 必填，没有就跳过二次筛选，直接用引擎首选
set LLM_BASE_URL=https://api.openai.com/v1   # 可选，任何 OpenAI 兼容接口都行
set LLM_MODEL=gpt-4o-mini          # 可选
set LLM_TIMEOUT=20                 # 可选，秒
```

只用标准库 `urllib` 调用 `/chat/completions`，不引入新依赖。
**没配 Key 或调用失败（网络/超时/返回不是 JSON）都会自动降级**：
`llm_pick` 退回引擎首选，`reason` 写明降级原因（`引擎首选（未配置 LLM，没做二次筛选）`
/ `引擎首选（LLM 二次筛选失败，已降级）`），`llm_used = false`，引擎结果照常返回。
LLM 挑的着法如果不在候选里（幻觉），一律当成失败处理，绝不会把编出来的着法显示出来。

## 四、输出格式说明

| 字段     | 含义                                                                 |
| -------- | -------------------------------------------------------------------- |
| 最佳着法 | 引擎给出的最佳走法，ICCS 坐标格式，方括号内是中文着法                 |
| 评分     | 引擎评估值。`厘兵` 即 1/100 兵的价值；**正数表示轮到走棋的一方占优**。若引擎算到杀棋，这里会显示 `绝杀, X 步内将死` |
| 搜索深度 | 引擎搜索的层数，越深越可靠                                            |
| 候选变例 | PV（Principal Variation），引擎认为双方接下来的最佳应对序列           |

### 关于 FEN

中国象棋 FEN 与国象类似，但棋盘是 9 列 × 10 行：

```
rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1
```

- 大写字母为红方（`K`帅 `A`仕 `B`相 `N`马 `R`车 `C`炮 `P`兵），小写为黑方
- 第一段从**黑方底线**开始逐行往下写，数字表示连续的空格数
- 第二段 `w` 表示红方走棋，`b` 表示黑方走棋
- 注意：Pikafish 新版对 FEN 做**严格校验**，行列数不对、棋子不合法会直接报错退出，
  此时程序会提示「引擎拒绝了该棋面 (通常是 FEN 不合法)」

### 关于 ICCS 坐标

坐标形如 `h2e2`，前两位是起点、后两位是终点：

- 纵线用字母 `a`~`i` 表示，`a` 是红方视角的最左边；横线用数字 `0`~`9` 表示，`0` 是红方底线
- 例如 `h2e2` = 红炮从 h 列 2 行走到 e 列 2 行 = **炮二平五**

`coord_utils.py` 里的 `move_to_chinese()` 会把 ICCS 转成中文着法（`demo.py`、`app.py` 都复用它）：

- 红方用汉字数字（一~九），黑方用阿拉伯数字（1~9）
- 同一纵线上有两枚同种棋子时，用「前/后」区分，如 `前车进一`
- 车/炮/兵/将的进/退记步数，马/相(象)/仕(士)记目标纵线

> 简化说明：同一纵线上出现 **3 枚及以上**同种棋子时（例如多兵叠在同一线），
> 本实现不做「前/中/后」细分，仍按纵线编号输出。

## 五、API 用法

```python
from chess_engine import ChessEngine

engine = ChessEngine()                      # 默认读 engine/ 下的引擎
engine.configure_engine()                   # 可选: 发 Threads=4 / Hash=256MB / MultiPV=3
try:
    result = engine.analyze(
        "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1",
        movetime=3000,                      # 也可以省略, 用 engine.dynamic_movetime(fen) 让它自己决定
    )
    print(result["bestmove"])   # 'h2e2'
    print(result["score"])      # 38，走子方视角的厘兵分；若是杀棋则为 None
    print(result["mate"])       # 杀棋步数，非杀棋为 None
    print(result["pv"])         # ['h2e2', 'b9c7', ...]
    print(result["depth"])      # 搜索深度
    print(result["moves"])      # 多路候选: [{'rank': 1, 'bestmove': ..., 'score': ..., 'pv': [...]}, ...]
finally:
    engine.quit()
```

`ChessEngine` 的方法：

| 方法                                | 说明                                                     |
| ----------------------------------- | -------------------------------------------------------- |
| `start_engine()`                    | 启动子进程并发 `uci`，等待 `uciok`                       |
| `send_command(cmd)`                 | 向引擎发送单行命令                                       |
| `set_nnue(filepath)`                | 发 `setoption name EvalFile value <path>` 加载权重       |
| `configure_engine(threads, hash_mb, multipv)` | 设 `Threads` / `Hash` / `MultiPV`，在 `start_engine()` 之后调用；`MultiPV` 会记下来，作为 `analyze(multipv=None)` 的默认值 |
| `analyze(fen, movetime=3000, multipv=None)` | 分析棋面。返回 `moves`（按 `rank` 排序的候选，每项含 `bestmove` / `score` / `mate` / `depth` / `pv`）和 `primary`（`moves[0]`），同时保留顶层 `bestmove` / `score` / `mate` / `pv` / `depth` 这几个老字段；`multipv=None` 表示用 `configure_engine()` 设的路数（默认 1，即只给一条） |
| `dynamic_movetime(fen)`             | 按剩余子力数给思考时间：多于 20 子 3000、10~20 子 5000、少于 10 子 8000 毫秒 |
| `quit()`                            | 发 `quit`，必要时强杀子进程并关闭管道                    |

> Pikafish 支持的 `setoption` 只有 `Threads` / `Hash` / `MultiPV` / `EvalFile` 等少数几个，
> 别家的 `Repetition Rule` 这类选项它没有，发了也会被静默忽略，所以这里不发。

## 六、异常处理

所有引擎相关异常都抛出 `EngineError`，`demo.py` 会捕获并打印友好提示，
Web 界面则把同样的文字显示在页面状态栏（红色）：

- 引擎文件不存在 / 没有执行权限 → 打印下载指引
- 启动失败、管道写入失败 → 提示引擎崩溃
- 等待 `uciok` / `readyok` / `bestmove` 超时 → 提示超时
- 引擎中途意外退出 → 提示进程已退出
- 棋面被引擎拒绝 → 打印引擎的 `CRITICAL ERROR` 原文

## 七、常见问题

**Q: 提示 `未找到 Pikafish 引擎文件`**
按第一节把 `pikafish.exe` 和 `pikafish.nnue` 放进 `engine/` 目录，或用 `--engine` / `--nnue` 指定。

**Q: macOS / Linux 报 `Permission denied`**
`chmod +x engine/pikafish`。

**Q: 第一次分析很慢**
引擎启动 + 加载 NNUE 权重需要一点时间，之后同一进程内的分析会快很多。

**Q: 评分正负怎么看**
`score` 是**走子方视角**的厘兵分。红方走棋时正数代表红方占优；黑方走棋时正数代表黑方占优。

**Q: AI 教练卡片上标着「引擎首选」，没有挑选理由**
没设 `LLM_API_KEY`（或调用失败）时会跳过二次筛选，直接用引擎首选，卡片标签显示「引擎首选」，
理由那行会写明是「未配置 LLM」还是「LLM 二次筛选失败，已降级」。
想启用 LLM 挑选见上面「AI 二次筛选（可选）」，配置后需要重启 `python app.py` 才生效（环境变量在进程启动时读取）。

**Q: 想多给几条候选 / 让引擎算得更快**
展开面板下方的「引擎参数」，调 `MultiPV`（候选路数）、`Threads`（线程数）、`Hash`（哈希表 MB），
点「应用」即可，不用重启。

**Q: 改了 `templates/index.html`，刷新页面却没变化**
Flask 不是 debug 模式时不会自动重载模板，重启 `python app.py` 即可。

**Q: 箭头位置上下颠倒 / 画在错误的格子上**
只有一种可能：前后端的棋盘朝向不一致。校准点见「棋盘行号与 ICCS 的映射」一节 ——
改 `coord_utils.py` 里的 `GRID_ORIENTATION` 即可，前端会自动跟随。

**Q: 保存的棋局存在哪里？怎么备份 / 迁移？**
本机 `data/chess.db`（一个 SQLite 文件）。备份有两招：在「我的棋局」里导出 `json`，
或者直接拷走 `data/chess.db` 这个文件。想清空就停掉服务删掉它，下次启动自动重建空表。
详见下面的「八、棋局库」。

**Q: 换台电脑 / 换个浏览器，我的棋局还在吗？**
在。棋局存在服务端的 `data/chess.db` 里，跟浏览器无关；只要还是同一份项目目录、同一个
`python app.py` 进程，打开 `/studies` 就能看到。反过来，浏览器里的悔棋栈、绑定状态是不落盘的。

## 八、棋局库（保存 / 复盘）

「保存棋局、后期研究」这一层做的事：本机 SQLite 存盘 → `/studies` 列表 → 打开续下 → 逐步复盘 /
加评语 / 标关键步 → 复用现有引擎分析给某一步打分 → 导出 `fenmoves / iccs / pgn / json` → 再导入。

实现上**只用标准库**（`sqlite3` + `re` + `json`），没有 ORM、没有账号系统、不连公网存储，
单人本机使用；`requirements.txt` 不用加任何东西。

| 文件 | 职责 |
| ---- | ---- |
| `storage.py` | 建表 + 增删改查 + 事务（`sqlite3`，返回普通 dict） |
| `pgn_io.py` | 棋谱导入导出：`fen / fenmoves / iccs / pgn / json`，含中文着法 ↔ ICCS 反查 |
| `games_routes.py` | 9 个 HTTP 接口的 Blueprint，由 `app.py` 通过 `init_app()` 注入引擎 |
| `templates/studies.html` | 「我的棋局」页：列表 / 复盘 / 评语 / 导入导出 |
| `test_storage.py` / `test_pgn_io.py` / `test_games_routes.py` | 三层的单元测试（各 30 / 34 / 10 个用例） |

### 数据放在哪

| 项 | 说明 |
| -- | ---- |
| 数据库文件 | `data/chess.db`；目录不存在会自动建，**首次运行自动建表** |
| 换路径 | 环境变量 `XQ_DB_PATH`（测试就是靠它指向临时目录，不碰真库） |
| 推倒重来 | 停掉服务 → 删掉 `data/chess.db` → 下次启动重新建空表 |
| 备份 / 迁移 | 导出 `json`，或直接拷 `data/chess.db` 这一个文件 |

### 建表 SQL

就是 `storage.py` 里的 `SCHEMA`，首次运行自动执行（都是 `CREATE ... IF NOT EXISTS`，反复启动无副作用）：

```sql
CREATE TABLE IF NOT EXISTS games (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT,
  category      TEXT DEFAULT 'game',
  start_fen     TEXT,
  current_fen   TEXT,
  side_to_move  TEXT,
  result        TEXT DEFAULT '*',
  status        TEXT DEFAULT 'active',
  red_name      TEXT,
  black_name    TEXT,
  event         TEXT,
  site          TEXT,
  date          TEXT,
  round         TEXT,
  opening       TEXT,
  ecco          TEXT,
  tags          TEXT,
  note          TEXT,
  created_at    TEXT,
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS moves (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id            INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  ply                INTEGER NOT NULL,
  side               TEXT,
  iccs               TEXT,
  chinese            TEXT,
  from_sq            TEXT,
  to_sq              TEXT,
  captured           TEXT,
  fen_before         TEXT,
  fen_after          TEXT,
  score_cp           INTEGER,
  score_mate         INTEGER,
  eval_by            TEXT,
  comment            TEXT,
  is_key             INTEGER DEFAULT 0,
  branch_of_move_id  INTEGER,
  UNIQUE (game_id, ply)
);
CREATE INDEX IF NOT EXISTS idx_moves_game_ply ON moves(game_id, ply);
CREATE INDEX IF NOT EXISTS idx_games_category_updated ON games(category, updated_at);
CREATE INDEX IF NOT EXISTS idx_games_tags ON games(tags);
```

两张表怎么配合：

- **games** 一行 = 一局棋。`start_fen` 是起始局面（**残局/摆设局面就存这里**，不给就是标准开局），
  `current_fen` 是最新局面，`side_to_move` 是 `red`/`black` 的冗余列（列表页不用解析 FEN 就知道该谁走）。
  `category` 取 `game / opening / endgame / study / imported`，`result` 取 `1-0 / 0-1 / 1/2-1/2 / *`，
  `status` 取 `active / finished / archived`，`tags` 是逗号分隔的字符串。
- **moves** 一行 = 一个半回合，`ply` 从 1 开始，`UNIQUE(game_id, ply)` 保证写不重。
  每步都留着 `iccs` 和 `chinese` 两种记谱，以及 `fen_before` / `fen_after`
  （复盘时跳到任意一步都不用从头重放）；`score_cp` / `score_mate` 是**走这一步的人**的视角，
  `comment` 是评语，`is_key` 是关键步。
- 删 `games` 会**级联删**它的 `moves`（连接上开了 `PRAGMA foreign_keys = ON`）。
- `branch_of_move_id` 这一轮只建了列、主线一律 `NULL`：变着分支 / 走法树缩进是规划中的下一轮，
  现在的数据结构已经预留好了。

### 复盘页怎么用

`/studies` 里点某一局的「复盘」：

- 左边棋盘、右边走法列表（序号 / 红黑 / 中文着法 / ICCS / 评分 / 评语 / 关键步 ★）。
  列表里**点任意一行就跳到那一步之后**的局面，棋盘渲染的是 `moves[pos-1].fen_after`
  （`pos = 0` 也就是「开局」时用 `start_fen`），所以每步存的 `fen_after` 让跳转不用从头重放；
  也可以用「⏮ 开局」「◀ 上一步」「下一步 ▶」「最后 ⏭」，底下会显示「第 k / N 步之后」。
- 选中某一步后，可以在下面「这一步」里写评语、勾「标为关键步」，点「保存评语」即写库
  （只更新这一步的 `comment` / `is_key`，不动别的）。
- 「🤖 AI 分析该步之前的局面」调的是现有的 `/api/analyze`（复用同一个引擎进程），
  显示这一步之前轮谁走、引擎评分、引擎首选和后续变化。**只有当引擎首选跟实战走的不是同一步时**，
  才会多出一个「把评分记到这一步」按钮 —— 点它就把面板上那个分数**原样**写进这一步的
  `score_cp` / `score_mate`（`eval_by = engine`）。这里不用换算符号：分析的是 `fen_before`，
  引擎报的本来就是"走这一步的人"的视角，和「保存时顺便记录引擎评分」存下来的口径一致。
- 「导出」区可以生成 `fenmoves / pgn / iccs / json / fen` 文本，pgn 还能勾「中文着法」；
  点「下载文件」后端会带下载头（文件名用棋局名，中文用 RFC 5987 编码，不会乱码）。

列表页每一张卡片上有「复盘」「续下」「改名」「删除」「导出」五个操作。「续下」等同于
`/?game=<id>`，下棋页会自动载入这一局、把着法重放到最新局面，接着走子再点「保存」就存回它。

### 接口

所有接口的响应都是 `{"ok": true/false, ...}`；参数或规则出错一律 HTTP 400 + 中文 `error`，
**并且一个字节都不写库**。对局不存在是 404。

| 方法 | 路径 | 作用 |
| ---- | ---- | ---- |
| POST | `/api/games/save` | 新建 / 更新一局（可带整串着法） |
| POST | `/api/games/<id>/append_move` | 续下一步（先验合法再落子） |
| GET | `/api/games/list` | 列表（带步数，**不含**着法数组） |
| GET | `/api/games/<id>` | 详情（元信息 + 全部着法） |
| POST | `/api/games/<id>/update` | 改元信息 / 备注 / 结果 / 状态 |
| DELETE | `/api/games/<id>` | 删局（级联删着法） |
| POST | `/api/games/<id>/move/<move_id>/comment` | 改某步的评语 / 关键步 / 评分 |
| GET | `/api/games/<id>/export` | 导出 `fmt=fen\|fenmoves\|iccs\|pgn\|json` |
| POST | `/api/games/import` | 导入（自动认格式或显式指定 `fmt`） |

**`POST /api/games/save`**

```jsonc
// 请求：game_id 不给 = 新建；给了 = 更新那一局（并按 move_list 增量写 moves）
{
  "game_id": null,
  "name": "2026-09-17 中炮对屏风马",
  "category": "game",                 // game / opening / endgame / study / imported
  "start_fen": "rnbakabnr/... w - - 0 1",   // 不给就用库里的，再没有就是标准开局
  "current_fen": "rnbakabr1/... b - - 0 1", // 没给 move_list 时才会用到它
  "move_list": ["h2e2", "h9g7", "h0g2", "i9h9"],   // 也可写 [{"iccs": "h2e2", "comment": "当头炮"}]
  "meta": {"red": "红方", "black": "黑方", "event": "网战", "date": "2026-09-17",
           "tags": "中炮,顺手炮", "note": "第 12 手转折", "result": "*"},
  "score": false,        // true = 顺手让引擎给每步打分（最多 20 步）
  "movetime": 300        // 打分时引擎思考毫秒数（默认 300，夹在 50~10000）
}
// 响应
{"ok": true, "game_id": 1, "created": true,
 "current_fen": "rnbakabr1/9/1c4nc1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C1N2/9/RNBAKAB1R w - - 0 1",
 "side_to_move": "red", "move_count": 4, "scored": 0}
```

- `move_list` 的每一项可以是 ICCS 字符串，也可以是 `{iccs, chinese?, comment?, score_cp?, score_mate?, eval_by?, is_key?}`。
  给了就**整串重放**：能对上库里的公共前缀（顺手补评语）、多余的截掉、缺的追加 ——
  所以"同一局又走了几步"只写新增的那几行，不会把 `moves` 删了重建。
- 一走着不通（不在合法着法里）就整体拒绝并返回中文原因，不会存进去半截对局。
- `start_fen` / `current_fen` 都会先过 `rules.validate_fen()` 做结构校验，不合法直接报错。
- `score: true` 只给**还没评分**的着法补分，一步最多 `movetime` 毫秒、一次请求最多 20 步；
  引擎没装好或打分时挂了，只是这一步没有评分，不影响保存本身。

**`POST /api/games/<id>/append_move`**

```jsonc
// 请求（chinese 不给就按局面现算；score=true 时顺手给这一步打分）
{"iccs": "a0a1", "chinese": "车九进一", "comment": "抢先", "score": false}
// 响应
{"ok": true, "game_id": 1, "move_id": 5, "ply": 5, "side": "red", "iccs": "a0a1",
 "chinese": "车九进一",
 "fen": "rnbakabr1/9/1c4nc1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C1N2/R8/1NBAKAB1R b - - 0 1",
 "side_to_move": "black", "score_cp": null, "score_mate": null, "comment": "抢先"}
```

落子前先用当前局面 + `rules.apply_iccs()` 验一遍（不在 `legal_moves` 里、自将、将帅照面都会被拒），
落子、算新 FEN、写 `moves`、更新 `games.current_fen / side_to_move` 在同一个事务里完成。

**`GET /api/games/list?category=game&tag=中炮&keyword=屏风马&limit=50&offset=0`**

```jsonc
{"ok": true, "total": 1,
 "games": [{"id": 1, "name": "2026-09-17 中炮对屏风马", "category": "game",
            "result": "*", "status": "active", "side_to_move": "red",
            "current_fen": "rnbakabr1/... w - - 0 1", "red_name": "红方", "black_name": "黑方",
            "tags": "中炮,顺手炮", "date": "2026-09-17",
            "created_at": "2026-09-17 10:20:00", "updated_at": "2026-09-17 10:25:00",
            "move_count": 4, "last_chinese": "车9平8"}],
 "categories": ["game", "opening", "endgame", "study", "imported"]}
```

`category` 只认上面五个值（给别的返回 400）；`tag` 是 `tags` 列里做模糊匹配；
`keyword` 会在 `name / tags / note / opening / red_name / black_name` 里找。
**列表不返回 `moves` 数组**，只算出 `move_count` 和最后一步中文着法，大对局也不会拖慢列表页。

**`GET /api/games/<id>`** → `{"ok": true, ...games 的所有列..., "moves": [...]}`，
`moves` 里每项含 `id / ply / side / iccs / chinese / from_sq / to_sq / captured /
fen_before / fen_after / score_cp / score_mate / eval_by / comment / is_key / branch_of_move_id`。

**`POST /api/games/<id>/update`** → `{"name": "新名字"}` 或
`{"category": "endgame", "tags": "单车胜双士", "note": "...", "result": "1-0", "status": "finished"}`
（也可以包在 `meta` 里）；至少要有一个可改字段，否则 400。

**`DELETE /api/games/<id>`** → `{"ok": true, "game_id": 1}`（`moves` 级联删）。

**`POST /api/games/<id>/move/<move_id>/comment`** → `{"comment": "这里应走马八进七", "is_key": true}`
或 `{"score_cp": -23, "eval_by": "engine"}`；返回 `{"ok": true, "move": {...}}`（改完的那一步）。

**`GET /api/games/<id>/export?fmt=fenmoves&move_format=iccs&download=1`**

- `fmt=fen`：当前局面的 FEN 一行。
- `fmt=fenmoves`：`<start_fen> moves h2e2 h9g7 ...`，云库 / 引擎复盘直接吃这个格式。
- `fmt=iccs`：纯 ICCS 序列，一行空格分隔。
- `fmt=pgn`：中国象棋类 PGN，标签段有 `Game / Event / Site / Date / Round / Red / Black /
  Result / Opening / ECCO / FEN / Format`；`move_format=chinese` 时棋步写「炮二平五」并把
  `[Format]` 写成 `Chinese`，默认写 ICCS；评语写在 `{}` 里。
- `fmt=json`：`{"game": {...}, "moves": [...]}` 完整结构，用来备份 / 迁移。
- 加 `download=1` 会带 `Content-Disposition` 直接下载，文件名就是棋局名。

**`POST /api/games/import`**

```jsonc
// fmt 不给就自动认（PGN / JSON / fenmoves / 裸 FEN / ICCS）
{"content": "1. 炮二平五 马8进7 2. 马二进三 车9平8 *", "fmt": null, "name": "随手导入的"}
// 响应
{"ok": true, "game_id": 2, "fmt": "pgn", "name": "随手导入的", "move_count": 4,
 "current_fen": "rnbakabr1/... w - - 0 1", "warnings": []}
```

- 导入后**直接建一局**：着法逐步重放算好每一步的 FEN；解析不了的那几步只记进 `warnings`，
  不影响整局（`warnings` 里带第几步、原文是什么）。
- 缺标签用默认值补齐（`Event/Site/Round/Opening/ECCO` 给 `?`），缺 FEN 用标准开局；
  分类默认 `imported`，没给名字就按日期 + 格式自动起一个。用户显式传的 `name / category` 优先于内容里的标签。

### 一个端到端的例子（走几步 → 保存 → 复盘 → 导出 → 再导入）

**在网页里（推荐先跑这个）**

1. `python app.py`，打开 <http://127.0.0.1:5000>。
2. 点着走几步，比如 炮二平五、马8进7、马二进三、车9平8（棋盘下方标签会显示「已走 4 步」）。
3. 点「💾 保存」→ 棋局名填 `2026-09-17 中炮对屏风马`、分类选「对局」→ 保存。
   弹窗里出现「已保存到「我的棋局」#1（共 4 步）」，棋盘下方变成「已绑定棋局 #1 · 已走 4 步」。
4. 再走两步，再点「💾 保存」→ 还是 #1，只是变成 6 步（`moves` 表只新增了那两行）。
5. 点「📂 我的棋局」→ 列表里看到这一局（4+2 步、最后一步、标签）→「复盘」
   → 点走法列表任意一行跳到那一步 → 写评语 / 标关键步 / 「🤖 AI 分析该步之前的局面」。
6. 在「导出」区选 `fenmoves`，点「生成」看文本，再点「下载文件」存到本地；换成 `json` 再导一次
   就是完整备份。
7. 回下棋页点「⇅ 导出导入」→ 把上面导出的 `fenmoves` 或一段 PGN 贴进「导入棋谱」→「导入到棋盘」
   → 会新建一局并把着法摆到棋盘上，接着就能走子 / 再保存。

**等价的接口调用**（`curl`；PowerShell 里换成 `Invoke-RestMethod -UseBasicParsing -Body ...`，
中文建议走 UTF-8 文件，避免控制台编码把请求体弄乱）

```bash
# 1) 保存整局（不带 game_id = 新建）
curl -X POST http://127.0.0.1:5000/api/games/save -H "Content-Type: application/json" -d '{
  "name": "2026-09-17 中炮对屏风马", "category": "game",
  "red_name": "红方", "black_name": "黑方", "date": "2026-09-17", "tags": "中炮,顺手炮",
  "start_fen": "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1",
  "move_list": ["h2e2", "h9g7", "h0g2", "i9h9"]
}'
# → {"ok":true,"game_id":1,"created":true,"move_count":4,"side_to_move":"red",...}

# 2) 续下一步（校验合法后落子，并推进到新局面）
curl -X POST http://127.0.0.1:5000/api/games/1/append_move \
     -H "Content-Type: application/json" -d '{"iccs":"a0a1","comment":"抢先"}'
# → {"ok":true,"move_id":5,"ply":5,"chinese":"车九进一","side_to_move":"black",...}

# 3) 列表 / 详情
curl "http://127.0.0.1:5000/api/games/list?category=game&limit=20"
curl http://127.0.0.1:5000/api/games/1

# 4) 给第 5 步写评语并标关键步
curl -X POST http://127.0.0.1:5000/api/games/1/move/5/comment \
     -H "Content-Type: application/json" -d '{"comment":"出车抢先","is_key":true}'

# 5) 导出三种格式（download=1 会带上下载头）
curl "http://127.0.0.1:5000/api/games/1/export?fmt=fenmoves"
curl "http://127.0.0.1:5000/api/games/1/export?fmt=pgn&move_format=chinese"
curl "http://127.0.0.1:5000/api/games/1/export?fmt=json&download=1" -o game1.json
```

导出的样子（四步开局 + 第一步一条评语）：

```text
# fmt=fenmoves
rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1 moves h2e2 h9g7 h0g2 i9h9

# fmt=iccs
h2e2 h9g7 h0g2 i9h9

# fmt=pgn（move_format=iccs；评语跟在着法后面）
[Game "2026-09-17 中炮对屏风马"]
[Event "网战"]
[Site "?"]
[Date "2026-09-17"]
[Round "?"]
[Red "红方"]
[Black "黑方"]
[Result "*"]
[Opening "?"]
[ECCO "?"]
[FEN "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"]
[Format "ICCS"]

1. h2e2 {当头炮} h9g7 2. h0g2 i9h9
*

# fmt=pgn（move_format=chinese）第 13 行起是：
1. 炮二平五 {当头炮} 马8进7 2. 马二进三 车9平8
*
```

### 再导入一份 PGN 接着复盘

```bash
curl -X POST http://127.0.0.1:5000/api/games/import -H "Content-Type: application/json" -d '{
  "content": "[Game \"教学：中炮对屏风马\"]\n[Red \"甲\"]\n[Black \"乙\"]\n[Date \"2026-09-17\"]\n[Format \"Chinese\"]\n\n1. 炮二平五 {抢中} 马8进7 2. 马二进三 车9平8 *"
}'
# → {"ok":true,"game_id":2,"fmt":"pgn","name":"教学：中炮对屏风马","move_count":4,"warnings":[]}
```

- 着法是**逐个 token 自己判断**的：`h2e2` 这种直接当 ICCS，其余的按中文反查坐标（`炮二平五`）——
  所以 `[Format]` 写 `Chinese` 还是 `ICCS`、甚至干脆不写，都能解析，标签只用来记录格式。
  异体字（`車/馬/砲`）和红方误用阿拉伯数字也能识别。
- 中文着法反查的做法是：拿当前局面的**全部合法着法**逐个生成中文着法建索引，再按走子方的体例去查 ——
  所以「同一纵线两枚同种棋子」的「前/后」也准。
- 解析不出来的那一步不会中断导入，只记在 `warnings` 里（页面上会提示「有 N 处着法没能解析」，
  详细内容打在浏览器控制台），其余着法照常入库。
- 导入完回到「我的棋局」点这局的「复盘」，就能逐步查看、写评语、点「🤖 AI 分析该步之前的局面」。

### 关于"顺手打分"和残局研究

- 「保存时顺便记录引擎评分」（`score: true`）与复盘页的「把评分记到这一步」都复用现有引擎，
  走的是同一个 `ChessEngine.analyze()`，也共用那把 `_analyze_lock`（不会跟页面上的分析抢引擎）。
  默认每步只给 300 毫秒，一次请求最多 20 步 —— 想全量评分就多点几次保存，或者只给关键步打分。
- 研究 / 残局模式可以把 `start_fen` 换成任意摆设局面存下来（分类选「残局」或「研究」，
  或勾「只存当前局面」）。`validate_fen()` 只做**结构校验**（10 行、每行 9 列、棋子字母合法、
  走子方是 `w`/`b`），默认**不要求双方将帅都在**（残局摆设局面常常"缺将"，要严格查可以传
  `require_kings=True`），也不管将帅是否照面；真正的走子合法性仍然由
  `rules.legal_moves()` + 落子规则（不能自将、不能照面）把关，所以摆出来的局面一样能接着走、能复盘。

---

## 版本管理与分支模型

本仓库把「稳定版 Web」与「残局研究版」放进同一条历史，用分支区分，长期以 `main` 作唯一稳定线。

### 分支一览

| 分支 | 用途 | 来源 | 本地端口 |
| --- | --- | --- | --- |
| `main` | **稳定版**：基础对局、AI 提示、保存与复盘。唯一长期稳定线 | — | 5000 |
| `feature/endgame` | 残局研究：摆盘 / 候选 / 主变 / 备注、引擎深度分析、定式库、LLM 解说、研究导入导出 | 从 `main` 创建 | 5001 |
| `feature/mobile` | 手机端（安卓 / iOS / WASM） | 从 `main` 创建 | 5002 |

约定：

- **只从 `main` 派生**，特性分支之间不互相合并；
- 特性分支定期用 `scripts/branch_manager.sh sync <分支>` 把 `main` 的改动并进来；
- 回灌 `main` 走人工确认（脚本里反向同步必须显式加 `--confirm`）；
- 不在 `main` 上直接堆功能，`main` 只接受验证过的合并。

### 目录结构

```
<repo>/
  app.py chess_engine.py storage.py rules.py pgn_io.py games_routes.py coord_utils.py explainer.py
  templates/            index.html / studies.html（公共）、study.html（残局）
  endgame/              残局研究模块：仅 feature/endgame 存在
  migrations/           表结构迁移 SQL（残局研究相关表）
  tests                 test_*.py（公共）与 test_endgame_*.py（残局）
  scripts/              分支管理 / 运行 / worktree / 初始化报告
  engine/               本地引擎目录：Pikafish.exe 与 NNUE 不入库，见下
  data/                 运行时数据库目录：*.db 不入库，见下
```

`main` 的**仓库根目录就是可直接启动的稳定版**；`feature/endgame` 在同样的根目录上叠加残局模块，所以
`git diff main...feature/endgame` 展示的就是这个模块的完整增量（新增 + 修改），不含任何与残局无关的改动。

### 引擎与 NNUE（不入库）

`engine/Pikafish.exe`（约 6.6MB）与 `engine/pikafish.nnue`（约 48MB）**不进仓库**（见 `.gitignore` 末节），
克隆后自行放到 `engine/` 下即可：

- 引擎与权重下载：<https://github.com/official-pikafish/Pikafish/releases>
- 放好后 `python app.py`；`engine/README.txt` 记录了当前使用的版本与放置方式。

需要把引擎随仓库一起分发（例如给非开发同学用）时，删掉 `.gitignore` 里 `engine/Pikafish.exe`、
`engine/*.nnue` 三行再提交即可——但仓库体积会各分支各涨约 55MB。

### 数据库与迁移（不入库）

- `data/*.db` 及其 `-journal/-wal/-shm` 是运行时文件，已忽略；
- 残局版引入了新表，切换分支后按需执行：

  ```bash
  python migrate.py up      # 应用迁移（幂等）
  python migrate.py down    # 回滚
  ```

- 不同分支的库各自独立，**不要**把一个分支的 `.db` 覆盖到另一个分支当“同步”；
  需要搬数据请用残局研究的导出/导入（`xq-endgame-study v1`）或 `pgn_io` 的棋局导入导出。

### 脚本

| 脚本 | 作用 |
| --- | --- |
| `scripts/branch_manager.sh` | `list / switch / new / diff / status / sync / delete` |
| `scripts/run.sh` | `stable / endgame / mobile / all / stop / status` 按端口启动与停止 |
| `scripts/worktree_setup.sh` | `init / list / remove`，多分支并行工作区 |
| `scripts/init_report.sh` | 生成 `scripts/init_report.txt`（分支 / commit / 差异统计 / 未跟踪 / 远程状态） |

常用命令（Windows 上用 **Git Bash** 跑，不是 PowerShell）：

```bash
scripts/branch_manager.sh list
scripts/branch_manager.sh switch feature/endgame    # 有未提交改动会提示 stash / 提交 / 中止
scripts/branch_manager.sh diff main feature/endgame
scripts/branch_manager.sh sync feature/endgame      # 只允许 main -> 特性分支
scripts/branch_manager.sh new feature/xxx           # 从 main 派生新特性分支

scripts/worktree_setup.sh init                      # ../chess-coach-endgame、../chess-coach-mobile
scripts/run.sh all                                  # 5000/5001 一起后台起，PID 在 scripts/.pids/
scripts/run.sh stop
```

一个工作区只能检出一个分支，所以「同时跑稳定版和残局版」要靠 worktree：`worktree_setup.sh init`
之后 `run.sh all` 会自动去 `../chess-coach-endgame` 启动残局版。

### 初始化与差异报告

首次导入（stable + endgame 合仓）由 `scripts/init_repo.sh` 完成，它分 8 个阶段执行，任一步失败即中止。
导入结果、分支与 commit、文件差异统计、暂存目录处理情况写在 `scripts/init_report.txt`（本地产物，不入库）。

### 后续演进（尚未实现）

合并残局分支之前先把公共代码抽出来，避免两条线长期分叉：

1. `core/`：与平台无关的公共层（`chess_engine`、`explainer`、`rules`、`pgn_io`、`storage` 的纯逻辑部分）；
2. `web/routes/`：Web 路由（`games_routes`、`endgame.routes`）与模板；
3. `mobile/`：手机端，先定公共引擎接口（局面表示、搜索参数、结果结构），再分别实现安卓与 iOS，
   C++ 引擎优先考虑复用（Android NDK / iOS 静态库或 WASM）。

抽层完成前，`main` 与 `feature/endgame` 的公共文件保持同源改动，合并时人工核对。
