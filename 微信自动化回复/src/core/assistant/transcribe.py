import json
import os
import sys
from pathlib import Path
from typing import Protocol

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


class TranscriberProtocol(Protocol):
    def __call__(self, audio: str, model_path: str) -> dict:
        """Transcribe local audio using installed weights, without downloads."""
        raise NotImplementedError


def transcribe(audio: str, model_path: str) -> dict:
    model = Path(model_path).resolve()
    if not (model / "weights.safetensors").is_file():
        raise ValueError("Local weights missing; download is disabled")

    import mlx_whisper

    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=str(model),
        language="zh",
        verbose=None,
        condition_on_previous_text=False,
    )
    return {"text": result["text"]}


if __name__ == "__main__":
    print(json.dumps(transcribe(sys.argv[1], sys.argv[2]), ensure_ascii=False))
