"""skills/ 目录与 AgentScope skill 注册单测：SKILL.md 可被加载、Toolkit 暴露
Skill 元工具并产出系统提示注入块。风格沿用 test_agent_tools.py——asyncio.run
包同步用例。"""

import asyncio
from pathlib import Path

from agentscope.skill import LocalSkillLoader
from agentscope.tool import Toolkit

from app.agent_skills import SKILL_LOADER

MEDICAL = "medical-literature-search"


def load_skills() -> dict[str, object]:
    return {skill.name: skill for skill in asyncio.run(SKILL_LOADER.list_skills())}


class TestSkillLoading:
    def test_medical_skill_discovered(self):
        assert MEDICAL in load_skills()

    def test_frontmatter_and_body_complete(self):
        skill = load_skills()[MEDICAL]
        assert skill.description.strip()
        # 正文须是可独立执行的完整流程：指明检索工具与降级话术
        assert "medical_rag_search" in skill.markdown
        assert "本地文献库未找到相关依据" in skill.markdown

    def test_description_covers_triggers(self):
        # 纯 skill 路由：description 是医学问题触发技能的唯一入口，
        # 关键领域词缺失会导致模型漏读技能、跳过文献检索
        description = load_skills()[MEDICAL].description
        for word in ("疾病", "症状", "诊断", "治疗", "用药", "健康"):
            assert word in description, f"触发词缺失：{word}"


class TestToolkitIntegration:
    def test_skill_viewer_and_instructions(self):
        async def scenario():
            toolkit = Toolkit(skills_or_loaders=[SKILL_LOADER])
            schemas = await toolkit.get_tool_schemas()
            instructions = await toolkit.get_skill_instructions()
            return schemas, instructions

        schemas, instructions = asyncio.run(scenario())
        names = {schema["function"]["name"] for schema in schemas}
        # 有技能注册时框架自动暴露只读元工具 Skill，供模型按需读取全文
        assert "Skill" in names
        assert instructions is not None
        assert "<agent-skills>" in instructions
        assert MEDICAL in instructions

    def test_scan_subdir_finds_nested_skill_md(self, tmp_path):
        # agent_skills.py 用 scan_subdir=True 支撑「一个技能一个子目录」布局
        (tmp_path / "demo-skill").mkdir()
        (tmp_path / "demo-skill" / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: 演示技能\n---\n# 演示",
            encoding="utf-8",
        )

        async def scenario():
            loader = LocalSkillLoader(directory=str(tmp_path), scan_subdir=True)
            return await loader.list_skills()

        skills = asyncio.run(scenario())
        assert [skill.name for skill in skills] == ["demo-skill"]

    def test_empty_dir_degrades_to_no_skills(self, tmp_path):
        async def scenario():
            loader = LocalSkillLoader(directory=str(tmp_path / "missing"), scan_subdir=True)
            return await loader.list_skills()

        assert asyncio.run(scenario()) == []
