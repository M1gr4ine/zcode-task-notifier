"""显式安装技能的 frontmatter、触发边界和编排顺序测试。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import pytest


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def parse_skill(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, raw_frontmatter, body = text.split("---\n", 2)
    values: dict[str, object] = {}
    description_lines: list[str] = []
    in_description = False
    for line in raw_frontmatter.splitlines():
        if line.startswith("description:"):
            in_description = True
            continue
        if in_description and (line.startswith("  ") or not line.strip()):
            description_lines.append(line.strip())
            continue
        in_description = False
        if line.startswith("name:"):
            values["name"] = line.split(":", 1)[1].strip()
        elif line.startswith("user-invocable:"):
            values["user-invocable"] = line.split(":", 1)[1].strip().casefold() == "true"
    values["description"] = " ".join(description_lines).strip()
    return values, body


@dataclass(frozen=True)
class SkillScenario:
    triggered: bool
    required_behavior: str


def evaluate_skill_scenario(prompt: str, *, active_skill: bool) -> SkillScenario:
    """用可重复的静态模型核对显式触发和正文硬约束。"""
    skill = repo_root() / "skills/zcode-task-notifier-install/SKILL.md"
    frontmatter, body = parse_skill(skill)
    explicit = (
        prompt.strip() == "/zcode-task-notifier-install"
        or bool(re.search(r"(?<![A-Za-z0-9-])zcode-task-notifier-install(?![A-Za-z0-9-])", prompt))
        or active_skill
    )
    return SkillScenario(
        triggered=explicit,
        required_behavior=body,
    )


def test_install_skill_frontmatter_is_explicit_and_user_invocable():
    skill = repo_root() / "skills/zcode-task-notifier-install/SKILL.md"
    frontmatter, body = parse_skill(skill)
    assert frontmatter["name"] == "zcode-task-notifier-install"
    assert frontmatter["user-invocable"] is True
    assert "/zcode-task-notifier-install" in str(frontmatter["description"])
    assert "仅当用户明确点名" in str(frontmatter["description"])
    assert "安装器" not in str(frontmatter["description"])
    assert "计划任务" not in body or "不承载常驻监控" in body


@pytest.mark.parametrize(
    ("prompt", "active_skill", "should_trigger", "expected_step"),
    [
        ("帮我安装任务通知器", False, False, "不触发"),
        ("/zcode-task-notifier-install", False, True, "微信机器人引导"),
        ("请使用 zcode-task-notifier-install", False, True, "微信机器人引导"),
        ("不要使用 zcode-task-notifier-installing", False, False, "不触发"),
        ("请帮我安装通知", False, False, "不触发"),
        ("没有可用微信机器人", True, True, "停下教学"),
        ("我已启用微信机器人", True, True, "继续复检"),
        ("复检仍失败", True, True, "再次停下"),
        ("Codex 不需要监控", True, True, "仅配置 ZCode"),
    ],
)
def test_install_skill_scenarios(prompt, active_skill, should_trigger, expected_step):
    scenario = evaluate_skill_scenario(prompt, active_skill=active_skill)
    assert scenario.triggered is should_trigger
    assert expected_step in scenario.required_behavior


def test_install_skill_preserves_order_and_never_owns_monitor_loop():
    _, body = parse_skill(repo_root() / "skills/zcode-task-notifier-install/SKILL.md")
    order = (
        "微信机器人引导",
        "停下教学",
        "继续复检",
        "是否同时监控 Codex",
        "仅配置 ZCode",
        "scripts/install.ps1",
        "doctor",
    )
    positions = [body.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert "不承载常驻监控" in body
    assert "读取会话正文" in body
    assert "禁止" in body
    assert "不要读取、打印、解密或上传" in body
    assert "绝不解密、打印、上传或持久化凭据值" in body


def test_install_skill_has_no_local_path_or_credential_instruction():
    _, body = parse_skill(repo_root() / "skills/zcode-task-notifier-install/SKILL.md")
    assert re.search(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]", body) is None
    assert "不要读取、打印、解密或上传" in body


def test_install_skill_does_not_run_a_second_baseline_after_installer():
    """安装器已在注册前基线，技能侧只能复检 doctor。"""
    _, body = parse_skill(repo_root() / "skills/zcode-task-notifier-install/SKILL.md")
    assert "baseline" not in body.casefold()
    assert "基线" not in body
    assert "安装器完成后" in body
    assert "`doctor`" in body


def test_install_skill_marks_real_runtime_end_to_end_as_pending():
    """pytest 只校验技能契约，不能伪造 ZCode 运行时到达。"""
    _, body = parse_skill(repo_root() / "skills/zcode-task-notifier-install/SKILL.md")
    assert "真实端到端验证仍待" in body
    assert "静态检查和合成测试不能替代" in body
