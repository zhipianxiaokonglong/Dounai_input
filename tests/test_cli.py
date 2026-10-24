"""Command-line smoke tests without spawning a server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dounai_input.cli import main


@pytest.fixture
def cli_config(tmp_path: Path) -> Path:
    config = tmp_path / "test.toml"
    config.write_text(
        f"""
data_dir = "{tmp_path.as_posix()}/data"
[server]
host = "127.0.0.1"
port = 8123
[providers.local-whisper]
enabled = true
""",
        encoding="utf-8",
    )
    return config


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert "0.1.0" in capsys.readouterr().out


def test_token_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["token"]) == 0
    token = capsys.readouterr().out.strip()
    assert len(token) >= 32


def test_models_list(cli_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(cli_config), "models", "list"]) == 0
    output = capsys.readouterr().out
    assert "Whisper" in output
    assert "未安装" in output


def test_providers_and_privacy_status(cli_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(cli_config), "providers", "list"]) == 0
    output = capsys.readouterr().out
    assert "offline" in output
    assert "dashscope" in output

    assert main(["--config", str(cli_config), "privacy", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is False
    assert payload["local_only"] is True


def test_privacy_consent_cli(cli_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            [
                "--config",
                str(cli_config),
                "privacy",
                "consent",
                "--accept",
                "--policy-version",
                "1.0",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["consent_granted"] is True
    assert payload["enabled"] is True


def test_consent_requires_explicit_accept(
    cli_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(cli_config), "privacy", "consent"]) == 2
    assert "accept" in capsys.readouterr().err.lower()


def test_data_export_and_delete(
    cli_config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from fastapi.testclient import TestClient

    from dounai_input.api import build_app
    from dounai_input.config import load_settings

    app = build_app(load_settings(cli_config))
    with TestClient(app) as client:
        assert client.post("/v1/inputs/text", json={"text": "cli export"}).status_code == 201

    output = tmp_path / "export.zip"
    assert (
        main(
            [
                "--config",
                str(cli_config),
                "data",
                "export",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert output.exists()
    assert output.stat().st_size > 0
    capsys.readouterr()

    assert main(["--config", str(cli_config), "data", "delete", "--yes"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deleted"] is True


def test_doctor(cli_config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(cli_config), "doctor"]) == 0
    output = capsys.readouterr().out
    assert "检查完成" in output


def test_unknown_model_is_a_clean_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["models", "install", "not-a-model", "--yes"]) == 2
    assert "Unknown local model" in capsys.readouterr().err
