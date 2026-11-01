"""Trusted local inference adapter. No remote model code or pickle weights are loaded."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any


def chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {k: t[k] for k in ("name", "description", "parameters") if k in t},
        }
        if "function" not in t
        else t
        for t in tools
        if t.get("type") == "function"
    ]


def chat_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages = []
    if payload.get("instructions"):
        messages.append({"role": "system", "content": payload["instructions"]})
    items = payload.get("input", [])
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": item["call_id"],
                            "type": "function",
                            "function": {
                                "name": item["name"],
                                "arguments": json.loads(item["arguments"]),
                            },
                        }
                    ],
                }
            )
        elif kind == "function_call_output":
            messages.append(
                {"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]}
            )
        elif item.get("role"):
            content = item.get("content", "")
            if isinstance(content, list):
                if any(c.get("type") not in {"input_text", "output_text", "text"} for c in content):
                    raise ValueError("Local model supports text inputs only")
                content = "".join(c.get("text", "") for c in content)
            messages.append({"role": item["role"], "content": content})
    return messages


class LocalModel:
    def __init__(self, artifact: Path) -> None:
        if not (artifact / "adapter_model.safetensors").is_file():
            raise ValueError("Local adapters require safetensors weights")
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.manifest = json.loads((artifact / "jarvis-manifest.json").read_text())
        self.tokenizer = AutoTokenizer.from_pretrained(
            artifact, local_files_only=True, trust_remote_code=False
        )
        base = Path(self.manifest["base_model"])
        base = artifact / base if not base.is_absolute() else base
        self.model = AutoModelForCausalLM.from_pretrained(
            base,
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
        )
        self.model = PeftModel.from_pretrained(self.model, artifact, local_files_only=True)
        self.model.eval()
        self.torch = torch

    def respond(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("stream"):
            raise ValueError("Local Responses streaming is not supported")
        messages = chat_messages(payload)
        kwargs = {"tools": chat_tools(payload["tools"])} if payload.get("tools") else {}
        inputs = self.tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, return_tensors="pt", **kwargs
        )
        limit = int(getattr(self.model.config, "max_position_embeddings", 2048))
        maximum = min(int(payload.get("max_output_tokens") or 256), 1024, limit - inputs.shape[-1])
        if maximum <= 0:
            raise ValueError("Local model context is full; shorten the conversation")
        with self.torch.inference_mode():
            generated = self.model.generate(
                inputs,
                attention_mask=self.torch.ones_like(inputs),
                do_sample=False,
                max_new_tokens=maximum,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        text = self.tokenizer.decode(
            generated[0, inputs.shape[-1] :], skip_special_tokens=True
        ).strip()
        output = []
        calls = re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.S)
        if "<tool_call>" in text and not calls:
            raise ValueError("Local model produced an incomplete tool call")
        if calls:
            permitted = {
                t.get("name") for t in payload.get("tools", []) if t.get("type") == "function"
            }
            for raw in calls:
                call = json.loads(raw)
                if call.get("name") not in permitted or not isinstance(call.get("arguments"), dict):
                    raise ValueError("Invalid local model tool call")
                output.append(
                    {
                        "id": "fc_" + uuid.uuid4().hex,
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "call_" + uuid.uuid4().hex,
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"]),
                    }
                )
        else:
            output.append(
                {
                    "id": "msg_" + uuid.uuid4().hex,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                }
            )
        return {
            "id": "resp_" + uuid.uuid4().hex,
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": payload["model"],
            "output": output,
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": payload.get("tools", []),
            "usage": {
                "input_tokens": inputs.shape[-1],
                "output_tokens": generated.shape[-1] - inputs.shape[-1],
                "total_tokens": generated.shape[-1],
            },
        }
