from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.api.model_configs import (
    _verification_probe_tokens,
    create_model_config,
    set_default_model_config,
    test_model_config as run_model_config_test,
    update_model_config,
)
from app.db.models import ModelConfig, Tenant, User
from app.llm.schemas import ModelConfigCreateRequest, ModelConfigUpdateRequest
from app.security.encryption import encrypt_secret


def _db(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'model-api.db'}")
    SQLModel.metadata.create_all(engine)
    db = Session(engine)
    db.add(Tenant(id="tenant_a", name="Tenant A"))
    db.commit()
    return db


def _admin() -> User:
    return User(
        id="user_admin",
        tenant_id="tenant_a",
        username="admin",
        role="admin",
        password_hash="unused",
    )


def test_new_model_config_is_never_enabled_or_default(tmp_path) -> None:
    with _db(tmp_path) as db:
        created = create_model_config(
            ModelConfigCreateRequest(
                tenant_id="tenant_a",
                name="Chat",
                api_protocol="openai_chat_completions",
                api_key="secret",
                model="model-a",
                enabled=True,
                is_default=True,
            ),
            db=db,
            current_user=_admin(),
        )

        assert created.enabled is False
        assert created.is_default is False
        assert created.trust_status == "unverified"


def test_gemini_model_config_can_be_created(tmp_path) -> None:
    with _db(tmp_path) as db:
        created = create_model_config(
            ModelConfigCreateRequest(
                tenant_id="tenant_a",
                name="Gemini",
                api_protocol="gemini_generate_content",
                base_url="https://llm-center.modelbest.cn/llm",
                api_key="secret",
                model="gemini-2.5-flash",
            ),
            db=db,
            current_user=_admin(),
        )

        assert created.api_protocol == "gemini_generate_content"
        assert created.enabled is False
        assert created.is_default is False
        assert created.protocol_options == {}


def test_gemini_verification_reserves_tokens_for_visible_output() -> None:
    from app.llm.model_protocols import ModelApiProtocol

    assert _verification_probe_tokens(
        ModelApiProtocol.GEMINI_GENERATE_CONTENT, "stream", 32
    ) == 128
    assert _verification_probe_tokens(
        ModelApiProtocol.OPENAI_CHAT_COMPLETIONS, "stream", 32
    ) == 32


def test_security_change_invalidates_and_disables_legacy_config(tmp_path) -> None:
    with _db(tmp_path) as db:
        row = ModelConfig(
            id="model_a",
            tenant_id="tenant_a",
            name="Chat",
            api_key_encrypted=encrypt_secret("secret"),
            model="model-a",
            trust_status="legacy_trusted",
            enabled=True,
            is_default=True,
        )
        db.add(row)
        db.commit()

        updated = update_model_config(
            "model_a",
            ModelConfigUpdateRequest(tenant_id="tenant_a", model="model-b"),
            db=db,
            current_user=_admin(),
        )

        assert updated.enabled is False
        assert updated.is_default is False
        assert updated.trust_status == "unverified"
        assert updated.security_revision == 2


def test_disabling_default_clears_default_in_same_update(tmp_path) -> None:
    with _db(tmp_path) as db:
        db.add(
            ModelConfig(
                id="model_a",
                tenant_id="tenant_a",
                name="Chat",
                api_key_encrypted=encrypt_secret("secret"),
                model="model-a",
                trust_status="legacy_trusted",
                enabled=True,
                is_default=True,
            )
        )
        db.commit()

        updated = update_model_config(
            "model_a",
            ModelConfigUpdateRequest(tenant_id="tenant_a", enabled=False),
            db=db,
            current_user=_admin(),
        )

        assert updated.enabled is False
        assert updated.is_default is False


def test_unverified_config_cannot_become_default(tmp_path) -> None:
    with _db(tmp_path) as db:
        db.add(
            ModelConfig(
                id="model_a",
                tenant_id="tenant_a",
                name="Chat",
                api_key_encrypted=encrypt_secret("secret"),
                model="model-a",
                trust_status="unverified",
                enabled=False,
            )
        )
        db.commit()

        try:
            set_default_model_config("model_a", tenant_id="tenant_a", db=db)
        except HTTPException as exc:
            assert exc.status_code == 409
            assert exc.detail == "MODEL_CONFIG_VERIFICATION_REQUIRED"
        else:
            raise AssertionError("unverified config unexpectedly became default")


