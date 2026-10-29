"""Imported only by the isolated child. Never performs network I/O."""

import hashlib
from pathlib import Path

from knowledge.embedding import EmbeddingUnavailable, validate_texts, validate_vectors
from knowledge.profile import PROFILE


def verify_assets(root: Path):
    expected = {a.name for a in PROFILE.artifacts}
    if root.is_symlink() or not root.is_dir():
        raise EmbeddingUnavailable("assets_unavailable")
    entries = list(root.rglob("*"))
    if (
        any(p.is_symlink() for p in entries)
        or {p.relative_to(root).as_posix() for p in entries if p.is_file()} != expected
    ):
        raise EmbeddingUnavailable("unmanifested_assets")
    for artifact in PROFILE.artifacts:
        path = root / artifact.name
        if path.stat().st_size != artifact.size:
            raise EmbeddingUnavailable("asset_size")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != artifact.sha256:
            raise EmbeddingUnavailable("asset_hash")


class Model:
    def __init__(self, assets: Path):
        verify_assets(assets)
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self.tokenizer = Tokenizer.from_file(str(assets / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.session = ort.InferenceSession(
            str(assets / "onnx/model_int8.onnx"), options, providers=["CPUExecutionProvider"]
        )
        if self.session.get_providers() != ["CPUExecutionProvider"]:
            raise EmbeddingUnavailable("provider_mismatch")

    def embed(self, texts):
        import numpy as np

        validate_texts(texts)
        encoded = self.tokenizer.encode(texts[0], add_special_tokens=True)
        if len(encoded.ids) > 512:
            raise EmbeddingUnavailable("token_limit")
        inputs = {
            "input_ids": np.array([encoded.ids], dtype=np.int64),
            "attention_mask": np.array([encoded.attention_mask], dtype=np.int64),
        }
        output = self.session.run(["last_hidden_state"], inputs)[0]
        if (
            not isinstance(output, np.ndarray)
            or output.ndim != 3
            or output.shape != (1, len(encoded.ids), 1024)
        ):
            raise EmbeddingUnavailable("invalid_output")
        vector = output[:, 0, :].astype(np.float64)
        norm = np.linalg.norm(vector, axis=1, keepdims=True)
        if not np.all(np.isfinite(norm)) or np.any(norm == 0):
            raise EmbeddingUnavailable("invalid_vectors")
        return validate_vectors((vector / norm).tolist())
