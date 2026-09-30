import json

from conftest import ROOT


def test_both_directions(pkg):
    from hermes_woof_plugin.hermes_woof import names
    raw = (ROOT / "hermes_woof" / "names.json").read_text(encoding="utf-8")
    table = names.table()
    # the stored file does not spell the former name; the loaded table has it whole
    former = table["former"]
    assert former and former.lower() not in raw.lower()
    assert table["files"][0][0] == former.lower() + "_wrf_"
    old_prefix, new_prefix = table["files"][0]
    assert names.media_name(old_prefix + "20260928_12z_f003.png") == new_prefix + "20260928_12z_f003.png"
    for old, new in table["text"]:
        assert new in names.to_display(f"x {old} y")
    # ids shown in WOOF wording map back to the engine's id before a command runs
    gid_old, gid_new = [p for p in table["text"] if p[0].endswith("_global_")][0]
    shown = names.to_display(gid_old + "t255_quickstart")
    assert shown == gid_new + "t255_quickstart"
    assert names.to_engine(shown) == gid_old + "t255_quickstart"
    assert names.to_engine("thompson-mp8") == "thompson-mp8"
    nested = names.display_obj({"k": [gid_old + "x", {"j": old_prefix + "a.png"}]})
    assert nested == {"k": [gid_new + "x", {"j": new_prefix + "a.png"}]}


def test_host_store_gets_the_filled_in_table(plugin_env):
    """The host-side runner reads names.json from the run store as plain pairs."""
    from hermes_woof_plugin.hermes_woof import names, settings
    from hermes_woof_plugin.hermes_woof.host import Host
    host = Host(settings.load())
    host.deploy()
    deployed = json.loads((plugin_env["runs"] / "names.json").read_text(encoding="utf-8"))
    assert deployed["files"] == names.table()["files"]
    assert deployed["text"] == names.table()["text"]
    assert not any("{" in a for a, _ in deployed["text"] + deployed["files"])
