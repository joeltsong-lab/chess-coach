# 手机端（占位）

本分支从 `main` 派生，**不含残局研究代码**，只放安卓 / iOS / WASM 的落地实现。

## 计划

1. 先定公共引擎接口：局面表示、搜索参数（深度 / 限时 / MultiPV）、结果结构（bestmove / pv / 评分）；
2. 引擎复用：优先把 C++ 引擎按平台编译（Android NDK、iOS 静态库），Web/小程序侧考虑 WASM；
3. 再分平台实现 UI 与对局流程，公共逻辑尽量下沉到 `core/`（见根 README 的「后续演进」）。

## 本地端口

预留 5002（`scripts/run.sh mobile`）：目前只有本文件，脚本会提示“尚无启动入口”，不会真的起服务。

## 起步

```bash
git merge main                        # 把 main 的最新改动并进当前分支
scripts/branch_manager.sh sync feature/mobile   # 同上，脚本会先检查工作区是否干净
```
