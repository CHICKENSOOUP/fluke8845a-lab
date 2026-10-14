# Release and package publishing

[简体中文](#简体中文) | [English](#english)

## 简体中文

- 每次推送到 `main`，GitHub Actions 分别在 Windows / Python 3.11 和 3.12 环境安装锁定依赖，并为每个 pytest 步骤设置 **60 秒上限**。两套测试都通过后，才为该提交创建独立预发布版本，并同时发布 `Fluke8845A_Source_Project.zip`、`SHA256SUMS.txt` 和 GHCR OCI 文件包。
- 推送 `v*` 版本标签会创建对应的正式 GitHub Release，并将同一源码 ZIP 和校验文件发布到 GHCR。
- Release 与 GHCR 使用相同的版本标签。GHCR 包名为 `ghcr.io/chickensooup/fluke8845a-lab-source`；例如 `main` 提交 `0123456789ab` 对应 `main-build-0123456789ab`。
- GitHub Packages 没有原生 Python 包注册表；这里把源码 ZIP 和校验文件作为 OCI 文件包发布，不是可用 `pip install` 安装的 Python wheel。可用 [ORAS](https://oras.land/) 下载：`oras pull ghcr.io/chickensooup/fluke8845a-lab-source:main-build-0123456789ab --output ./dist`。
- 当前 GHCR 包 `ghcr.io/chickensooup/fluke8845a-lab-source` 已设为 Public；2026-10-15 对标签 `main-build-885d80bace96` 进行匿名 manifest 读取，收到 HTTP 200。后续提交只会在这个现有包下新增标签，沿用当前公开可读状态。如果以后改用新的包名，首次发布后需核对其可见性；新建 GHCR 包可能默认为私有。
- 重跑同一提交的工作流会更新该 Release 的附件和 GHCR 版本，不会重复创建同名 Release。
- **暂不发布 Windows 便携 EXE。** Qt/PySide 源码要约、完整第三方声明和最终包上的库替换验证仍未闭环，详见 [Qt 许可审查](licenses/windows-portable-review-2026-10-15.md)。源码 ZIP 会保留可复现的 Windows 构建脚本。
- 自动发布使用仓库的 `GITHUB_TOKEN`（授予 `packages: write`），不需要个人访问令牌。测试与 Mock 验证不能视为真实 8845A 硬件验证。

## English

- Every push to `main` runs pytest on Windows with locked Python 3.11 and 3.12 environments; each pytest step has a **60-second limit**. Both suites must pass before the workflow creates a commit-specific prerelease and publishes `Fluke8845A_Source_Project.zip`, `SHA256SUMS.txt`, and a GHCR OCI file package.
- Pushing a `v*` version tag creates the corresponding stable GitHub Release and publishes the same source ZIP and checksum to GHCR.
- The Release and GHCR use the same version tag. The package is `ghcr.io/chickensooup/fluke8845a-lab-source`; for example, commit `0123456789ab` on `main` uses `main-build-0123456789ab`.
- GitHub Packages does not provide a native Python package registry. The source ZIP and checksum are published as an OCI file package, not as a Python wheel installable with `pip`. Download with [ORAS](https://oras.land/): `oras pull ghcr.io/chickensooup/fluke8845a-lab-source:main-build-0123456789ab --output ./dist`.
- The current GHCR package `ghcr.io/chickensooup/fluke8845a-lab-source` is Public. An anonymous manifest read for tag `main-build-885d80bace96` returned HTTP 200 on 2026-10-15. Future commits add tags to this existing package and retain its public access. If the workflow ever switches to a new package name, verify visibility after its first publication because a new GHCR package may default to private. Release assets remain directly downloadable.
- Rerunning a workflow for the same commit updates the existing Release assets and GHCR tag instead of creating a duplicate Release.
- **The Windows portable EXE is not published yet.** The Qt/PySide source offer, complete third-party notices, and library replacement verification against the final archive remain open. See the [Qt licensing review](licenses/windows-portable-review-2026-10-15.md). The source ZIP retains the reproducible Windows build script.
- Publishing uses the repository's `GITHUB_TOKEN` with `packages: write`; no personal access token is required. Tests and Mock checks do not constitute validation on a real 8845A.
