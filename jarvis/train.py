from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def load_examples(path: Path) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            messages = row.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError(f"Line {line_number} does not contain a messages array")
            if not any(message.get("role") == "assistant" for message in messages):
                raise ValueError(f"Line {line_number} has no assistant target")
            examples.append(row)
    if not examples:
        raise ValueError("Training dataset is empty")
    return examples


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def train_lora(
    base_model: str,
    dataset_path: Path,
    output_dir: Path,
    epochs: float,
    learning_rate: float,
    max_length: int,
) -> None:
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig, get_peft_model
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            Trainer,
            TrainingArguments,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Training dependencies are not installed. Run: pip install -e '.[training]'"
        ) from exc

    examples = load_examples(dataset_path)
    tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=False)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    dataset = Dataset.from_list(examples)
    loss_modes: set[str] = set()

    def tokenize(row: dict[str, Any]) -> dict[str, Any]:
        template_kwargs: dict[str, Any] = {
            "conversation": row["messages"],
            "tokenize": True,
            "add_generation_prompt": False,
            "return_dict": True,
            "return_assistant_tokens_mask": True,
            "truncation": True,
            "max_length": max_length,
        }
        if row.get("tools"):
            template_kwargs["tools"] = row["tools"]
        try:
            encoded = tokenizer.apply_chat_template(**template_kwargs)
            mask = encoded.pop("assistant_masks", encoded.pop("assistant_tokens_mask", None))
            input_ids = list(encoded["input_ids"])
            if mask and any(mask):
                labels = [
                    token if keep else -100 for token, keep in zip(input_ids, mask, strict=False)
                ]
                loss_modes.add("assistant_only")
            else:
                labels = input_ids.copy()
                loss_modes.add("full_sequence_fallback")
            return {
                "input_ids": input_ids,
                "attention_mask": list(encoded.get("attention_mask", [1] * len(input_ids))),
                "labels": labels,
            }
        except (TypeError, ValueError):
            render_kwargs: dict[str, Any] = {
                "conversation": row["messages"],
                "tokenize": False,
                "add_generation_prompt": False,
            }
            if row.get("tools"):
                render_kwargs["tools"] = row["tools"]
            text = tokenizer.apply_chat_template(**render_kwargs)
            encoded = tokenizer(text, truncation=True, max_length=max_length, padding=False)
            loss_modes.add("full_sequence_fallback")
            return {
                "input_ids": encoded["input_ids"],
                "attention_mask": encoded["attention_mask"],
                "labels": list(encoded["input_ids"]),
            }

    tokenized = dataset.map(tokenize, batched=False, remove_columns=dataset.column_names)

    def collate(features: list[dict[str, Any]]) -> dict[str, Any]:
        padded = tokenizer.pad(
            [
                {"input_ids": row["input_ids"], "attention_mask": row["attention_mask"]}
                for row in features
            ],
            padding=True,
            return_tensors="pt",
        )
        width = int(padded["input_ids"].shape[1])
        padded_labels = [row["labels"] + [-100] * (width - len(row["labels"])) for row in features]
        padded["labels"] = torch.tensor(padded_labels, dtype=torch.long)
        return padded

    if (
        torch.cuda.is_available()
        or getattr(torch.backends, "mps", None)
        and torch.backends.mps.is_available()
    ):
        dtype = torch.float16
    else:
        dtype = torch.float32
    model = AutoModelForCausalLM.from_pretrained(
        base_model, torch_dtype=dtype, trust_remote_code=False
    )
    model = get_peft_model(
        model,
        LoraConfig(
            r=16,
            lora_alpha=32,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
        ),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    arguments = TrainingArguments(
        output_dir=str(output_dir),
        num_train_epochs=epochs,
        learning_rate=learning_rate,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        logging_steps=5,
        save_strategy="epoch",
        report_to=[],
        remove_unused_columns=False,
    )
    trainer = Trainer(
        model=model,
        args=arguments,
        train_dataset=tokenized,
        data_collator=collate,
    )
    trainer.train()
    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)
    manifest = {
        "schema": "jarvis.model.v1",
        "base_model": base_model,
        "dataset": str(dataset_path.resolve()),
        "dataset_sha256": file_sha256(dataset_path),
        "examples": len(examples),
        "epochs": epochs,
        "learning_rate": learning_rate,
        "max_length": max_length,
        "loss_modes": sorted(loss_modes),
        "tool_aware_examples": sum(bool(row.get("tools")) for row in examples),
    }
    (output_dir / "jarvis-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fine-tune a local model with a Jarvis LoRA adapter"
    )
    parser.add_argument("--base-model", required=True)
    parser.add_argument("--dataset", type=Path, default=Path("exports/jarvis-sft.jsonl"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--max-length", type=int, default=2048)
    args = parser.parse_args()
    train_lora(
        args.base_model,
        args.dataset,
        args.output,
        args.epochs,
        args.learning_rate,
        args.max_length,
    )


if __name__ == "__main__":
    main()
