# 项目目录说明

核对日期：2026-10-27。当前开发仓库仍在原位置，目标总目录为 `D:\CODEX_FLUKE`。目录迁移完成前，以实际路径为准。

| 内容 | 位置 |
| --- | --- |
| 当前主仓库（main） | `D:\fluke8845a-lab` |
| 候选 ZIP、EXE 与源码包 | 主仓库的 `dist\` |
| 构建缓存和中间文件 | 主仓库的 `build\` |
| 测试临时文件、日志和截图 | 主仓库的 `build\test-artifacts\` |
| 运行验证报告 | 主仓库的 `output\` |
| 原始官方资料、旧实现 | `D:\Fluke8845A_Agent_Reference_Pack`（已标注不是当前开发目录） |
| 另一个旧 Git checkout | `D:\CODEX_FLUKE\archives\f8845a-r3`（有未提交改动，已无损迁入） |
| 原始资料 ZIP | `D:\CODEX_FLUKE\archives\Fluke8845A_Agent_Reference_Pack.zip` |
| 界面参考与目标记录 | `D:\CODEX_FLUKE\design-reference\` |

## 本次归档

此前散落于 D 盘根目录的测试目录、手册提取文本，以及两个项目根目录的测试缓存，集中归档到：

`D:\fluke8845a-lab\build\test-artifacts\archived-d-root-20261022`

该目录内三个 `*migration-manifest.json` 记录原位置、新位置和每项结果。采用同盘移动，不删除内容、不合并同名目录。需要查找旧文件时按清单中的 destination 查找。

共归档 207 项，移动后逐项核对原位置不存在且新位置存在。旧资料目录的 `.pytest_cache` 因 Windows 拒绝访问保留在原位置，清单记为 failed；没有修改其权限。

实验数据、用户资料和工具链未移动；当前开发仓库及原资料目录尚未迁移。`D:\Temp`、`D:\tmp`、`D:\CodexTemp` 保留，不因名称像临时目录就移动用户未知内容。

## 完成总目录迁移

`D:\CODEX_FLUKE\Finish-Move.cmd` 调用同目录的 `finish-move.ps1`，对当前仓库和原资料目录做同盘原子移动，拒绝覆盖或合并目标目录。移动前后核对 Git HEAD、工作区差异与状态，修复 worktree 路径，检查 Python 模块导入。若目录被占用，应退出占用程序后重试，不强制终止进程。迁移结果写入 `D:\CODEX_FLUKE\finish-move-result.json`。

移动成功后，在 Codex 打开 `D:\CODEX_FLUKE\fluke8845a-lab`；使用环境的 `python.exe -m pip` 维护依赖，避免使用可能仍含旧绝对路径的独立 pip 启动器。

今后开发遵守仓库 `AGENTS.md`：不再往 D 盘或仓库根目录生成测试目录。便携包实际构建、测试是否通过，以交付报告为准；目录整理不等同于软件验收。
