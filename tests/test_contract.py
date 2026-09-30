"""The tool contract: manifest, schemas.json and registered handlers agree."""
import json
import re

from conftest import ROOT


def _provides_tools():
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    m = re.search(r"provides_tools:\s*\[(.*?)\]", text, re.S)
    return [t.strip() for t in m.group(1).replace("\n", " ").split(",") if t.strip()]


def test_provides_tools_equals_schemas_equals_handlers(pkg):
    schemas = json.loads((ROOT / "hermes_woof" / "schemas.json").read_text(encoding="utf-8"))
    names = [s["name"] for s in schemas]
    assert len(names) == 12
    assert _provides_tools() == names
    from hermes_woof_plugin.hermes_woof import tools
    assert list(tools.HANDLERS) == names


def test_schema_shapes():
    schemas = json.loads((ROOT / "hermes_woof" / "schemas.json").read_text(encoding="utf-8"))
    for s in schemas:
        assert set(s) == {"name", "description", "parameters"}
        assert len(s["name"]) < 64 and s["name"].startswith("woof_")
        assert s["parameters"]["type"] == "object"
        for key in s["parameters"].get("required", []):
            assert key in s["parameters"]["properties"]


def test_registration_uses_no_subprocess(pkg, plugin_env, monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("register() must not start a subprocess")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)

    class Ctx:
        def __init__(self):
            self.tools, self.skills, self.sections, self.cli = [], [], [], []

        def get_config(self, key, default=None):
            return default

        def set_config(self, key, value):
            pass

        def register_tool(self, **kw):
            self.tools.append(kw)

        def register_skill(self, name, path, description=""):
            self.skills.append((name, path))

        def register_system_prompt_section(self, id, content, **kw):
            self.sections.append((id, content))

        def register_cli_command(self, **kw):
            self.cli.append(kw)

        def inject_message(self, text, role="user"):
            return True

    ctx = Ctx()
    import hermes_woof_plugin
    hermes_woof_plugin.register(ctx)
    schemas = json.loads((ROOT / "hermes_woof" / "schemas.json").read_text(encoding="utf-8"))
    assert [t["name"] for t in ctx.tools] == [s["name"] for s in schemas]
    assert all(t["toolset"] == "woof" and t["check_fn"]() is True for t in ctx.tools)
    assert ctx.skills[0][0] == "woof" and ctx.skills[0][1].exists()
    assert ctx.sections == [("woof.pointer", "WOOF weather simulation tools (woof_*) are available. "
                             "Before planning or launching a run, read the skill woof:woof with skill_view.")]
    assert ctx.cli[0]["name"] == "woof"


def test_skill_frontmatter_and_size():
    text = (ROOT / "skills" / "woof" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: woof\n")
    order = ["## When to Use", "## Prerequisites", "## How to Run", "## Quick Reference", "## Procedure",
             "## Pitfalls", "## Verification"]
    pos = [text.index(h) for h in order]
    assert pos == sorted(pos)
    assert len(text) / 4 < 6000  # under 6,000 tokens at about 4 characters per token
    refs = list((ROOT / "skills" / "woof" / "references").glob("*.md"))
    assert len(refs) == 10
    for ref in refs:
        assert len(ref.read_text(encoding="utf-8")) / 4 < 4000, ref.name


def test_model_sees_loosened_nested_objects_but_handlers_check_the_contract(pkg, plugin_env):
    from hermes_woof_plugin.hermes_woof import schemas, tools
    offered = {s["name"]: s for s in schemas.for_model()}
    contract = {s["name"]: s for s in schemas.load()}
    assert set(offered) == set(contract)
    intent = offered["woof_plan"]["parameters"]["properties"]["intent"]
    assert "properties" not in intent and "point" in intent["description"]
    assert offered["woof_doctor"] == contract["woof_doctor"]
    bad = tools.woof_plan({"intent": {"point": "1,2", "nx": 100}})
    assert '"invalid"' in bad
