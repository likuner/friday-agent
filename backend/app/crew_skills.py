"""技能加载（纯自研，CrewAI 运行时）：每个子目录一个 SKILL.md。

复刻原 AgentScope skill 机制的行为：技能清单（name/description）注入
system prompt 的 <agent-skills> 块，模型经只读元工具 Skill 按需读取全文
（渐进式披露，技能内容不再占用每轮提示词）。新增技能=在 skills/ 下加
目录，无需改代码。mtime 缓存跨请求共享；目录缺失仅告警不注入。
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import frontmatter
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

logger = logging.getLogger("friday.crew_skills")

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

# <agent-skills> 块的头部说明 + 技能条目模板（AgentScope 同款渐进披露语义：
# 清单常驻提示词，全文按需经 Skill 元工具读取）
_SKILL_HEADER = (
    "可用技能清单如下。技能不是工具，不要把技能名当函数调用：\n"
    "需要使用某项技能时，必须先用 `Skill` 工具（入参 skill=<技能名>）读取该技能的完整说明，再严格按说明执行。"
)
_SKILL_ENTRY = "- {name}：{description}（目录：{dir}）"


@dataclass
class Skill:
    """一个已加载的技能：frontmatter 元数据 + 全文 markdown。"""

    name: str
    description: str
    dir: Path
    markdown: str
    updated_at: float


# name → (文件 mtime, Skill)：内容未变时跨请求复用解析结果
_cache: dict[str, tuple[float, Skill]] = {}


def list_skills(directory: Path | None = None) -> list[Skill]:
    """扫描技能目录（一个子目录一个 SKILL.md）；缺 name/description 的跳过并告警。"""
    root = directory or SKILLS_DIR
    if not root.is_dir():
        logger.warning("技能目录不存在，本轮不注入技能：%s", root)
        return []
    skills: list[Skill] = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        mtime = skill_md.stat().st_mtime
        cached = _cache.get(str(skill_md))
        if cached and cached[0] == mtime:
            skills.append(cached[1])
            continue
        try:
            post = frontmatter.load(skill_md)
        except OSError as exc:
            logger.warning("技能文件读取失败，跳过：%s（%s）", skill_md, exc)
            continue
        name = str(post.metadata.get("name", "")).strip()
        description = str(post.metadata.get("description", "")).strip()
        if not name or not description:
            logger.warning("技能缺少 name/description，跳过：%s", skill_md)
            continue
        skill = Skill(
            name=name,
            description=description,
            dir=skill_md.parent,
            markdown=post.content.strip(),
            updated_at=mtime,
        )
        _cache[str(skill_md)] = (mtime, skill)
        skills.append(skill)
    return skills


def skill_instructions(skills: list[Skill] | None = None) -> str:
    """渲染注入 system prompt 的 <agent-skills> 块；无技能时返回空串。"""
    skills = list_skills() if skills is None else skills
    if not skills:
        return ""
    entries = "\n".join(
        _SKILL_ENTRY.format(name=s.name, description=s.description, dir=s.dir)
        for s in skills
    )
    return f"<agent-skills>\n{_SKILL_HEADER}\n\n{entries}\n</agent-skills>"


def probe_skills() -> None:
    """技能注册探测：目录配错导致技能悄悄消失时，这条日志是主线索。"""
    skill_names = [skill.name for skill in list_skills()]
    logger.info("节点[Skill注册] dir=%s skills=%s", SKILLS_DIR, skill_names)


class _SkillArgs(BaseModel):
    skill: str


class SkillTool(BaseTool):
    """只读技能元工具：入参技能名，返回该技能 SKILL.md 全文（渐进披露）。"""

    skills: dict = Field(default_factory=dict, exclude=True)

    def _run(self, skill: str) -> str:
        target = self.skills.get(skill.strip())
        if target is None:
            available = "、".join(self.skills) or "（无）"
            return f"Error: 技能 {skill} 不存在。可用技能：{available}"
        return target.markdown


def skill_tool(skills: list[Skill] | None = None) -> SkillTool | None:
    """构建 Skill 元工具；无技能时返回 None（不注册）。"""
    skills = list_skills() if skills is None else skills
    if not skills:
        return None
    return SkillTool(
        name="Skill",
        description=(
            "读取指定技能的完整说明（SKILL.md 全文）。技能不是工具："
            "需要使用某项技能时，必须先用本工具读取其完整说明，再严格按说明执行。"
        ),
        skills={s.name: s for s in skills},
    )
