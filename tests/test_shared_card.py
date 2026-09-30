"""A local model served from the same card: plans are sized for the memory it leaves."""
from __future__ import annotations

from types import SimpleNamespace


class _X:
    kind = "local"

    def __init__(self, outputs):
        self.outputs = outputs

    def run(self, argv, **kw):
        from hermes_woof_plugin.hermes_woof.executor import Result
        key = " ".join(argv[:2])
        text = self.outputs.get(key, "")
        return Result(rc=0 if key in self.outputs else 1, out=text.encode(), err="", seconds=0.0)


def _host(outputs, reserve=""):
    return SimpleNamespace(x=_X(outputs), settings=SimpleNamespace(vram_reserve_gib=reserve), label="t")


SMI = {
    "nvidia-smi --query-gpu=index,uuid": "0, GPU-aaa\n",
    "nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory":
        "GPU-aaa, 4242, /opt/llm/bin/llama-server, 17500\nGPU-aaa, 777, /usr/bin/python3, 900\n",
    "ps -o": "4242 /opt/llm/bin/llama-server -m q.gguf\n 777 python3 some_other_job.py\n",
}


def test_resident_model_is_measured_and_reserved(pkg):
    from hermes_woof_plugin.hermes_woof import tools
    devices = [{"index": 0, "name": "NVIDIA GeForce RTX 4090", "memory_total_gib": 23.99, "memory_free_gib": 5.6}]
    host = _host(SMI)
    tools._resident_models(host, devices)
    d = devices[0]
    assert [m["pid"] for m in d["resident_models"]] == [4242]  # the other python job is not a model server
    assert d["resident_gib"] == 17.09
    gib, why = tools._vram_reserve(host, d)
    assert 17.5 < gib < 17.7 and "llama-server" in why


def test_plan_card_is_the_part_the_model_leaves(pkg, monkeypatch):
    from hermes_woof_plugin.hermes_woof import tools
    gpu = {"index": 0, "name": "NVIDIA GeForce RTX 4090", "memory_total_gib": 23.99, "memory_free_gib": 5.6,
           "resident_models": [{"pid": 1, "name": "llama-server", "used_gib": 17.09}], "resident_gib": 17.09}
    monkeypatch.setattr(tools, "doctor_data", lambda host, **kw: {"mode": "full", "gpus": [gpu]})
    warnings: list = []
    card = tools._target_card(_host({}), {}, warnings)
    assert card["vram_gib"] == 6 and card["intent"] == {"vram_gib": 6}
    assert "llama-server" in warnings[0] and "6 GiB" in warnings[0]


def test_setting_overrides_and_no_model_means_whole_card(pkg, monkeypatch):
    from hermes_woof_plugin.hermes_woof import tools
    gpu = {"index": 0, "name": "RTX", "memory_total_gib": 32.0, "memory_free_gib": 31.5}
    monkeypatch.setattr(tools, "doctor_data", lambda host, **kw: {"mode": "full", "gpus": [gpu]})
    w: list = []
    assert tools._target_card(_host({}), {}, w)["vram_gib"] == 32 and w == []
    assert tools._target_card(_host({}, reserve="10"), {}, w)["vram_gib"] == 22
