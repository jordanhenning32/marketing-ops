from pathlib import Path

import compliance


def test_compliance_blocks_profit_and_guarantee_claims(tmp_path, monkeypatch):
    monkeypatch.setattr(compliance, "CONFIG_DIR", tmp_path / "config")
    asset = tmp_path / "01-x-thread.md"
    asset.write_text("Guaranteed profit and never lose with this secret strategy.", encoding="utf-8")

    result = compliance.check_asset(asset)

    assert result.status == "block"
    assert result.risk_level == "high"
    assert any("banned phrase" in reason for reason in result.reasons)


def test_compliance_passes_safe_risk_management_language(tmp_path, monkeypatch):
    monkeypatch.setattr(compliance, "CONFIG_DIR", tmp_path / "config")
    asset = tmp_path / "01-x-thread.md"
    asset.write_text(
        "Move risk rules closer to execution. Tools and education only. No financial advice.",
        encoding="utf-8",
    )

    result = compliance.check_asset(asset)

    assert result.status == "pass"
    assert result.risk_level == "low"


def test_campaign_folder_writes_machine_readable_result(tmp_path, monkeypatch):
    monkeypatch.setattr(compliance, "CONFIG_DIR", tmp_path / "config")
    (tmp_path / "01-x-thread.md").write_text("Safe risk-control workflow language.", encoding="utf-8")

    result = compliance.check_campaign_folder(tmp_path)

    assert result.passed
    output = tmp_path / "08-compliance-check.md"
    assert output.exists()
    assert "Status: pass" in output.read_text(encoding="utf-8")
