"""冻结策略 artifact 的加载与校验。

artifact 是经过离线评测冻结的 JSON，形如 eval/automation/policy.json。
- 校验 schema、policy version、必要字段、数值范围。
- 任何校验失败都抛 PolicyLoaderError，调用方必须 fail closed。
- 未经过真实 calibration 的 intent 必须 enabled=false。
"""

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.automation.models import ActionIntent

POLICY_SCHEMA_VERSION = "1.0"

# 所有已知 action intent。artifact 中缺失的 intent 视为 disabled。
ALL_INTENTS = [
    ActionIntent.ADD_CATEGORY_LABEL,
    ActionIntent.REQUEST_MISSING_INFORMATION,
    ActionIntent.POST_TECHNICAL_REPLY,
    ActionIntent.DUPLICATE_ACTION,
]


class RuleConfig(BaseModel):
    enabled: bool
    min_model_confidence: float = Field(ge=0.0, le=1.0)
    require_evidence: bool = True
    observed_precision: float | None = Field(default=None, ge=0.0, le=1.0)
    coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    sample_count: int = Field(default=0, ge=0)
    # 是否允许自动侧写。default False：未校准的 intent 不允许自动执行。
    allow_auto: bool = False


class CalibratedPolicy(BaseModel):
    schema_version: str
    policy_version: str
    created_at: str
    source_dataset_hash: str
    model_name: str | None = None
    prompt_version: str | None = None
    rules: dict[str, RuleConfig]

    def rule_for(self, intent: ActionIntent | str) -> RuleConfig | None:
        key = intent.value if isinstance(intent, ActionIntent) else intent
        return self.rules.get(key)

    def is_auto_enabled(self, intent: ActionIntent | str) -> bool:
        rule = self.rule_for(intent)
        return bool(rule and rule.enabled and rule.allow_auto)


class PolicyLoaderError(RuntimeError):
    pass


def _default_policy(policy_version: str) -> dict:
    """初始 artifact：未校准的 intent 全部 disabled，绝不伪造评测指标。"""
    rules = {}
    for intent in ALL_INTENTS:
        rules[intent.value] = {
            "enabled": False,
            "min_model_confidence": 1.0,
            "require_evidence": True,
            "observed_precision": None,
            "coverage": None,
            "sample_count": 0,
            "allow_auto": False,
        }
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_version": policy_version,
        "created_at": "",
        "source_dataset_hash": "",
        "model_name": None,
        "prompt_version": None,
        "rules": rules,
    }


def load_calibrated_policy(
    path: Path | None = None,
    *,
    policy_version: str | None = None,
) -> CalibratedPolicy:
    """加载并校验冻结策略。

    若文件不存在且给了 policy_version，返回全 disabled 的初始策略；
    否则抛 PolicyLoaderError（fail closed）。
    """
    if path is None:
        path = _default_policy_path()

    if not path.exists():
        if policy_version:
            return CalibratedPolicy.model_validate(_default_policy(policy_version))
        raise PolicyLoaderError(f"策略 artifact 不存在: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyLoaderError(f"策略 artifact 无法解析: {path}: {exc}") from exc

    if raw.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise PolicyLoaderError(
            f"策略 artifact schema 版本不匹配: {raw.get('schema_version')!r}"
        )
    if not raw.get("policy_version"):
        raise PolicyLoaderError("策略 artifact 缺少 policy_version")

    try:
        policy = CalibratedPolicy.model_validate(raw)
    except ValidationError as exc:
        raise PolicyLoaderError(f"策略 artifact 校验失败: {exc}") from exc

    return policy


def _default_policy_path() -> Path:
    here = Path(__file__).resolve().parents[3]
    return here / "eval" / "automation" / "policy.json"


def list_auto_enabled_intents(policy: CalibratedPolicy) -> list[str]:
    return [
        intent.value
        for intent in ALL_INTENTS
        if policy.is_auto_enabled(intent)
    ]


def policy_to_dict(policy: CalibratedPolicy) -> dict:
    return policy.model_dump(mode="json")


def _disabled_reason() -> Literal[""]:
    return ""
