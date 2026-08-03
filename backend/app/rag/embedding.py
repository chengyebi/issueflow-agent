import hashlib
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.core.config import Settings, get_settings

FASTEMBED_MAX_INPUT_TOKENS = 512


class EmbeddingError(RuntimeError):
    pass


class EmbeddingUnavailableError(EmbeddingError):
    pass


class EmbeddingDimensionError(EmbeddingError):
    pass


@dataclass(frozen=True)
class EmbeddingObservation:
    input_characters: int
    original_tokens: int
    embedded_tokens: int
    max_input_tokens: int
    truncated: bool


class EmbeddingProvider(Protocol):
    model_name: str
    dimensions: int
    last_observations: list[EmbeddingObservation]

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, texts: list[str]) -> list[list[float]]: ...

    def tokenize(self, text: str) -> list[int]: ...

    def decode_tokens(self, token_ids: list[int]) -> str: ...


class DisabledEmbeddingProvider:
    model_name = "disabled"
    dimensions = 0
    last_observations: list[EmbeddingObservation] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("Embedding Provider 未配置")

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)

    def tokenize(self, text: str) -> list[int]:
        raise EmbeddingUnavailableError("Embedding Provider 未配置")

    def decode_tokens(self, token_ids: list[int]) -> str:
        raise EmbeddingUnavailableError("Embedding Provider 未配置")


class FakeEmbeddingProvider:
    """Deterministic, dependency-free provider for tests and reproducible baselines."""

    def __init__(self, dimensions: int = 16, model_name: str = "fake-hash-v1"):
        if dimensions < 2 or dimensions > 4096:
            raise ValueError("Embedding dimensions 必须在 2 到 4096 之间")
        self.dimensions = dimensions
        self.model_name = model_name
        self.call_count = 0
        self.last_observations: list[EmbeddingObservation] = []
        self._token_text: dict[int, str] = {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        self.last_observations = [
            EmbeddingObservation(
                input_characters=len(text),
                original_tokens=len(text.split()),
                embedded_tokens=len(text.split()),
                max_input_tokens=FASTEMBED_MAX_INPUT_TOKENS,
                truncated=False,
            )
            for text in texts
        ]
        vectors = []
        for text in texts:
            values = []
            counter = 0
            while len(values) < self.dimensions:
                digest = hashlib.sha256(
                    f"{self.model_name}:{counter}:{text}".encode("utf-8")
                ).digest()
                values.extend((byte - 127.5) / 127.5 for byte in digest)
                counter += 1
            vector = values[: self.dimensions]
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts)

    def tokenize(self, text: str) -> list[int]:
        # Stable word/punctuation tokens are sufficient for unit tests; production uses
        # the model's real tokenizer through FastEmbedEmbeddingProvider.
        import re

        pieces = re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE)
        ids = []
        for piece in pieces:
            token_id = int.from_bytes(
                hashlib.sha256(piece.encode("utf-8")).digest()[:8], "big"
            )
            self._token_text[token_id] = piece
            ids.append(token_id)
        return ids

    def decode_tokens(self, token_ids: list[int]) -> str:
        import re

        pieces = [self._token_text[token_id] for token_id in token_ids]
        text = " ".join(pieces)
        text = re.sub(r"\[\s+([^\]]+?)\s+\]", r"[\1]", text)
        return re.sub(r"\s+([,.;:!?])", r"\1", text)


class FastEmbedEmbeddingProvider:
    max_input_tokens = FASTEMBED_MAX_INPUT_TOKENS

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.model_name = settings.embedding_model
        self.dimensions = settings.embedding_dimension
        self.batch_size = settings.embedding_batch_size
        self.query_prefix = settings.embedding_query_prefix
        self.cache_dir = Path(settings.embedding_cache_dir)
        self.local_files_only = settings.embedding_local_files_only
        self.last_observations: list[EmbeddingObservation] = []
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        try:
            from fastembed import TextEmbedding
            from tokenizers import Tokenizer
        except ImportError as exc:  # pragma: no cover - dependency is present in runtime image
            raise EmbeddingUnavailableError("FastEmbed 运行依赖未安装") from exc

        declared_dimension = TextEmbedding.get_embedding_size(self.model_name)
        if declared_dimension != self.dimensions:
            raise EmbeddingDimensionError(
                f"模型 {self.model_name} 声明维度为 {declared_dimension}，"
                f"但 EMBEDDING_DIMENSION={self.dimensions}"
            )
        try:
            self._model = TextEmbedding(
                model_name=self.model_name,
                cache_dir=str(self.cache_dir),
                providers=["CPUExecutionProvider"],
                local_files_only=self.local_files_only,
            )
        except Exception as exc:
            mode = "离线缓存加载" if self.local_files_only else "下载或加载"
            raise EmbeddingUnavailableError(
                f"FastEmbed 无法{mode}模型 {self.model_name}: {type(exc).__name__}"
            ) from exc

        tokenizer = self._model.model.tokenizer
        if tokenizer is None:
            raise EmbeddingUnavailableError("FastEmbed 模型未加载 tokenizer")
        self._raw_tokenizer = Tokenizer.from_str(tokenizer.to_str())
        self._raw_tokenizer.no_truncation()
        self._raw_tokenizer.no_padding()

    def _observe(self, texts: list[str]) -> list[EmbeddingObservation]:
        observations = []
        for text in texts:
            original_tokens = len(self._raw_tokenizer.encode(text).ids)
            embedded_tokens = self._model.token_count(text)
            observations.append(
                EmbeddingObservation(
                    input_characters=len(text),
                    original_tokens=original_tokens,
                    embedded_tokens=embedded_tokens,
                    max_input_tokens=self.max_input_tokens,
                    truncated=original_tokens > embedded_tokens,
                )
            )
        return observations

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self.last_observations = self._observe(texts)
        vectors = [
            vector.astype(float, copy=False).tolist()
            for vector in self._model.embed(texts, batch_size=self.batch_size)
        ]
        if len(vectors) != len(texts):
            raise EmbeddingError("FastEmbed 返回的向量数量与输入数量不一致")
        for vector in vectors:
            if len(vector) != self.dimensions:
                raise EmbeddingDimensionError(
                    f"模型实际输出 {len(vector)} 维，数据库配置要求 {self.dimensions} 维"
                )
        return vectors

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        prepared = [f"{self.query_prefix}{text}" if self.query_prefix else text for text in texts]
        return self.embed(prepared)

    def tokenize(self, text: str) -> list[int]:
        return self._raw_tokenizer.encode(text).ids

    def decode_tokens(self, token_ids: list[int]) -> str:
        return self._raw_tokenizer.decode(token_ids, skip_special_tokens=True)

    def validate(self) -> dict:
        vector = self.embed_query(["IssueFlow embedding dimension probe"])[0]
        return {
            "provider": "fastembed",
            "model": self.model_name,
            "dimensions": len(vector),
            "cache_dir": str(self.cache_dir),
            "max_input_tokens": self.max_input_tokens,
        }


def create_embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    settings = settings or get_settings()
    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider(
            dimensions=settings.embedding_dimensions,
            model_name=settings.embedding_model,
        )
    if settings.embedding_provider == "fastembed":
        return FastEmbedEmbeddingProvider(settings)
    if settings.embedding_provider == "disabled":
        return DisabledEmbeddingProvider()
    raise EmbeddingUnavailableError(
        f"未实现的 Embedding Provider: {settings.embedding_provider}"
    )


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    return create_embedding_provider()


def clear_embedding_provider_cache() -> None:
    get_embedding_provider.cache_clear()
