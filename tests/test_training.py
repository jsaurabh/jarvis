import json
from pathlib import Path

import pytest

from jarvis.train import file_sha256, load_examples


def test_training_dataset_validation_and_hash(tmp_path: Path) -> None:
    path = tmp_path / "train.jsonl"
    row = {
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert load_examples(path) == [row]
    assert len(file_sha256(path)) == 64


def test_training_dataset_requires_assistant_target(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"messages": [{"role": "user", "content": "Hi"}]}) + "\n")
    with pytest.raises(ValueError):
        load_examples(path)
