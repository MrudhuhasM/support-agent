"""Optional inference adapters. Importing this module loads no model or SDK."""

import importlib.metadata
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from evaluation.files import canonical
from evaluation.prompts import render_base_prompt


def _failure(error: Exception, seconds: float = 0.0) -> dict:
    return {"text": None, "usage": None, "latency_seconds": seconds,
            "provider_metadata": {}, "error": {"type": type(error).__name__, "message": str(error)}}


def _check_config(config: dict) -> None:
    if config.get("backend") not in {"fake", "huggingface", "chat_api"}:
        raise ValueError("Backend must be fake, huggingface, or chat_api")
    if not isinstance(config.get("model_id"), str) or not config["model_id"].strip():
        raise ValueError("Configure model_id explicitly")
    if config.get("prompt_style", "chat") not in {"chat", "base"}:
        raise ValueError("prompt_style must be chat or base")
    if not isinstance(config.get("parameters", {}), dict):
        raise ValueError("Generation parameters must be a dictionary")
    for key in ("api_key", "token", "authorization", "password"):
        if key in config or key in config.get("parameters", {}):
            raise ValueError("Credentials must be supplied through a configured environment variable")


def create_adapter(config: dict, *, allow_api: bool = False, allow_download: bool = False,
                   http_open=None) -> dict:
    """Return {generate, metadata, requires_api}; generate takes a request batch.

    Requests contain request_id, job, messages, and seed only. Each returned item
    contains raw text, usage, timing, provider metadata, and an optional error.
    The factory and inference requests never accept gold or verifier data.
    """
    _check_config(config)
    backend = config["backend"]
    if backend == "chat_api" and not allow_api:
        raise PermissionError("Paid/API inference requires explicit --allow-api after cost approval")
    metadata = {"backend": backend, "model_id": config["model_id"], "revision": config.get("revision"),
                "parameters": config.get("parameters", {}), "prompt_style": config.get("prompt_style", "chat")}

    if backend == "fake":
        def generate(requests: list[dict]) -> list[dict]:
            responses = []
            for request in requests:
                start = time.perf_counter()
                job = request["job"]
                if job == "triage":
                    value = {"intent": "cancellation", "urgency": "routine", "route": "order_support"}
                elif job == "reply":
                    value = {"response": "This is an offline infrastructure smoke response."}
                else:
                    actual = None
                    for message in reversed(request["messages"]):
                        try:
                            parsed = json.loads(message["content"])
                        except (ValueError, TypeError):
                            continue
                        if isinstance(parsed, dict) and "customer_message" in parsed:
                            actual = parsed
                            break
                    match = re.search(r"order\s+([\w-]+)", actual["customer_message"], re.IGNORECASE) if actual else None
                    observed = any(message["content"].startswith("Tool observation:") for message in request["messages"])
                    if match and not observed:
                        value = {"type": "tool_call", "tool_name": "get_order", "arguments": {"order_id": match.group(1)}}
                    else:
                        value = {"type": "final", "response": "The offline smoke episode is complete."}
                responses.append({"text": canonical(value), "usage": None,
                                  "latency_seconds": time.perf_counter() - start,
                                  "provider_metadata": {"offline_fake": True}, "error": None})
            return responses
        return {"generate": generate, "metadata": metadata, "requires_api": False}

    if backend == "chat_api":
        if config.get("prompt_style", "chat") != "chat":
            raise ValueError("The Chat Completions adapter uses chat-style messages")
        endpoint = config.get("endpoint")
        parsed_url = urllib.parse.urlparse(endpoint or "")
        if parsed_url.scheme not in {"https", "http"} or not parsed_url.netloc or parsed_url.username or parsed_url.password or parsed_url.query:
            raise ValueError("Configure a credential-free full chat-completions endpoint URL")
        if parsed_url.scheme == "http" and parsed_url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote API endpoints must use HTTPS")
        key_env = config.get("api_key_env")
        key = os.environ.get(key_env) if isinstance(key_env, str) else None
        if not key:
            raise ValueError("Set the API credential in the explicitly configured api_key_env")
        parameters = dict(config.get("parameters", {}))
        if set(parameters) & {"model", "messages", "stream", "n", "tools", "tool_choice", "seed"}:
            raise ValueError("Reserved API payload fields cannot be overridden by generation parameters")
        token_cap = parameters.get("max_completion_tokens", parameters.get("max_tokens"))
        if type(token_cap) is not int or token_cap < 1:
            raise ValueError("Configure an explicit positive API output-token cap")
        opener = http_open or urllib.request.urlopen
        metadata.update(endpoint=endpoint, api_key_env=key_env, seed_transmitted=bool(config.get("send_seed", False)))

        def generate(requests: list[dict]) -> list[dict]:
            responses = []
            for request in requests:
                started = time.perf_counter()
                body = {"model": config["model_id"], "messages": request["messages"], **parameters}
                if config.get("send_seed", False):
                    body["seed"] = request["seed"]
                http_request = urllib.request.Request(
                    endpoint, data=canonical(body).encode("utf-8"), method="POST",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                try:
                    with opener(http_request, timeout=config.get("timeout_seconds", 120)) as response:
                        payload = json.loads(response.read())
                        request_id = response.headers.get("x-request-id")
                    choice = payload["choices"][0]
                    responses.append({
                        "text": choice["message"].get("content") or "", "usage": payload.get("usage"),
                        "latency_seconds": time.perf_counter() - started, "error": None,
                        "provider_metadata": {"response_id": payload.get("id"), "request_id": request_id,
                            "latency_scope": "request_round_trip",
                            "returned_model": payload.get("model"), "system_fingerprint": payload.get("system_fingerprint"),
                            "finish_reason": choice.get("finish_reason"), "cost": payload.get("cost"),
                            "raw_response": payload},
                    })
                except urllib.error.HTTPError as error:
                    failure = _failure(RuntimeError(f"HTTP {error.code}; inspect provider logs using the request metadata"), time.perf_counter() - started)
                    failure["provider_metadata"] = {"http_status": error.code,
                        "request_id": error.headers.get("x-request-id") if error.headers else None}
                    responses.append(failure)
                except Exception as error:
                    responses.append(_failure(error, time.perf_counter() - started))
            return responses
        return {"generate": generate, "metadata": metadata, "requires_api": True}

    # Optional dependencies are imported only for an explicitly requested HF run.
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device = config.get("device", "auto")
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise ValueError("CUDA was requested but is unavailable in this PyTorch installation")
    dtype_name = config.get("dtype", "auto")
    if dtype_name == "auto":
        dtype_name = "float16" if device.startswith("cuda") else "float32"
    dtypes = {"float16": torch.float16, "float32": torch.float32, "bfloat16": torch.bfloat16}
    if dtype_name not in dtypes:
        raise ValueError("dtype must be auto, float16, float32, or bfloat16")
    revision = config.get("revision")
    if not isinstance(revision, str) or not revision:
        raise ValueError("Configure a model revision or commit SHA explicitly")
    options = {"revision": revision, "local_files_only": not allow_download, "trust_remote_code": False}
    tokenizer = AutoTokenizer.from_pretrained(config["model_id"], padding_side="left", **options)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        config["model_id"], torch_dtype=dtypes[dtype_name], use_safetensors=True, **options
    ).to(device).eval()
    metadata.update(device=device, dtype=dtype_name,
                    resolved_revision=getattr(model.config, "_commit_hash", None),
                    torch_version=torch.__version__, transformers_version=importlib.metadata.version("transformers"),
                    downloads_allowed=allow_download,
                    cuda_version=getattr(getattr(torch, "version", None), "cuda", None),
                    cuda_device_name=torch.cuda.get_device_name(device) if device.startswith("cuda") else None)
    parameters = {"max_new_tokens": 256, "do_sample": False, **config.get("parameters", {})}
    metadata["effective_parameters"] = parameters
    if set(parameters) & {"input_ids", "attention_mask", "generator", "return_dict_in_generate", "output_scores", "pad_token_id"}:
        raise ValueError("Reserved Hugging Face generation parameters")
    maximum_input = config.get("max_input_tokens", 6144)

    def generate(requests: list[dict]) -> list[dict]:
        responses = []
        # Sampling uses per-request RNG seeds; batch composition cannot change them.
        batches = [[request] for request in requests] if parameters.get("do_sample", False) else [requests]
        for batch in batches:
            if not batch:
                continue
            start = time.perf_counter()
            try:
                texts = [render_base_prompt(request["messages"]) if config.get("prompt_style") == "base" else tokenizer.apply_chat_template(
                    request["messages"], tokenize=False, add_generation_prompt=True,
                    enable_thinking=config.get("enable_thinking", False),
                ) for request in batch]
                inputs = tokenizer(texts, return_tensors="pt", padding=True, truncation=False)
                width = inputs["input_ids"].shape[1]
                if width > maximum_input:
                    raise ValueError(f"Prompt has {width} tokens, exceeding max_input_tokens={maximum_input}; no silent truncation")
                if width + parameters.get("max_new_tokens", 256) > model.config.max_position_embeddings:
                    raise ValueError("Prompt plus generation exceeds model context capacity")
                inputs = inputs.to(device)
                torch.manual_seed(batch[0]["seed"])
                if device.startswith("cuda"):
                    torch.cuda.synchronize()
                start = time.perf_counter()
                with torch.inference_mode():
                    output = model.generate(**inputs, **parameters, pad_token_id=tokenizer.pad_token_id)
                if device.startswith("cuda"):
                    torch.cuda.synchronize()
                elapsed = time.perf_counter() - start
                configured_eos = parameters.get("eos_token_id", getattr(getattr(model, "generation_config", None), "eos_token_id", None))
                if isinstance(configured_eos, int):
                    eos_ids = {configured_eos}
                elif isinstance(configured_eos, list) and all(isinstance(token, int) for token in configured_eos):
                    eos_ids = set(configured_eos)
                else:
                    eos_ids = {tokenizer.eos_token_id}
                for index, tokens in enumerate(output[:, width:]):
                    ids = tokens.tolist()
                    stop = next((position for position, token in enumerate(ids) if token in eos_ids), None)
                    generated_count = stop + 1 if stop is not None else len(ids)
                    responses.append({"text": tokenizer.decode(tokens, skip_special_tokens=True),
                        "usage": {"prompt_tokens": int(inputs["attention_mask"][index].sum()), "completion_tokens": generated_count},
                        "latency_seconds": elapsed, "error": None,
                        "provider_metadata": {"latency_scope": "batch", "batch_size": len(batch),
                            "returned_model": config["model_id"], "resolved_revision": metadata["resolved_revision"],
                            "finish_reason": "stop" if stop is not None else "length"}})
            except Exception as error:
                responses.extend(_failure(error, time.perf_counter() - start) for _ in batch)
                if device.startswith("cuda"):
                    torch.cuda.empty_cache()
        return responses
    return {"generate": generate, "metadata": metadata, "requires_api": False}
