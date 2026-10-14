"""技能加载（AgentScope 原生 skill 机制）：每个子目录一个 SKILL.md。

框架把技能清单（name/description）自动注入 system prompt，模型经只读
元工具 Skill 按需读取全文（渐进式披露，技能内容不再占用每轮提示词）。
新增技能=在 skills/ 下加目录，无需改代码。
"""

import logging
from pathlib import Path

from agentscope.skill import LocalSkillLoader

logger = logging.getLogger("friday.agent_skills")

# scan_subdir=True 支持多技能并存；mtime 缓存跨请求共享，目录缺失仅告警
# 不注入，不影响对话
SKILL_LOADER = LocalSkillLoader(
    directory=str(Path(__file__).resolve().parents[1] / "skills"),
    scan_subdir=True,
)


async def probe_skills() -> None:
    """技能注册探测：Toolkit 对扫不到的目录只静默告警，这条日志是排查
    「skills/ 路径配错导致技能悄悄消失」的主线索（loader 内部不抛异常）。"""
    skill_names = [skill.name for skill in await SKILL_LOADER.list_skills()]
    logger.info("节点[Skill注册] dir=%s skills=%s", SKILL_LOADER.directory, skill_names)
