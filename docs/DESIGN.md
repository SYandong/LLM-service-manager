# vLLM Local Service Manager Design

## Goal

Create a small local tool that starts and manages a vLLM model server for a group sharing one machine. The tool runs directly from the project directory and exposes a machine-local API surface for the group.

## Design Goals

- Small codebase that is easy to read and modify
- Each module has a single, clear responsibility
- Stable local service interface the group can depend on
- Easy to extend with new commands or config fields

## Source Material

The starting point is `utils/setup_vllm.py`.

This file provides the initial launcher logic and model-serving defaults. The new project should extract the useful ideas, simplify them, and reorganize them into a reusable local service manager.

## Users And Access

This service is meant for a group using the same machine. The default binding should therefore use a machine-local address:

- host: `127.0.0.1`
- port: `8000`
- base URL: `http://127.0.0.1:8000/v1`

## System Boundary

The new project should own service management, configuration, and the local serving entrypoint for the group.

This project is responsible for:

- loading config
- constructing the serving command
- starting the serving process
- writing logs
- recording the PID
- checking service readiness
- stopping the managed process
- showing status for local users

The model-serving runtime is responsible for:

- loading the model
- serving the inference API
- performing inference

## Project Structure

The initial project structure should be:

```text
  .
  README.md
  .gitignore
  config/
    server.yaml
  vllm_service/
    __init__.py
    cli.py
    config.py
    readiness.py
    launcher.py
    process.py
  var/
    log/
      vllm.log
    run/
      vllm.pid
```

The `var/` directory holds runtime state and is excluded from version control. It is created on first use.

The modules should have these responsibilities:

- `cli.py`: command-line entrypoint for `start`, `stop`, and `status`
- `config.py`: load and validate server config
- `launcher.py`: translate config into the serving command; owns the log file path
- `process.py`: start, stop, and inspect the managed process; owns the PID file path and all process identity logic
- `readiness.py`: check whether the local service is ready

The tool is invoked as `python -m vllm_service <command>` from the project directory.

## Commands

The first version should expose three core commands:

- `start`
- `stop`
- `status`

Expected behavior:

### `start`

- load config
- check whether the managed process is already running
- report the existing service state when the service is already running
- launch the configured serving process
- redirect stdout and stderr to the log file
- wait for the API to become ready
- print the local base URL on success

### `stop`

- read the PID file
- terminate the managed process
- clean up the PID file after shutdown
- report whether shutdown succeeded

### `status`

- inspect PID file state
- check whether the process is alive
- probe the API
- print model, PID, URL, and log path

Additional commands such as `restart`, `logs`, or profile management fit into the same command structure.

## Config Model

The first version should use one active local config file as the primary service definition.

Suggested config fields:

- `model`
- `host`
- `port`
- `gpu_memory_utilization`
- `max_model_len`
- `enable_reasoning`
- `reasoning_parser`

Suggested default values:

- `model: "Qwen/Qwen3-32B"`
- `host: "127.0.0.1"`
- `port: 8000`
- `gpu_memory_utilization: 0.9`
- `max_model_len: 32768`
- `enable_reasoning: true`
- `reasoning_parser: "deepseek_r1"`

The config is the primary control surface.

## Service Lifecycle

### Start flow

1. Load config from `config/server.yaml`.
2. Ask `process.py` whether the managed process is running.
3. If the process is already running, print current status and exit.
4. Build the serving command from config.
5. Start the serving process with stdout and stderr redirected to the log file.
6. Write the PID and process start time to the PID file.
7. Poll the local API until `/models` returns success.
8. Print the usable base URL.

### Stop flow

1. Ask `process.py` whether the managed process is running.
2. If no live process is found, report that the service is not running and exit.
3. Send a termination signal.
4. Wait for shutdown.
5. Remove the PID file.
6. Report whether shutdown succeeded.

### Status flow

1. Read config.
2. Ask `process.py` whether the managed process is running.
3. Probe the API readiness endpoint if the process is alive.
4. Print current model, PID, base URL, and log path.

## Process Identity

`process.py` is the sole owner of the PID file and all process identity logic. No other module reads or writes the PID file directly.

When starting, `process.py` writes both the PID and the process start time to the PID file. When checking whether the managed process is running, it verifies two conditions: the PID exists, and its start time matches the recorded value. A PID whose start time differs is treated as stale — the PID file is removed and the process is considered not running.

Start time is read from `/proc/<pid>/stat` on Linux.

## Testing Strategy

The first version should test the control plane through fast automated checks and a separate manual smoke test for a real model launch.

Automated tests should cover:

- config loading
- command construction
- PID-file lifecycle
- readiness polling behavior
- CLI behavior for `start`, `stop`, and `status`

The repository should also document a manual smoke test:

- start the service
- confirm `/models` responds
- confirm a client can target `http://127.0.0.1:8000/v1`
- stop the service cleanly

## Planned Features

### Auto-Suspend

When no requests have been received for a configurable idle period, the service suspends vLLM to release the GPU. A lightweight proxy process runs permanently on the service port. When a request arrives and vLLM is suspended, the proxy starts vLLM, waits for readiness, then forwards the request. When vLLM is running, the proxy forwards requests directly.

### Restart Command

A `restart` command that stops and starts the service in one step.

## Follow-On Decisions

The following decisions can follow once the basic version is in place:

- whether multiple named model presets are needed
- whether one machine-local service should support multiple concurrent servers
- whether the group needs machine-local auth or API keys
- whether the service should run under `systemd`
- whether log rotation should be built into the project

