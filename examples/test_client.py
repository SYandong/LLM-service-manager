from openai import OpenAI


def main():
    client = OpenAI(base_url="http://10.86.229.182:8000/v1", api_key="unused")
    response = client.chat.completions.create(
        model="Qwen/Qwen3-4B-Instruct-2507",
        messages=[{"role": "user", "content": "Who are you?"}],
    )
    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
