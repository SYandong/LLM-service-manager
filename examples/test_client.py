# Generated-By: Codex / gpt-6-astra
import os


def main():
    base_url = os.environ.get("LEGACY_OPENAI_BASE_URL")
    if not base_url or not base_url.strip():
        raise SystemExit("Set LEGACY_OPENAI_BASE_URL to the administrator-provided legacy OpenAI API base URL")

    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key="unused")
    response = client.chat.completions.create(
        model="Qwen/Qwen3-4B-Instruct-2507",
        messages=[{"role": "user", "content": "Who are you?"}],
    )
    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