def test_read_returns_only_current_protocol_options(tmp_path) -> None:
    from app.api.model_configs import model_config_read

    row = ModelConfig(
        id="model_a",
        tenant_id="tenant_a",
        name="Chat",
        api_key_encrypted=encrypt_secret("secret"),
        model="model-a",
        protocol_options_json={
            "openai_chat_completions": {"thinking": {"type": "disabled"}},
            "anthropic_messages": {},
        },
    )

    assert model_config_read(row).protocol_options == {
        "thinking": {"type": "disabled"}
    }


def test_verification_runs_bounded_text_stream_and_json_probes(tmp_path, monkeypatch) -> None:
    calls = []

    class FakeClient:
        def __init__(self, config) -> None:  # noqa: ANN001
            calls.append(("init", config.max_output_tokens, config.timeout_seconds))

        def generate_text(self, _prompt, _payload):  # noqa: ANN001
            calls.append(("text",))
            return "ok"

        def generate_text_stream(self, _prompt, _payload):  # noqa: ANN001
            calls.append(("stream",))
            yield "ok"

        def generate_json(self, _prompt, _payload):  # noqa: ANN001
            calls.append(("json",))
            return {"ok": True}

    monkeypatch.setattr("app.api.model_configs.LLMClient", FakeClient)
    with _db(tmp_path) as db:
        db.add(
            ModelConfig(
                id="model_a",
                tenant_id="tenant_a",
                name="Chat",
                api_key_encrypted=encrypt_secret("secret"),
                model="model-a",
                trust_status="unverified",
                enabled=False,
            )
        )
        db.commit()

        result = run_model_config_test("model_a", tenant_id="tenant_a", db=db)

        assert result.success is True
        assert [item.id for item in result.capabilities] == ["text", "stream", "json"]
        assert calls == [
            ("init", 32, 25.0),
            ("text",),
            ("init", 32, 25.0),
            ("stream",),
            ("init", 128, 35.0),
            ("json",),
        ]


def test_initial_verification_can_atomically_activate_first_model(tmp_path, monkeypatch) -> None:
    _install_passing_verification_client(monkeypatch)
    with _db(tmp_path) as db:
        db.add(
            ModelConfig(
                id="model_a",
                tenant_id="tenant_a",
                name="Chat",
                api_key_encrypted=encrypt_secret("secret"),
                model="model-a",
                trust_status="unverified",
                enabled=False,
                is_default=False,
            )
        )
        db.commit()

        result = run_model_config_test(
            "model_a",
            tenant_id="tenant_a",
            activate_if_initial=True,
            db=db,
        )

        row = db.get(ModelConfig, "model_a")
        assert result.success is True
        assert result.activated is True
        assert row is not None
        assert row.trust_status == "verified"
        assert row.enabled is True
        assert row.is_default is True
        assert result.model is not None
        assert result.model.id == "model_a"
        assert result.model.enabled is True
        assert result.model.is_default is True


def test_retesting_disabled_verified_model_does_not_reenable_it(tmp_path, monkeypatch) -> None:
    _install_passing_verification_client(monkeypatch)
    with _db(tmp_path) as db:
        db.add(
            ModelConfig(
                id="model_a",
                tenant_id="tenant_a",
                name="Chat",
                api_key_encrypted=encrypt_secret("secret"),
                model="model-a",
                trust_status="unverified",
                enabled=False,
                is_default=False,
            )
        )
        db.commit()
        first = run_model_config_test("model_a", tenant_id="tenant_a", db=db)
        assert first.success is True

        result = run_model_config_test(
            "model_a",
            tenant_id="tenant_a",
            activate_if_initial=True,
            db=db,
        )

        row = db.get(ModelConfig, "model_a")
        assert result.success is True
        assert result.activated is False
        assert row is not None
        assert row.enabled is False
        assert row.is_default is False


