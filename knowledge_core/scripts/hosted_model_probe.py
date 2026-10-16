"""Use the existing PilotDeck config parser and StaffDeck credential/model resolver.

Only the isolated acceptance DB is written. Credentials never enter reports.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from sqlmodel import Session, select
    from app.db import database
    from app.db.models import ModelConfig
    from app.llm import LLMClient
    from app.llm.model_config_resolver import resolve_model_config_for_runtime
    from app.security.encryption import encrypt_secret
    script = '''import {loadPilotConfig} from './dist/src/pilot/config/loadPilotConfig.js';
const {config} = loadPilotConfig({configPath: process.argv[1]});
process.stdout.write(JSON.stringify({defaultId:config.agent.model.id, providers:config.model.providers}));'''
    parsed = json.loads(subprocess.check_output([args.node, "--input-type=module", "-e", script, args.config], cwd=args.pilot_root, stderr=subprocess.DEVNULL))
    report = {"status": "not_accepted", "source": "existing PilotDeck config via loadPilotConfig",
              "attempts": [], "selected_model_id": None}
    with Session(database.engine) as db:
        existing = db.exec(select(ModelConfig).where(ModelConfig.tenant_id == "tenant_demo", ModelConfig.enabled == True)).all()
        if not existing:
            for provider_id, provider in parsed["providers"].items():
                for name in provider["models"]:
                    protocol = {"openai": "openai_chat_completions", "anthropic": "anthropic_messages"}.get(provider["protocol"])
                    if not protocol: continue
                    identifier = provider_id + "/" + name
                    db.add(ModelConfig(id=identifier, tenant_id="tenant_demo", name=identifier,
                        provider=provider_id, api_protocol=protocol, base_url=provider["url"],
                        api_key_encrypted=encrypt_secret(provider["apiKey"]), model=name,
                        extra_body_json=provider.get("extraBody") or {},
                        is_default=identifier == parsed["defaultId"], max_output_tokens=512))
            db.commit()
            existing = db.exec(select(ModelConfig).where(ModelConfig.tenant_id == "tenant_demo", ModelConfig.enabled == True)).all()
        for row in sorted(existing, key=lambda row: (not row.is_default, row.id)):
            attempt = {"config_id": row.id, "provider": row.provider, "model": row.model, "is_default": row.is_default}
            try:
                config = resolve_model_config_for_runtime(db, "tenant_demo", row.id)
                client = LLMClient(config)
                client.timeout_seconds = 20
                value = client.generate_json('Return exactly a JSON object with km_acceptance set to true.', {"purpose": "temporary KM acceptance"})
                if not isinstance(value, dict) or value.get("km_acceptance") is not True:
                    raise ValueError("MODEL_RESULT_INVALID")
                attempt.update(status="passed", result=value)
                report.update(status="available", selected_model_id=row.id)
                row.max_output_tokens = 8192
                db.add(row)
                db.commit()
            except Exception as exc:
                # HTTP detail is a stable resolver code; upstream exception text
                # may contain URLs, credentials or request data and is omitted.
                attempt.update(status="failed", error_type=type(exc).__name__,
                               error_code=getattr(exc, "detail", None))
            report["attempts"].append(attempt)
            if report["selected_model_id"]: break
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    return 0 if report["selected_model_id"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
