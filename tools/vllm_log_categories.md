# vLLM Log Categories

vLLM produces 4 categories of log lines, each with its own disable flag.

## 1. Uvicorn Access Logs
HTTP request lines emitted by the uvicorn web server.

Example:
```
(APIServer pid=572368) INFO:     127.0.0.1:40668 - "POST /v1/chat/completions HTTP/1.1" 200 OK
```

Disable with: `--disable-uvicorn-access-log`

## 2. Request Logs
Per-request details logged by vLLM at INFO level (request ID, parameters, LoRA request) and DEBUG level (prompt inputs).

Disable with: `--disable-log-requests`

## 3. Stats Logs
Periodic engine metrics emitted every 10 seconds by `LoggingStatLogger`.

Fields always present:
- Avg prompt throughput (tokens/s)
- Avg generation throughput (tokens/s)
- Running requests
- Waiting requests
- GPU KV cache usage (%)
- Prefix cache hit rate (%)

Fields present when relevant:
- Deferred requests
- Preemptions
- Corrupted requests
- External prefix cache hit rate
- Multi-modal cache hit rate

Example:
```
(APIServer pid=572368) INFO 04-18 11:14:12 [loggers.py:259] Engine 000: Avg prompt throughput: 1.9 tokens/s, Avg generation throughput: 0.2 tokens/s, Running: 0 reqs, Waiting: 0 reqs, GPU KV cache usage: 0.0%, Prefix cache hit rate: 0.0%
```

Disable with: `--disable-log-stats`

## 4. General vLLM Logs
Everything else: startup, model loading, GPU memory allocation, CUDA graph capture, warnings, errors.

Disable with: `VLLM_CONFIGURE_LOGGING=0`

## Sources
- [vllm serve CLI docs](https://docs.vllm.ai/en/stable/cli/serve/)
- [Disable detailed request logging](https://github.com/vllm-project/vllm/issues/1240)
- [How to disable HTTP request logs](https://github.com/vllm-project/vllm/issues/14736)
