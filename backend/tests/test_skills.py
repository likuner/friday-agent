"""skills/ 目录与技能加载单测：SKILL.md 可被发现、<agent-skills> 注入块与
Skill 元工具可用。自研加载器（crew_skills）复刻原 AgentScope 行为。"""

from pathlib import Path

from app.crew_skills import SkillTool, list_skills, skill_instructions, skill_tool

MEDICAL = "medical-literature-search"


def load_skills(directory: Path | None = None) -> dict[str, object]:
    return {skill.name: skill for skill in list_skills(directory)}


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

    def test_scan_subdir_finds_nested_skill_md(self, tmp_path):
        # 一个技能一个子目录布局
        (tmp_path / "demo-skill").mkdir()
        (tmp_path / "demo-skill" / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: 演示技能\n---\n# 演示",
            encoding="utf-8",
        )
        skills = list_skills(tmp_path)
        assert [skill.name for skill in skills] == ["demo-skill"]

    def test_missing_fields_skill_skipped(self, tmp_path):
        (tmp_path / "bad-skill").mkdir()
        (tmp_path / "bad-skill" / "SKILL.md").write_text(
            "---\nname: bad-skill\n---\n# 缺 description", encoding="utf-8"
        )
        assert list_skills(tmp_path) == []

    def test_empty_dir_degrades_to_no_skills(self, tmp_path):
        assert list_skills(tmp_path / "missing") == []


class TestSkillInjection:
    def test_instructions_block(self):
        instructions = skill_instructions()
        assert "<agent-skills>" in instructions
        assert MEDICAL in instructions
        assert "medical_rag_search" not in instructions  # 全文不占提示词，只列清单

    def test_no_skills_no_block(self, tmp_path):
        assert skill_instructions(list_skills(tmp_path / "missing")) == ""

    def test_skill_tool_reads_full_markdown(self):
        tool = skill_tool()
        assert isinstance(tool, SkillTool)
        body = tool._run(skill=MEDICAL)
        assert "medical_rag_search" in body

    def test_skill_tool_unknown_name_errors(self):
        tool = skill_tool()
        assert tool._run(skill="nope").startswith("Error")

    def test_no_skills_no_tool(self, tmp_path):
        assert skill_tool(list_skills(tmp_path / "missing")) is None
