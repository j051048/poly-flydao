# 第三方组件与许可记录

本文档记录项目直接使用且参与核心运行时或研究边界的主要上游组件。完整、逐版本的 SBOM 由 CI 自动生成（Python: `cyclonedx-py`；Node: `npm ls --all --json`），本页只维护"人工可读"的许可摘要。

## 运行时直接依赖（backend）

| 上游仓库 | 锁定基线 | 许可 | 实际角色 |
|---|---|---|---|
| [Polymarket/py-sdk](https://github.com/Polymarket/py-sdk) | `polymarket-client==0.2.0` | MIT | 行情、签名、下单、撤单、对账 |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | `research` extra（Python 分支解析） | LGPL-3.0-or-later | 仅可选研究依赖，不进生产镜像 |
| [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph) | `langgraph==1.2.9` | MIT | 主预测 + skeptic + 聚合图 |
| [BerriAI/litellm](https://github.com/BerriAI/litellm) | `litellm==1.93.0` | 核心 MIT | OpenAI-compatible provider |
| [openai/openai-python](https://github.com/openai/openai-python) | `openai==2.46.0` | Apache-2.0 | Structured Outputs + Web Search evidence |
| [supabase/supabase-py](https://github.com/supabase/supabase-py) | `supabase==2.31.0` | MIT | 持久化、lease/fencing、RLS |
| [PrefectHQ/prefect](https://github.com/PrefectHQ/prefect) | `prefect==3.7.8` | Apache-2.0 | 有限 paper/shadow 离线入口 |
| [john-kurkowski/tldextract](https://github.com/john-kurkowski/tldextract) | `>=5.3,<6` | BSD-3-Clause | 证据来源 eTLD+1 去重 |

## 运行时直接依赖（apps/web）

| 包 | 许可 | 实际角色 |
|---|---|---|
| Next.js / React / React DOM | MIT | 控制台框架 |
| Supabase SSR / supabase-js | MIT | 登录与会话 |
| Recharts | MIT | 净值曲线与图表 |
| Vitest / Testing Library / jsdom（dev） | MIT | 前端测试 |

## 约束与隔离

- NautilusTrader（LGPL-3.0-or-later）仅存在于 `research` optional extra，默认 `uv sync` 与生产 Docker 镜像不安装；对其的任何修改须单独按 LGPL 履行义务。
- LiteLLM 仅使用 core MIT 范围，不复制或 vendor 其 `enterprise/` 内容。
- 本仓库自身代码采用 MIT（见 [LICENSE](../LICENSE)）。
- 许可记录不等于 SBOM；正式发布前以 CI 生成的 `sbom.python.json` / `sbom.node.json` 为准。
