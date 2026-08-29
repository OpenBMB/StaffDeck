# Quickstart Validation: Codex Subscription Models

## Automated validation

1. Create or update the focused backend tests before production code and run them to observe the expected failure.
2. Run `uv --directory backend run pytest tests/test_codex_subscription.py tests/test_model_configs_api.py tests/test_model_protocols.py tests/test_database_config.py -q`.
3. Run `uv --directory backend run ruff check .`.
4. Run `npm --prefix frontend-enterprise test -- --run src/pages/ModelsPage.test.ts`.
5. Run `npm --prefix frontend-enterprise run build`.

## Manual local validation

1. Launch StaffDeck locally and sign in as a tenant administrator.
2. Open **模型** → **新建模型** and select **ChatGPT 订阅（Codex）**.
3. Click **连接 ChatGPT 订阅**. Confirm that the local browser opens without displaying a code or callback to copy.
4. Complete the normal Codex login in the browser and wait for the model form to show **已连接** (optionally with plan type).
5. Save and test a subscription model. Confirm that it can be enabled and selected as default only after its three capability probes succeed.
6. Add an API Key model and confirm both entries remain independently visible, editable and selectable.
7. Inspect the account-status and model-list responses in browser developer tools. Confirm they include only status/plan information, never login URLs, token-like values, user email or API keys.
8. In the model form, use **退出本机订阅** only after confirming the device-wide Codex sign-out warning. Confirm subscription models no longer test successfully while API Key models continue to work.