def test_verification_does_not_replace_or_clear_existing_default(tmp_path, monkeypatch) -> None:
    _install_passing_verification_client(monkeypatch)
    with _db(tmp_path) as db:
        db.add_all(
            [
                ModelConfig(
                    id="model_default",
                    tenant_id="tenant_a",
                    name="Default",
                    api_key_encrypted=encrypt_secret("secret"),
                    model="model-default",
                    trust_status="legacy_trusted",
                    enabled=True,
                    is_default=True,
                ),
                ModelConfig(
                    id="model_new",
                    tenant_id="tenant_a",
                    name="New",
                    api_key_encrypted=encrypt_secret("secret"),
                    model="model-new",
                    trust_status="unverified",
                    enabled=False,
                    is_default=False,
                ),
            ]
        )
        db.commit()

        new_result = run_model_config_test(
            "model_new",
            tenant_id="tenant_a",
            activate_if_initial=True,
            db=db,
        )
        default_result = run_model_config_test(
            "model_default",
            tenant_id="tenant_a",
            activate_if_initial=True,
            db=db,
        )

        default_row = db.get(ModelConfig, "model_default")
        new_row = db.get(ModelConfig, "model_new")
        assert new_result.activated is False
        assert default_result.activated is False
        assert default_row is not None
        assert default_row.enabled is True
        assert default_row.is_default is True
        assert new_row is not None
        assert new_row.enabled is False
        assert new_row.is_default is False


def test_concurrent_initial_verification_activates_only_one_default(tmp_path, monkeypatch) -> None:
    _install_passing_verification_client(monkeypatch)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent-model-api.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_model_configs_tenant_default "
                "ON model_configs(tenant_id) WHERE is_default = 1"
            )
        )
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="Tenant A"))
        for model_id in ("model_a", "model_b"):
            db.add(
                ModelConfig(
                    id=model_id,
                    tenant_id="tenant_a",
                    name=model_id,
                    api_key_encrypted=encrypt_secret("secret"),
                    model=model_id,
                    trust_status="unverified",
                    enabled=False,
                    is_default=False,
                )
            )
        db.commit()

    from app.api import model_configs

    real_has_available_model = model_configs._has_available_model
    first_check_barrier = threading.Barrier(2)
    second_check_barrier = threading.Barrier(2)
    thread_state = threading.local()

    def synchronized_has_available_model(db, tenant_id):  # noqa: ANN001
        check_count = getattr(thread_state, "check_count", 0) + 1
        thread_state.check_count = check_count
        if check_count == 1:
            result = real_has_available_model(db, tenant_id)
            db.rollback()
            first_check_barrier.wait(timeout=10)
            return result
        if check_count == 2:
            second_check_barrier.wait(timeout=10)
            return False
        return real_has_available_model(db, tenant_id)

    monkeypatch.setattr(model_configs, "_has_available_model", synchronized_has_available_model)

    def verify(model_id: str):
        with Session(engine) as db:
            return run_model_config_test(
                model_id,
                tenant_id="tenant_a",
                activate_if_initial=True,
                db=db,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(verify, ("model_a", "model_b")))

    with Session(engine) as db:
        rows = [db.get(ModelConfig, model_id) for model_id in ("model_a", "model_b")]
        assert all(row is not None and row.trust_status == "verified" for row in rows)
        assert sum(bool(row and row.enabled) for row in rows) == 1
        assert sum(bool(row and row.is_default) for row in rows) == 1
    assert sorted(result.activated for result in results) == [False, True]


def _install_passing_verification_client(monkeypatch) -> None:
    class PassingClient:
        def __init__(self, _config) -> None:  # noqa: ANN001
            pass

        def generate_text(self, _prompt, _payload):  # noqa: ANN001
            return "ok"

        def generate_text_stream(self, _prompt, _payload):  # noqa: ANN001
            yield "ok"

        def generate_json(self, _prompt, _payload):  # noqa: ANN001
            return {"ok": True}

    monkeypatch.setattr("app.api.model_configs.LLMClient", PassingClient)
