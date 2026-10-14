# Release and package publishing

[简体中文](#简体中文) | [English](#english)

## 简体中文

- 每次推送到 `main`，GitHub Actions 分别在 Windows / Python 3.11 和 3.12 环境安装锁定依赖，并为每个 pytest 步骤设置 **60 秒上限**。两套测试都通过后，才为该提交创建独立预发布版本，附带 `Fluke8845A_Source_Project.zip` 和 `SHA256SUMS.txt`。
- 推送 `v*` 版本标签会创建对应的正式 GitHub Release，并附带同样的源码包和校验文件。
- 重跑同一提交的工作流会更新该 Release 中的附件，不会重复创建同名 Release。
- Release 附件是下载包；本项目没有向 GitHub Packages 注册表发布 Python 包。
- **暂不发布 Windows 便携 EXE。** Qt/PySide 对应源码、完整第三方声明和最终包上的库替换验证仍未闭环，详见 [Qt 许可审查](licenses/windows-portable-review-2026-10-14.md)。源码 ZIP 会保留可复现的 Windows 构建脚本。
- 自动发布使用仓库的 `GITHUB_TOKEN`，不需要个人访问令牌。测试与 Mock 验证不能视为真实 8845A 硬件验证。

## English

- Every push to `main` runs pytest on Windows with locked Python 3.11 and 3.12 environments; each pytest step has a **60-second limit**. Both suites must pass before the workflow creates a commit-specific prerelease with `Fluke8845A_Source_Project.zip` and `SHA256SUMS.txt`.
- Pushing a `v*` version tag creates the corresponding stable GitHub Release with the same source package and checksum file.
- Rerunning a workflow for the same commit updates the existing Release assets instead of creating a duplicate Release.
- Release attachments are downloadable project packages; this project does not publish a Python package to the GitHub Packages registry.
- **The Windows portable EXE is not published yet.** The Qt/PySide corresponding-source set, complete third-party notices, and library replacement verification against the final archive remain open. See the [Qt licensing review](licenses/windows-portable-review-2026-10-14.md). The source ZIP retains the reproducible Windows build script.
- Publishing uses the repository's `GITHUB_TOKEN`; no personal access token is required. Tests and Mock checks do not constitute validation on a real 8845A.
