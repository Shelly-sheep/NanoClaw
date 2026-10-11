"""技能加载器：SkillsLoader。

从 skills_dir 下扫描子目录中的 SKILL.md，解析 YAML frontmatter
提取 name / description，供 Agent 在系统提示词中展示技能目录。
Agent 决定使用某项技能时，再调用 load_skill() 读取详细指南正文。

SKILL.md 格式：
    ---
    name: web_scraping
    description: 抓取网页并提取结构化数据
    ---
    # 详细使用指南...
"""

import os

import yaml


class SkillsLoader:
    """扫描并加载 skills_dir 下的技能定义文件。

    - 技能以子目录形式存在，每个子目录下一个 SKILL.md；
    - SKILL.md 用 YAML frontmatter 存元信息（name/description），
      正文是给 LLM 阅读的使用指南；
    - skills_dir 不存在时所有方法安全降级（返回空）。
    """

    def __init__(self, skills_dir: str = "skills") -> None:
        self.skills_dir = skills_dir

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict, str]:
        """解析 SKILL.md 的 YAML frontmatter，返回 (元信息, 正文)。

        frontmatter 以 "---\\n" 开头，以下一个 "---" 行结束。
        没有则返回 (空字典, 原文)。
        """
        if not content.startswith("---\n"):
            return {}, content

        # 找第二个 "---" 分隔行
        end = content.find("\n---", 4)
        if end == -1:
            # 有开头但没结尾，当作无 frontmatter 处理
            return {}, content

        yaml_text = content[4:end]
        body = content[end + 4:].lstrip("\n")

        try:
            metadata = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError:
            metadata = {}

        if not isinstance(metadata, dict):
            metadata = {}

        return metadata, body

    def build_skills_summary(self) -> str:
        """构建技能目录摘要字符串，供系统提示词注入。

        无技能或目录不存在时返回空字符串（调用方据此跳过注入）。
        """
        if not os.path.isdir(self.skills_dir):
            return ""

        entries = self.list_skills()
        if not entries:
            return ""

        lines = [
            "你有以下技能可用。当你需要使用某项技能时，"
            "请先用 read_file 工具读取对应的 SKILL.md 文件获取详细指南。\n\n"
            "可用技能：\n"
        ]
        for skill in entries:
            rel_path = os.path.relpath(skill["path"], self.skills_dir)
            # rel_path 形如 "web_scraping/SKILL.md"，取目录部分
            dir_part = os.path.dirname(rel_path) or skill["name"]
            lines.append(
                f"- {skill['name']} ({dir_part}/SKILL.md)：{skill['description']}\n"
            )
        return "".join(lines)

    def load_skill(self, name: str) -> str | None:
        """读取指定技能的 SKILL.md 正文（去掉 frontmatter）。

        找不到文件时返回 None。
        """
        skill_path = os.path.join(self.skills_dir, name, "SKILL.md")
        if not os.path.isfile(skill_path):
            return None

        try:
            with open(skill_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            return None

        _, body = self._parse_frontmatter(content)
        return body

    def list_skills(self) -> list[dict]:
        """返回所有已发现技能的列表 [{name, description, path}]。

        - 跳过非目录条目和没有 SKILL.md 的子目录；
        - name / description 缺失时用空字符串兜底。
        """
        if not os.path.isdir(self.skills_dir):
            return []

        skills: list[dict] = []
        for entry in sorted(os.listdir(self.skills_dir)):
            entry_path = os.path.join(self.skills_dir, entry)
            if not os.path.isdir(entry_path):
                continue

            skill_md = os.path.join(entry_path, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue

            try:
                with open(skill_md, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue

            metadata, _ = self._parse_frontmatter(content)
            skills.append(
                {
                    "name": metadata.get("name", entry),
                    "description": metadata.get("description", ""),
                    "path": skill_md,
                }
            )

        return skills
