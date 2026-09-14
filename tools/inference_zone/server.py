"""A minimal model server. Holds weights; exposes generation and nothing else.

The interface is deliberately narrow. There is no endpoint that returns weights,
no endpoint that loads a different model, no endpoint that reports the system
prompt back, and no passthrough that lets a caller set arbitrary generation
parameters. Everything a caller may vary is a field on `GenerationLimits`, bounded
here rather than by the caller.

Weights enter the airgap by physical provisioning, not through the diode. Model
files are large, they are not site content, and the bundle format has no role for
them. That is a deployment procedure, and `fetch_demo_models.py` stands in for it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parents[2] / "models"


@dataclass(frozen=True)
class GenerationLimits:
    """Bounds the caller cannot exceed. Not suggestions."""

    max_new_tokens: int = 128
    temperature: float = 0.0
    top_p: float = 1.0
    seed: int = 0

    def __post_init__(self) -> None:
        assert 1 <= self.max_new_tokens <= 1024, self.max_new_tokens
        assert 0.0 <= self.temperature <= 2.0, self.temperature
        assert 0.0 < self.top_p <= 1.0, self.top_p


class ModelServer:
    """Loads a local model directory and generates. No network, ever.

    `local_files_only=True` is not a performance choice. Inside the airgap there is
    no route to a model hub, so a code path that would silently fetch on a cache
    miss is a code path that fails confusingly in deployment and succeeds
    misleadingly in development. Fail the same way in both.
    """

    def __init__(self, model_dir: Path, *, device: str = "cpu") -> None:
        model_dir = Path(model_dir)
        assert model_dir.is_dir(), (
            f"{model_dir} not found. Model weights are provisioned physically, not "
            f"over the diode; run scripts/fetch_demo_models.py outside the airgap."
        )
        # Belt and braces: even if a library ignores local_files_only, offline mode
        # makes the attempt fail rather than reach out.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.model_dir = model_dir
        self.device = device
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
        self.model = AutoModelForCausalLM.from_pretrained(model_dir, local_files_only=True)
        self.model.to(device)
        self.model.eval()

    @property
    def model_id(self) -> str:
        """The only thing about the model a caller may learn."""
        return self.model_dir.name

    def generate(self, messages: list[dict[str, str]], limits: GenerationLimits) -> str:
        """Chat-format generation. Returns the completion text only."""
        import torch

        assert messages, "no messages"
        for message in messages:
            assert set(message) == {"role", "content"}, message
            assert message["role"] in ("system", "user", "assistant"), message["role"]

        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        torch.manual_seed(limits.seed)

        with torch.no_grad():
            output = self.model.generate(
                **inputs,
                max_new_tokens=limits.max_new_tokens,
                do_sample=limits.temperature > 0,
                temperature=limits.temperature or None,
                top_p=limits.top_p,
                pad_token_id=self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
            )
        completion = output[0][inputs["input_ids"].shape[1] :]
        return self.tokenizer.decode(completion, skip_special_tokens=True).strip()
