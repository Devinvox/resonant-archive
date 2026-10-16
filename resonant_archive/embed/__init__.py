"""Local embeddings (nomic-embed-text-v1.5, ONNX Runtime, CPU).

Plain functions and classes only; the CLI lives elsewhere. Heavy imports (onnxruntime,
tokenizers) are deferred until a model is actually loaded.
"""

from .client import EmbedClient, EmbedderUnavailable, EmbedServerError
from .fetch import ModelIntegrityError, fetch_model, verify_model_dir
from .nomic import FULL_DIM, MATRYOSHKA_DIMS, MODEL_NAME, PREFIXES
from .paths import data_dir, model_dir, server_url

__all__ = [
    "EmbedClient", "EmbedderUnavailable", "EmbedServerError",
    "ModelIntegrityError", "fetch_model", "verify_model_dir",
    "FULL_DIM", "MATRYOSHKA_DIMS", "MODEL_NAME", "PREFIXES",
    "data_dir", "model_dir", "server_url",
]
