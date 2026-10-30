# Release and package publishing

[简体中文](#简体中文) | [English](#english)

## 简体中文

- 推送与项目版本一致的 `v*` 标签时，GitHub Actions 在 Windows 上运行 Python 3.11、3.12 与 Web 测试，检查源码 ZIP 的首次启动，然后发布同版本 GitHub Release 和 GHCR 源码包。
- 推送 `v*` 版本标签会创建对应的正式 GitHub Release，并将同一源码 ZIP 和校验文件发布到 GHCR。正式 tag 必须与 `pyproject.toml` 的 `project.version` 相同，例如 `v1.0.4`；不匹配时工作流会在发布前失败。工作流会将 `docs/release-notes-v<version>.md` 加入正式 Release 说明。
- Release 与 GHCR 使用相同的版本标签。GHCR 包名为 `ghcr.io/chickensooup/fluke8845a-lab-source`。
- 工作流先创建或更新同标签的 GitHub Release 草稿，再发布并回读 GHCR OCI 清单，核对源码 ZIP 和校验文件的 SHA-256，最后才公开草稿 Release；如果 Release 权限检查或 OCI 校验失败，不会把新 Release 公开。失败后可重跑同一提交完成同步。
- GitHub Packages 没有原生 Python 包注册表；这里把源码 ZIP 和校验文件作为 OCI 文件包发布，不是可用 `pip install` 安装的 Python wheel。可用 [ORAS](https://oras.land/) 下载：`oras pull ghcr.io/chickensooup/fluke8845a-lab-source:v2.2.0 --output ./dist`。
- GHCR 包 `ghcr.io/chickensooup/fluke8845a-lab-source` 已设为 Public；2026-10-15 对旧标签 `main-build-676db181ced9` 进行匿名 manifest 读取，收到 HTTP 200，摘要为 `sha256:b285f6a4e20942c312631d4fb846273b85ccf42673adce539f5563fd631a41ef`。对应的历史 [GitHub Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-676db181ced9) 含源码 ZIP 和 `SHA256SUMS.txt`。新的版本标签仍与其 GHCR OCI Package 同名；只有两者都发布成功，才算该次推送完成。如果以后改用新的包名，首次发布后需核对其可见性；新建 GHCR 包可能默认为私有。
- 重跑同一提交的工作流会更新该 Release 的标题、说明、附件和 GHCR 版本，不会重复创建同名 Release。
- 已发布的历史 `main-build-*` 记录不会由代码变更自动重命名或删除；本规则只适用于后续工作流运行。历史发布仍会显示旧的无版本号标签，需要单独的历史整理操作。
- **暂不发布 Windows 便携 EXE。** Qt/PySide 源码来源与要约、完整第三方声明和最终包上的库替换验证仍未闭环，详见 [2026-10-20 Qt 许可审查](licenses/windows-portable-review-2026-10-20.md)。源码 ZIP 会保留可复现的 Windows 构建脚本。
- 自动发布使用仓库的 `GITHUB_TOKEN`（授予 `packages: write`），不需要个人访问令牌。测试与 Mock 验证不能视为真实 8845A 硬件验证。

## English

- Pushing a `v*` tag matching the project version runs Windows Python 3.11 and 3.12 tests, Web tests, and the extracted-source startup check, then publishes the matching GitHub Release and GHCR source package.
- Pushing a `v*` version tag creates the corresponding stable GitHub Release and publishes the same source ZIP and checksum to GHCR. The tag must match `project.version`, such as `v1.0.4`; a mismatch fails before publication. The workflow includes `docs/release-notes-v<version>.md` in the stable Release description.
- The Release and GHCR use the same version tag. The package is `ghcr.io/chickensooup/fluke8845a-lab-source`.
- The workflow creates or updates a draft Release for that tag first, then publishes and reads back the GHCR OCI manifest, verifies the ZIP and checksum SHA-256 values, and only then makes a new draft Release public. A Release permission failure or OCI validation error therefore does not publish a new Release; rerunning the same commit can finish synchronization.
- GitHub Packages does not provide a native Python package registry. The source ZIP and checksum are published as an OCI file package, not as a Python wheel installable with `pip`. Download with [ORAS](https://oras.land/): `oras pull ghcr.io/chickensooup/fluke8845a-lab-source:v2.2.0 --output ./dist`.
- The GHCR package `ghcr.io/chickensooup/fluke8845a-lab-source` is Public. An anonymous manifest read for historical tag `main-build-676db181ced9` returned HTTP 200 on 2026-10-15 with digest `sha256:b285f6a4e20942c312631d4fb846273b85ccf42673adce539f5563fd631a41ef`. Its historical [GitHub Release](https://github.com/CHICKENSOOUP/fluke8845a-lab/releases/tag/main-build-676db181ced9) contains the source ZIP and `SHA256SUMS.txt`. Versioned releases and packages share the same tag. If the workflow ever switches to a new package name, verify visibility after its first publication because a new GHCR package may default to private. Release assets remain directly downloadable.
- Rerunning a workflow for the same commit updates the existing Release title, notes, assets and GHCR tag instead of creating a duplicate Release. Existing historical `main-build-*` entries are not renamed or deleted by this source change; historical cleanup requires a separate operation.
- **The Windows portable EXE is not published yet.** Qt/PySide source provenance and offer, complete third-party notices, and library replacement verification against the final archive remain open. See the [2026-10-20 Qt licensing review](licenses/windows-portable-review-2026-10-20.md). The source ZIP retains the reproducible Windows build script.
- Publishing uses the repository's `GITHUB_TOKEN` with `packages: write`; no personal access token is required. Tests and Mock checks do not constitute validation on a real 8845A.
