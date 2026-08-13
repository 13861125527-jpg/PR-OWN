# RepoSage

GitHub PR 智能代码审查系统（Phase 0 工程骨架）。

> 完整架构设计见 [`docs/architecture/`](docs/architecture/)（00–16）。
> 当前为 Phase 0：Python 工程骨架、Domain 模型、Provider 协议与 Fake、配置加载、SQLite 基础、最小评测 Runner、基础 CI。

## 状态

- 架构：冻结中（`architecture-v1`，二轮审查通过）
- 阶段：**Phase 0**（工程骨架）
- 语言：Python ≥ 3.12（CI 验证 3.12/3.13；本地开发以 3.12/3.13 为主，3.14 亦可）

## 快速开始

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
ruff check .
mypy reposage
```

## 目录

```text
reposage/
├── app/          入口层（CLI 占位，V1 实现）
├── domain/       纯领域模型（无 IO 依赖）
├── providers/    Git / LLM Provider（base 协议 + Fake）
├── review/       V1 策略占位
├── tools/        V3 工具占位
├── publishing/   发布层占位
├── memory/       记忆层占位
├── observability/ 可观测占位
├── config/       配置加载（Pydantic + YAML）
├── prompts/      Prompt 目录占位
├── evals/        最小评测 Runner
└── storage/      SQLite 基础
```

## 设计契约（本阶段实现依据）

- Domain 模型字段与约束：`05-domain-data-design.md`
- Provider 协议与 Fake：`03-container-component-architecture.md` / `11-observability-evaluation-testing.md`
- 配置层级：`09-prompts-models-config.md` §6
- SQLite 概念模型：`05-domain-data-design.md` §6
- Phase 0 任务卡：`12-roadmap-adrs.md` §2
