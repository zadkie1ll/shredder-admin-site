#!/usr/bin/env python3
"""Fail-closed, non-mutating compatibility audit for shredder-common."""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODEL_COLUMN_CONTRACTS = {
    "User": {
        "id",
        "telegram_id",
        "username",
        "telegram_username",
        "bot_instance",
        "referred_by_id",
        "referral_type",
        "autopay_allow",
        "expire_at",
        "ymid",
    },
    "YkPayment": {
        "id",
        "user_id",
        "amount",
        "currency",
        "status",
        "captured_at",
        "created_at",
        "payment_id",
        "subscription_period",
        "is_trial_promotion",
    },
    "YkRecurrentPayment": {
        "id",
        "user_id",
        "amount",
        "currency",
        "recurrent_payment_id",
        "subscription_period",
        "captured_at",
        "scheduled_payment",
    },
    "ReferralBonus": {
        "id",
        "referral_id",
        "referrer_id",
        "created_at",
        "bonus_type",
        "days_added",
    },
}
ACTIVE_RUNTIME_FILES = (
    ROOT / "engine" / "shredder_admin_models.py",
    ROOT / "engine" / "shredder_admin_repository.py",
    ROOT / "engine" / "shredder_admin_views.py",
    ROOT / "web_app" / "admin_urls.py",
    ROOT / "web_app" / "admin_settings.py",
    ROOT / "web_app" / "admin_wsgi.py",
)


def common_imports(paths=ACTIVE_RUNTIME_FILES):
    result = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("common")
            ):
                result.setdefault(node.module, set()).update(
                    item.name for item in node.names if item.name != "*"
                )
    return result


def main():
    failures = []
    remote = subprocess.run(
        ["git", "-C", str(ROOT / "common"), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"common remote: {remote}")
    if "zadkie1ll/shredder-common" not in remote:
        failures.append("common submodule does not point to zadkie1ll/shredder-common")

    imports = common_imports()
    for module_name, names in sorted(imports.items()):
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            failures.append(
                f"missing module {module_name}: {type(exc).__name__}: {exc}"
            )
            continue
        missing = []
        for name in sorted(names):
            if hasattr(module, name):
                continue
            try:
                importlib.import_module(f"{module_name}.{name}")
            except ModuleNotFoundError:
                missing.append(name)
        if missing:
            failures.append(f"missing symbols in {module_name}: {', '.join(missing)}")

    try:
        db_models = importlib.import_module("common.models.db")
        for model_name, required in MODEL_COLUMN_CONTRACTS.items():
            model = getattr(db_models, model_name, None)
            if model is None:
                failures.append(f"missing model {model_name}")
                continue
            missing = sorted(required - set(model.__table__.columns.keys()))
            if missing:
                failures.append(
                    f"model {model_name} lacks columns: {', '.join(missing)}"
                )
    except Exception as exc:
        failures.append(f"cannot inspect common.models.db: {type(exc).__name__}: {exc}")

    print(f"runtime common imports: {sum(map(len, imports.values()))}")
    print("active mode: read-only; RWMS is not imported or called")
    if failures:
        print(f"INCOMPATIBLE ({len(failures)} contract groups)")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("COMPATIBLE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
