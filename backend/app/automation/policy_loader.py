"""冻结策略 artifact 的加载与校验。

artifact 是经过离线评测冻结的 JSON，形如 eval/automation/policy.json。
- 校验 schema、policy version、必要字段、数值范围。
- 任何校验失败都抛 PolicyLoaderError，调用方必须 fail closed。
- 未经过真实 calibration 的 intent 必须 enabled=false。
"""

import json
from pathlib import Path

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
    prediction_artifact_hash: str | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    rules: dict[str, RuleConfig]

    def rule_for(self, intent: ActionIntent | str) -> RuleConfig | None:
        key = intent.value if isinstance(intent, ActionIntent) else intent
        return self.rules.get(key)

    def is_auto_enabled(self, intent: ActionIntent | str) -> bool:
        rule = self.rule_for(intent)
        return bool(rule and rule.enabled and rule.allow_auto)

    def auto_enabled_intents(self) -> list[str]:
        return [
            intent.value
            for intent in ALL_INTENTS
            if self.is_auto_enabled(intent)
        ]

    def validate_for_enforce(self) -> None:
        """enforce 模式下的完整性校验（P0-7），任何失败都应 fail closed。

        要求：
        - source_dataset_hash 非空（正式策略必须来自可复现数据集）
        - 任一 enabled+allow_auto 的 intent 必须：
            observed_precision 非 null
            sample_count > 0
            prediction_artifact_hash 非空
        - 若存在 enable 的 intent 但缺 prediction_artifact_hash，直接拒绝。
        """
        if not self.source_dataset_hash:
            raise PolicyLoaderError(
                "enforce 模式拒绝：source_dataset_hash 为空，"
                "策略未绑定可复现数据集"
            )
        enabled = self.auto_enabled_intents()
        if not enabled:
            # 无自动 intent 的策略是安全的（全 disabled），允许。
            return
        if not self.prediction_artifact_hash:
            raise PolicyLoaderError(
                "enforce 模式拒绝：存在已启用自动执行的 intent，"
                "但缺少 prediction_artifact_hash"
            )
        for intent_key in enabled:
            rule = self.rules[intent_key]
            if rule.observed_precision is None:
                raise PolicyLoaderError(
                    f"enforce 模式拒绝：intent {intent_key} 的 observed_precision 为 null"
                )
            if rule.sample_count == 0:
                raise PolicyLoaderError(
                    f"enforce 模式拒绝：intent {intent_key} 的 sample_count 为 0"
                )


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
    for_enforce: bool = False,
) -> CalibratedPolicy:
    """加载并校验冻结策略。

    若文件不存在且给了 policy_version，返回全 disabled 的初始策略；
    否则抛 PolicyLoaderError（fail closed）。
    for_enforce=True 时额外执行 enforce 完整性校验（P0-7）。
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

    if for_enforce:
        policy.validate_for_enforce()

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
