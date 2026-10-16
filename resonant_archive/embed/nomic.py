"""Local nomic-embed-text-v1.5 embedder (ONNX Runtime, CPU, no network).

Recipe (nomic-embed-text-v1.5 model card):
  1. task prefix: "search_document: " for stored text, "search_query: " for queries
  2. tokenize (BERT wordpiece, [CLS] ... [SEP]); cap at max_tokens (2048 = trained context)
  3. last_hidden_state -> mean pool over the attention mask
  4. layer_norm over the hidden dim (no affine, eps 1e-5, as torch F.layer_norm)
  5. Matryoshka truncation to `dim` (768/512/256/128/64)
  6. L2 normalise

Attention in this ONNX export is unfused (B*H*L^2 float32 score tensors), so memory
grows with batch * length^2. Batches are planned against a measured memory model
(see est_mem_mb) rather than a fixed batch size, so 2048-token texts can't blow
the service's MemoryMax.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

from .paths import model_dir as _default_model_dir

MODEL_FILE = "model.onnx"
# fp32 ONNX (HF onnx/model.onnx, sha256 147d5aa8...). The int8 export
# (model_quantized.onnx) was rejected: DynamicQuantizeLinear takes one scale per
# whole activation tensor, so a text's vector changes with its batch neighbours
# (solo-vs-batched cosine down to 0.745). fp32 is exactly batch-invariant.
MODEL_NAME = "nomic-embed-text-v1.5"
FULL_DIM = 768
MATRYOSHKA_DIMS = (64, 128, 256, 512, 768)
MAX_TOKENS = 2048
PREFIXES = {"document": "search_document: ", "query": "search_query: "}
# Pre-cut absurd inputs by characters before tokenizing (a wordpiece token is
# >= 1 char, so cutting at 16 chars/token never removes text that would have fit).
CHAR_PRECUT_PER_TOKEN = 16

# Measured on a Haswell-class box (AVX2, ORT 1.30, fp32 model, 4 threads):
#   peak_rss ~= 890 MB (loaded model) + 92.8 MB per 1e6 (B*L^2) + 0.099 MB per (B*L)
# fitted on (1,2048) and (32,300); checked on (2,2048),(4,1500),(8,1500),(32,512),
# (16,1024): max error ~1%.
_MEM_A_PER_ML2 = 92.8e-6
_MEM_B_PER_TOK = 0.099


def est_mem_mb(batch: int, length: int) -> float:
    """Estimated working memory (MB, above the loaded model) for one forward pass."""
    return _MEM_A_PER_ML2 * batch * length * length + _MEM_B_PER_TOK * batch * length


@dataclass
class EmbedResult:
    vectors: np.ndarray  # (n, dim) float32, L2-normalised
    tokens: list[int]  # tokens actually fed per text (incl. prefix + [CLS]/[SEP])
    truncated: list[bool]  # True if the text was cut to max_tokens


class Embedder:
    def __init__(
        self,
        model_dir: str | os.PathLike | None = None,
        model_file: str = MODEL_FILE,
        threads: int = 4,
        max_batch: int = 8,
        mem_budget_mb: float = 1500.0,
        max_tokens: int = MAX_TOKENS,
        arena: bool = False,
        verify: bool = True,
    ) -> None:
        # Keep the Rust tokenizer's rayon pool inside the same thread budget.
        os.environ.setdefault("RAYON_NUM_THREADS", str(threads))
        model_dir = os.fspath(model_dir) if model_dir is not None else os.fspath(_default_model_dir())
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as e:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "the local embedder needs onnxruntime and tokenizers (pip install onnxruntime tokenizers numpy)"
            ) from e
        for need in ("tokenizer.json", model_file):
            if not os.path.isfile(os.path.join(model_dir, need)):
                raise FileNotFoundError(
                    f"{need} not found in {model_dir}; run `resonant-archive models fetch` to download the embedding model"
                )

        if verify:
            # Every pinned file, before anything is parsed: a swapped model.onnx or tokenizer.json is refused here
            # (ModelIntegrityError), whoever loads the model in-process. A folder that verified before and has not
            # changed is not hashed again.
            from .fetch import verify_cached

            verify_cached(model_dir)

        self.max_batch = max_batch
        self.mem_budget_mb = mem_budget_mb
        self.max_tokens = max_tokens
        self.threads = threads

        self.tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.sep_id = self.tokenizer.token_to_id("[SEP]")
        self.pad_id = self.tokenizer.token_to_id("[PAD]")
        if self.sep_id is None or self.pad_id is None:
            raise RuntimeError("tokenizer is missing [SEP]/[PAD]")

        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Shapes vary per batch: the memory-pattern planner can't reuse plans, and
        # with arena=False large activations go back to the OS after each pass.
        so.enable_mem_pattern = False
        so.enable_cpu_mem_arena = arena
        self.session = ort.InferenceSession(
            os.path.join(model_dir, model_file), so, providers=["CPUExecutionProvider"]
        )
        self.input_names = {i.name for i in self.session.get_inputs()}

    # -- tokenization -----------------------------------------------------
    def tokenize(self, texts: list[str], task: str) -> tuple[list[list[int]], list[bool]]:
        if task not in PREFIXES:
            raise ValueError(f"task must be one of {sorted(PREFIXES)}")
        prefix = PREFIXES[task]
        precut = self.max_tokens * CHAR_PRECUT_PER_TOKEN
        prepared, cut = [], []
        for t in texts:
            if not isinstance(t, str):
                raise ValueError("every text must be a string")
            cut.append(len(t) > precut)
            prepared.append(prefix + (t[:precut] if len(t) > precut else t))
        encs = self.tokenizer.encode_batch(prepared)
        ids_list, truncated = [], []
        for enc, was_cut in zip(encs, cut):
            ids = enc.ids
            if len(ids) > self.max_tokens:
                ids = ids[: self.max_tokens - 1] + [self.sep_id]
                was_cut = True
            ids_list.append(ids)
            truncated.append(was_cut)
        return ids_list, truncated

    # -- batching ---------------------------------------------------------
    def plan_batches(self, lengths: list[int]) -> list[list[int]]:
        """Group indices longest-first so padding is minimal and each batch fits the memory budget."""
        order = sorted(range(len(lengths)), key=lambda i: -lengths[i])
        batches, cur, cur_len = [], [], 0
        for i in order:
            if not cur:
                cur, cur_len = [i], lengths[i]
                continue
            # cur_len is the batch's padded length (first item is the longest).
            if len(cur) + 1 <= self.max_batch and est_mem_mb(len(cur) + 1, cur_len) <= self.mem_budget_mb:
                cur.append(i)
            else:
                batches.append(cur)
                cur, cur_len = [i], lengths[i]
        if cur:
            batches.append(cur)
        return batches

    # -- inference --------------------------------------------------------
    def _forward(self, batch_ids: list[list[int]]) -> np.ndarray:
        L = max(len(x) for x in batch_ids)
        B = len(batch_ids)
        input_ids = np.full((B, L), self.pad_id, dtype=np.int64)
        mask = np.zeros((B, L), dtype=np.int64)
        for r, ids in enumerate(batch_ids):
            input_ids[r, : len(ids)] = ids
            mask[r, : len(ids)] = 1
        feeds = {"input_ids": input_ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)
        (hidden,) = self.session.run(["last_hidden_state"], feeds)
        # mean pooling over the attention mask
        m = mask[:, :, None].astype(np.float32)
        summed = (hidden * m).sum(axis=1)
        counts = np.clip(m.sum(axis=1), 1e-9, None)
        return (summed / counts).astype(np.float32)

    @staticmethod
    def finish(pooled: np.ndarray, dim: int) -> np.ndarray:
        """layer_norm -> Matryoshka truncate -> L2 normalise."""
        mu = pooled.mean(axis=1, keepdims=True)
        var = pooled.var(axis=1, keepdims=True)
        x = (pooled - mu) / np.sqrt(var + 1e-5)
        x = x[:, :dim]
        norms = np.linalg.norm(x, axis=1, keepdims=True)
        return (x / np.clip(norms, 1e-12, None)).astype(np.float32)

    def embed(self, texts: list[str], task: str = "document", dim: int = FULL_DIM) -> EmbedResult:
        if dim not in MATRYOSHKA_DIMS:
            raise ValueError(f"dim must be one of {MATRYOSHKA_DIMS}")
        if not texts:
            return EmbedResult(np.zeros((0, dim), dtype=np.float32), [], [])
        ids_list, truncated = self.tokenize(texts, task)
        lengths = [len(x) for x in ids_list]
        pooled = np.zeros((len(texts), FULL_DIM), dtype=np.float32)
        for batch in self.plan_batches(lengths):
            pooled[batch] = self._forward([ids_list[i] for i in batch])
        return EmbedResult(self.finish(pooled, dim), lengths, truncated)
