"""The wall-time model against the measured check points (design 3.7)."""


def _proof_summary(transfer):
    return {"domains": [{"grid_id": 1, "nx": 100, "ny": 80, "nz": 49, "dt_s": 15.0}], "run_seconds": 10800,
            "physics_profile": "thompson-mp8-ysu-mm5-noah-rte-rrtmgp-v1", "download_transfer_bytes": transfer,
            "forcing_times": 4}


def test_cold_and_warm_within_factor(pkg):
    from hermes_woof_plugin.hermes_woof import estimate as est
    cold = est.estimate(_proof_summary(4_666_400_000), "NVIDIA GeForce RTX 4090", products=4, kernels_warm=False)
    warm = est.estimate(_proof_summary(0), "NVIDIA GeForce RTX 4090", products=4, kernels_warm=True)
    assert 217 / 1.5 <= cold["total_s"] <= 217 * 1.5, cold
    assert 62 / 1.5 <= warm["total_s"] <= 62 * 1.5, warm
    assert cold["label"] == "estimate" and cold["basis"]


def test_card_lookup(pkg):
    from hermes_woof_plugin.hermes_woof import estimate as est
    assert est.card_factor("NVIDIA GeForce RTX 5090")["relative"] == 1.0
    assert est.card_factor("NVIDIA RTX PRO 6000 Blackwell")["card"] == "RTX PRO 6000"
    low = est.card_factor("NVIDIA GeForce RTX 3080")
    assert low["confidence"] == "low" and low["card"]
    assert est.card_factor("16gb")["confidence"] == "low"


def test_full_stack_is_slower(pkg):
    from hermes_woof_plugin.hermes_woof import estimate as est
    base = est.physics_seconds_per_cell_step("thompson-mp8-ysu-mm5-noah-rte-rrtmgp-v1", 49)
    full = est.physics_seconds_per_cell_step("thompson-mp8-mynn-mynn-ruc-rrtmg-legacy", 59)
    assert abs(1 / full - 28e6) / 28e6 < 0.01
    assert abs(1 / base - 83e6) < 1


def test_calibration_corrects(pkg):
    from hermes_woof_plugin.hermes_woof import estimate as est
    s = _proof_summary(0)
    first = est.estimate(s, "RTX 4090", kernels_warm=True)
    cal = {"NVIDIA GeForce RTX 4090": [{"stages": {"prepare_s": first["parts"]["prepare_s"] * 2},
                                        "estimate_parts": first["parts"],
                                        "integration_wall_s": first["parts"]["forecast_s"] * 2}] * 3}
    second = est.estimate(s, "RTX 4090", calibration=cal, kernels_warm=True)
    assert second["basis"].startswith("this machine, 3 runs")
    assert second["total_s"] > first["total_s"]
