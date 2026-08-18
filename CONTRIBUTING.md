# Contributing / 参与贡献

感谢你愿意帮助 `restore-bead-pattern`。不会编程也可以贡献：测试安装、报告失败样本、改进文档、核对实体材料，和编写代码同样重要。

Thank you for contributing. Non-code contributions—testing, documentation, reproducible bug reports, and responsibly licensed samples—are welcome.

## 项目边界

本仓库有两条必须明确分开的流程：`restore` 恢复**源图中已经存在**的离散方格；`design` 把普通照片或插画缩减为新的 52×52 或 78×78 拼豆设计。提交前请保持以下边界：

- `restore` 只恢复原生网格，不凭语义重画角色；
- `restore` 保留不确定性，不为了“更像”而修改无证据格子；
- `design` 始终输出 `review`、`not_restoration: true`，不得冒充原生网格复原；
- 可复用设计流程必须保持通用，不得硬编码某张样本、角色或私人图片的坐标和修正规则；
- 模具、色卡、原生网格和新设计画布是相互独立的概念。

## 可以怎样参与

- **无代码**：在 Apple Silicon 或 Intel Mac 上做干净安装测试；
- **文档**：改进中文说明、增加英文翻译、补充无障碍文本；
- **测试数据**：贡献本人拥有版权或明确开放许可的样本及预期矩阵；
- **材料核验**：记录实体模具尺寸或商家色卡差异，但不要复制无再分发许可的完整色卡；
- **Python / CV**：网格估计、旋转/透视处理、拓扑、通用图片量化、色彩匹配、鲁棒性测试；
- **Issue 整理**：复现问题、补充环境信息、确认重复报告。

优先从 [`good first issue`](https://github.com/Rabbitgenius-rgb/restore-bead-pattern/labels/good%20first%20issue) 或 [`help wanted`](https://github.com/Rabbitgenius-rgb/restore-bead-pattern/labels/help%20wanted) 开始。领取任务前先留言说明计划，避免重复劳动。

## 本地开发

需要 Python 3.10–3.12、NumPy 和 Pillow。

```bash
git clone https://github.com/Rabbitgenius-rgb/restore-bead-pattern.git
cd restore-bead-pattern
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

运行完整离线测试：

```bash
PYTHONDONTWRITEBYTECODE=1 python \
  skills/restore-bead-pattern/scripts/self_test.py
python tests/validate_release.py
```

提交前还应运行：

```bash
git diff --check
```

不要为了让测试通过而降低安全上限、移除不确定性、跳过许可证通知或硬编码某张样本的坐标。

## Bug 报告

请使用 Bug Report 表单，并提供：

- 操作系统、芯片架构、Python/NumPy/Pillow 版本；
- 完整命令（删除用户名、绝对路径和令牌）；
- stdout 单行摘要与必要的 stderr；
- 预期结果和实际结果；
- 可以公开分享的最小复现材料。

安全问题不要提交公开 Issue，请按 [`SECURITY.md`](SECURITY.md) 私下报告。

## 图片、样本与隐私

样本必须满足以下至少一项：

- 由你本人创作并明确同意以项目兼容许可公开；
- CC0 / Public Domain；
- CC BY，并提供作者、来源和许可链接；
- 有可验证的书面再分发许可。

不要提交：

- 未获授权的角色图、商品图、付费图纸或商家完整色卡；
- 含姓名、地址、账号、定位、聊天记录或其他个人信息的截图；
- EXIF 中含敏感信息的原图；
- 工具运行输出中的私有源图叠加，除非源图本身允许公开。

优先使用程序生成的合成 fixture。必要的真实样本应尽量小，并附 `source`, `author`, `license`, `expected_use` 信息；仓库媒体必须逐文件登记在 [`ASSET_ATTRIBUTIONS.md`](ASSET_ATTRIBUTIONS.md)。

## 代码要求

- 生产代码必须保持离线，不上传用户图片；
- 不使用 `shell=True`、`eval`、`pickle` 或隐式网络请求；
- 文件写入应采用暂存目录和原子提交；
- 保持 `--overwrite` 所有权标记与受保护路径检查；
- 新增功能必须有确定性的合成回归测试；
- 不把 `__pycache__`、输出目录、源图或本机路径提交到仓库；
- 对 Schema 或默认行为的兼容性变化应更新版本和文档。

## Pull Request

1. 从 Issue 或 Discussion 明确任务范围；
2. Fork 仓库并创建聚焦分支；
3. 只提交与任务有关的文件；
4. 填写 PR 模板，关联 Issue，说明测试与风险；
5. 等待 CI；根据评审反馈继续在同一分支更新。

小型 PR 更容易被快速评审。请不要把格式化全仓库、功能新增和重构混成一个提交。

提交贡献即表示：你有权提交相关内容，并同意你的贡献按仓库的 MIT License 发布；第三方材料仍受其原许可证约束，并必须保留完整归因。

## 认可贡献

合并后的贡献会出现在 GitHub Contributors；重要贡献也会在 Release Notes 中致谢。持续帮助项目后，再讨论维护权限，不需要一开始就成为仓库协作者。
