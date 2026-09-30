import json

from conftest import envelope


def test_ok_and_err_shapes(pkg):
    from hermes_woof_plugin.hermes_woof import envelope as env
    ok = json.loads(env.ok("woof_plan", "line", {"a": 1}, ["woof_launch"], ["w"]))
    assert ok == {"ok": True, "tool": "woof_plan", "summary": "line", "data": {"a": 1},
                  "next": ["woof_launch"], "warnings": ["w"]}
    er = json.loads(env.err("woof_launch", "no_gpu", "m", "r"))
    assert er == {"ok": False, "tool": "woof_launch", "error": {"kind": "no_gpu", "message": "m", "remedy": "r"}}
    assert json.loads(env.err("x", "weird", "m"))["error"]["kind"] == "engine_failed"


def test_cap(pkg):
    from hermes_woof_plugin.hermes_woof import envelope as env
    text = env.ok("woof_catalog", "big", {"rows": [{"n": i, "t": "x" * 200} for i in range(500)]})
    assert len(text) <= env.TEXT_CAP
    assert json.loads(text)["data"]["truncated"] is True


def test_multimodal(pkg, tmp_path):
    from hermes_woof_plugin.hermes_woof import envelope as env
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    e = env.ok_dict("woof_show", "maps", {"run_id": "r"})
    res = env.multimodal(e, [png], [str(png)], {"run_id": "r"}, True)
    assert res["_multimodal"] is True
    assert res["content"][0]["type"] == "text" and res["content"][1]["type"] == "image_url"
    assert res["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert f"MEDIA:{png}" in res["text_summary"]
    assert envelope(res)["tool"] == "woof_show"
    off = env.multimodal(e, [png], [str(png)], {}, False)
    assert isinstance(off, str) and f"MEDIA:{png}" in off and envelope(off)["ok"] is True


def test_text_only_results_do_not_ask_for_a_description(pkg, tmp_path):
    """When the images do not reach the model (text_summary, or images_to_model off), the text
    must not ask it to describe pictures it cannot see."""
    from hermes_woof_plugin.hermes_woof import envelope as env
    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n")
    e = env.ok_dict("woof_show", "1 WOOF maps for r. Describe what you see, then copy the MEDIA lines into your reply.")
    res = env.multimodal(e, [png], [str(png)])
    assert "Describe what you see" not in res["text_summary"] and "not visible" in res["text_summary"]
    assert "Describe what you see" in res["content"][0]["text"]  # the model that gets the images is asked
    off = env.multimodal(e, [png], [str(png)], send_images=False)
    assert "Describe what you see" not in off and "not visible" in off


def test_successful_results_carry_no_error_key(pkg):
    """Hermes reads the key "error" anywhere in a result as a failed call."""
    from hermes_woof_plugin.hermes_woof import envelope as env
    text = env.ok("woof_status", "r: fetching", {"run": {"state": "fetching", "error": None, "stage": "fetch"}})
    assert '"error"' not in text and '"stage": "fetch"' in text
    failed = env.ok("woof_status", "r: failed", {"run": {"state": "failed", "error": {"message": "x"}}})
    assert '"error"' in failed  # a real failure keeps its detail
