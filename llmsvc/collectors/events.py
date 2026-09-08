# Generated-By: Codex / gpt-6-astra
"""llama-swap v252 SSE state/inflight snapshots; log payloads are discarded."""

import json


class EventSnapshot:
    def __init__(self):
        self.states = {}
        self.requests = {}
        self.have_states = False
        self.have_requests = False

    @property
    def complete(self):
        return self.have_states and self.have_requests

    def feed(self, envelope):
        if not isinstance(envelope, dict):
            raise ValueError("invalid SSE envelope")
        kind = envelope.get("type")
        if kind not in ("modelStatus", "inflight"):
            return
        payload = envelope.get("data")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if kind == "modelStatus":
            if not isinstance(payload, list):
                raise ValueError("invalid modelStatus")
            states = {}
            for model in payload:
                if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                    raise ValueError("invalid model status entry")
                if model.get("state") not in ("stopped", "starting", "ready", "stopping"):
                    raise ValueError("unknown model status")
                if model["id"] in states:
                    raise ValueError("duplicate model status")
                states[model["id"]] = model["state"]
            self.states = states
            self.have_states = True
            return
        if not isinstance(payload, dict):
            raise ValueError("invalid inflight event")
        operation = payload.get("operation")
        if operation == "snapshot":
            # v252 omits the empty requests list on the observed live endpoint.
            entries = payload.get("requests", [])
            if not isinstance(entries, list):
                raise ValueError("invalid inflight snapshot")
            requests = {}
            for entry in entries:
                request_id, model = self._request(entry)
                if request_id in requests:
                    raise ValueError("duplicate inflight request")
                requests[request_id] = model
            self.requests = requests
            self.have_requests = True
        elif operation == "upsert" and self.have_requests:
            request_id, model = self._request(payload.get("request"))
            self.requests[request_id] = model
        elif operation == "remove" and self.have_requests:
            request_id = payload.get("id")
            if not isinstance(request_id, str):
                raise ValueError("invalid inflight removal")
            self.requests.pop(request_id, None)
        else:
            raise ValueError("inflight update without valid snapshot")

    @staticmethod
    def _request(entry):
        if not isinstance(entry, dict):
            raise ValueError("invalid inflight request")
        request_id, model = entry.get("id"), entry.get("modelID")
        if not isinstance(request_id, str) or not request_id or not isinstance(model, str) or not model:
            raise ValueError("inflight request identity missing")
        return request_id, model

    def count(self, model):
        if not self.complete or model not in self.states:
            return None
        return sum(value == model for value in self.requests.values())
