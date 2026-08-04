from __future__ import annotations

from io import StringIO
from pathlib import Path

from polybot.config import Settings
from polybot.config_check import EX_CONFIG, check_configuration


def test_config_check_prints_structured_errors_without_inputs_or_secrets() -> None:
    secret = "sk-super-secret-must-never-appear"
    errors = StringIO()
    success = StringIO()

    result = check_configuration(
        settings_factory=lambda: Settings(
            _env_file=None,
            component="api",
            ai_api_key=secret,
        ),
        error_stream=errors,
        success_stream=success,
    )

    output = errors.getvalue()
    assert result == EX_CONFIG
    assert output.startswith("POLYBOT_CONFIG_ERROR field=settings message=")
    assert "POLYBOT_AI_API_KEY" in output
    assert secret not in output
    assert "input_value" not in output
    assert success.getvalue() == ""


def test_config_check_prints_each_invalid_field_on_its_own_line() -> None:
    errors = StringIO()

    result = check_configuration(
        settings_factory=lambda: Settings(
            _env_file=None,
            scan_interval_seconds=1,
            market_limit=0,
        ),
        error_stream=errors,
        success_stream=StringIO(),
    )

    lines = errors.getvalue().splitlines()
    assert result == EX_CONFIG
    assert len(lines) == 2
    assert any("field=scan_interval_seconds " in line for line in lines)
    assert any("field=market_limit " in line for line in lines)
    assert all(line.startswith("POLYBOT_CONFIG_ERROR ") for line in lines)


def test_config_check_success_output_contains_only_non_secret_runtime_metadata() -> None:
    secret = "personal-key-must-stay-hidden"
    errors = StringIO()
    success = StringIO()

    result = check_configuration(
        settings_factory=lambda: Settings(
            _env_file=None,
            personal_mode=True,
            component="all",
            account_id="11111111-1111-4111-8111-111111111111",
            ai_api_key=secret,
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
        ),
        error_stream=errors,
        success_stream=success,
    )

    assert result == 0
    assert errors.getvalue() == ""
    assert success.getvalue() == ("POLYBOT_CONFIG_OK component=all mode=paper personal_mode=true\n")
    assert secret not in success.getvalue()


def test_docker_roles_run_config_check_after_exporting_their_component() -> None:
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.count("python -m polybot.config_check") == 3
    assert "api) export POLYBOT_COMPONENT=api; python -m polybot.config_check" in dockerfile
    assert "export POLYBOT_PERSONAL_MODE=true; python -m polybot.config_check" in dockerfile
    assert (
        "export POLYBOT_WORKER_EXECUTION_MODEL=tenant_queue; "
        "python -m polybot.config_check" in dockerfile
    )


def test_personal_templates_use_explicit_owner_placeholder_and_infer_ai_provider() -> None:
    backend = Path(__file__).parents[1]
    for template in (
        backend / ".env.example",
        backend / "deploy" / "personal.env.example",
    ):
        contents = template.read_text(encoding="utf-8")
        assert "POLYBOT_ACCOUNT_ID=REPLACE_WITH_SUPABASE_AUTH_USER_UUID" in contents
        assert "POLYBOT_ACCOUNT_ID=00000000-0000-0000-0000-000000000000" not in contents
        assert "POLYBOT_AI_PROVIDER=" not in contents
        assert "POLYBOT_CUSTOM_AI_ALLOWED_HOSTS=" not in contents
