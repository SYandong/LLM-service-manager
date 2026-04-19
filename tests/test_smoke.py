import pytest
from openai import OpenAI
from vllm_service.readiness import is_ready

BASE_URL = "http://127.0.0.1:8000/v1"


@pytest.fixture(scope="module")
def client():
    if not is_ready("127.0.0.1", 8000):
        pytest.skip("Service is not running")
    return OpenAI(base_url=BASE_URL, api_key="unused")


def test_models_endpoint(client):
    models = client.models.list()
    assert len(models.data) > 0


def test_chat_completion(client):
    response = client.chat.completions.create(
        model=client.models.list().data[0].id,
        messages=[{"role": "user", "content": "Reply with one word: hello"}],
        max_tokens=16,
    )
    assert response.choices[0].message.content.strip()
