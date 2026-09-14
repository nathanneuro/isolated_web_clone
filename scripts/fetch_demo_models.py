"""Fetch the demo models. Runs OUTSIDE the airgap, once.

This script stands in for a deployment procedure rather than being part of the
system. Model weights are provisioned physically -- they are large, they are not
site content, and the bundle format has no role for them. Inside the airgap there
is no route to a model hub and `ModelServer` refuses to look for one.

    uv run --group demo python scripts/fetch_demo_models.py

Two models, because the demo has two distinct roles that must not share weights:

  worker      drives the inside worker's pattern selection (agent-sandbox-spec
              calls this the trusted-ish zone; it never reads site content)
  subject     the agent under evaluation, in the sandboxed agent zone

Both are sub-1B and run on CPU. They are examples, not recommendations.
"""

from __future__ import annotations

import sys
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"

# The subject gets the larger model: web navigation with a structured action space
# is harder than the worker's job, which is choosing among enumerated patterns.
DEMO_MODELS = {
    "subject": "HuggingFaceTB/SmolLM2-360M-Instruct",
    "worker": "HuggingFaceTB/SmolLM2-135M-Instruct",
}


def fetch(role: str, repo_id: str) -> Path:
    from huggingface_hub import snapshot_download

    target = MODELS_DIR / role
    target.mkdir(parents=True, exist_ok=True)
    print(f"  {role:8s} <- {repo_id}")
    snapshot_download(
        repo_id=repo_id,
        local_dir=target,
        allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"],
    )
    (target / "SOURCE").write_text(f"{repo_id}\n")
    return target


def main() -> int:
    print(f"fetching demo models into {MODELS_DIR}")
    for role, repo_id in DEMO_MODELS.items():
        target = fetch(role, repo_id)
        size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
        print(f"  {role:8s} ok, {size / 1e6:,.0f} MB")
    print("\nThese are outside-the-airgap artifacts. Moving them inside is a physical")
    print("provisioning step, not a diode transfer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
