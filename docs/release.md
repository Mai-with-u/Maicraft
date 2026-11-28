# 发布指南

本文档定义 Maicraft 的发布操作流程：版本号规则、CHANGELOG 写法与发布步骤。

## 分支角色

```
task/*  ──收口──▶  dev（开发主线）──发布──▶  tag vX.Y.Z + main 快进
```

- **dev 是开发主线**：平时不动版本号，版本号停在上一发布版本。
- **tag `vX.Y.Z` 是发布事实**：带说明的 tag，指向发布提交；`v` 只是 tag 名的前缀，不是版本号的一部分。
- **main 是发布线**：只在发布时用 `--ff-only` 快进到 dev，不产生自己的提交。

## 版本号规则（语义化版本）

| 升什么位 | 判定 |
|---|---|
| 补版本号 `X.Y.z` | 已有功能的修复 |
| 次版本号 `x.Y.0` | 新增成块的功能，配置向后兼容 |
| 主版本号 `X.0.0` | 不兼容的改动 |

版本号只在发布那一刻升（改 `pyproject.toml` 的 `version`）。

## CHANGELOG 写法

根目录 `CHANGELOG.md`，发布时手写。条目写给不写代码的使用者看：写使用者可感知的行为与结果，一条一件事，用短句。素材从 `git log v上一版..HEAD --oneline` 的提交提炼归类。格式：

```markdown
## [X.Y.Z] - 日期

### 分组名
- （该方面的变化，按需分组，无变化的方面不出现在本版）
```

## 发布步骤

1. **检查**（在 dev 上）：`uv run pytest -q` 全绿，`uv run ruff check` 与 `uv run ruff format --check` 通过。
2. **写 CHANGELOG**：`CHANGELOG.md` 顶部新增本版本条目。
3. **定版提交**：升 `pyproject.toml` 的 `version`，与 CHANGELOG 合为一个提交：`chore: 发布 X.Y.Z`。
4. **打 tag**：`git tag -a vX.Y.Z -m "<一句话发布摘要>"`。
5. **main 快进**：`git switch main && git merge --ff-only dev && git switch dev`。
6. **推送**：推送分支与 tag（推送需要仓库管理员同意）。

## 节奏

不定期发版：一批改动收口、需要给使用者一个稳定点时发布。改动收口并入 dev 不等于发布，发布永远是显式动作。
