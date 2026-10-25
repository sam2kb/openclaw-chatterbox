"""Pinned upstream/model identities.

These are intentionally not "latest" lookups. Reproducible releases use these
exact commits and revisions. Changing any value requires a new release manifest
and a fresh compatibility/quality run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CHATTERBOX_GIT = "https://github.com/resemble-ai/chatterbox.git"
CHATTERBOX_COMMIT = "5de7a54aa4e5e2baadb0182dde554908b48b85c2"
PERTH_GIT = "https://github.com/resemble-ai/Perth.git"
PERTH_COMMIT = "ff1c8ac55a976971245cdd53c18d6131ca00d993"

TURBO_REPO_ID = "ResembleAI/chatterbox-turbo"
TURBO_REVISION = "749d1c1a46eb10492095d68fbcf55691ccf137cd"
TURBO_SAMPLE_RATE = 24_000
TURBO_REFERENCE_MIN_MS = 5_001
TURBO_REFERENCE_MAX_MS = 20_000
TURBO_RECOMMENDED_REFERENCE_MS = (10_000, 15_000)
# Chatterbox's conditioning windows: 15 s encoder and 10 s decoder. Asking the
# user for a 10-15 s segment keeps both windows meaningful.
TURBO_DECODER_CONDITIONING_MS = 10_000
TURBO_ENCODER_CONDITIONING_MS = 15_000


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str


TURBO_FILES: tuple[ModelFile, ...] = (
    ModelFile("added_tokens.json", 418, "72e4ab6acb0d9309ac3df4b526ae5fd80a2da5bc5ab7bb02d85096a374f69193"),
    ModelFile("merges.txt", 456_318, "1ce1664773c50f3e0cc8842619a93edc4624525b728b188a9e0be33b7726adc5"),
    ModelFile("s3gen_meanflow.safetensors", 1_064_875_036, "d65cb687a2ed581ee6cc297e919ffefa63386944f42364ae13b78a594945514f"),
    ModelFile("special_tokens_map.json", 470, "92ba8063bf40aa163eadebbfe0de07c2aebe44cf0d4a9e8726580b0781fd2640"),
    ModelFile("t3_turbo_v1.safetensors", 1_915_480_052, "fcf1f8c1d651bb7e3acd69ee5be269b4ac10c02980b7708213d598bc9f7cdf87"),
    ModelFile("t3_turbo_v1.yaml", 8_457, "57623237f5072148a138a47fd93da1241bc5069fd0df7f1850c00053391c50de"),
    ModelFile("tokenizer_config.json", 3_878, "bca16a2ac1ddbd78b8d6228f0031884cc74b6ea54b967d6f6d2ebae9ccde23e6"),
    ModelFile("ve.safetensors", 5_695_784, "f0921cab452fa278bc25cd23ffd59d36f816d7dc5181dd1bef9751a7fb61f63c"),
    ModelFile("vocab.json", 999_186, "f6bd25a65e4e63ca31360e9fb11c7e4f9a391a78385d640acd814092dd6eee4f"),
)

# conds.pt is present in the pinned snapshot but is not needed for a
# reference-conditioned call. Verify it if present, but do not require it.
TURBO_OPTIONAL_FILES: tuple[ModelFile, ...] = (
    ModelFile("conds.pt", 169_454, "b1852099306fd6a7814eb9d0bd10186caba7249596cc23868f78a0eefbfa5033"),
)


def expected_files(backend: str) -> tuple[ModelFile, ...]:
    if backend != "turbo":
        raise ValueError(f"unsupported backend: {backend}")
    return TURBO_FILES


def missing_or_bad_files(model_dir: Path, backend: str = "turbo") -> list[dict[str, object]]:
    problems: list[dict[str, object]] = []
    for item in expected_files(backend):
        path = model_dir / item.name
        if not path.is_file():
            problems.append({"file": item.name, "problem": "missing"})
            continue
        size = path.stat().st_size
        if size != item.size:
            problems.append(
                {"file": item.name, "problem": "size_mismatch", "expected": item.size, "actual": size}
            )
    return problems


def model_identity(backend: str = "turbo") -> dict[str, object]:
    if backend != "turbo":
        raise ValueError(f"unsupported backend: {backend}")
    return {
        "backend": "turbo",
        "variant": "Chatterbox-Turbo",
        "language": "en",
        "sample_rate": TURBO_SAMPLE_RATE,
        "repo_id": TURBO_REPO_ID,
        "revision": TURBO_REVISION,
        "chatterbox_commit": CHATTERBOX_COMMIT,
        "perth_commit": PERTH_COMMIT,
        "license": "MIT",
        "watermark": "Perth implicit watermark (upstream behavior)",
        "reference_min_ms": TURBO_REFERENCE_MIN_MS,
        "reference_max_ms": TURBO_REFERENCE_MAX_MS,
    }
