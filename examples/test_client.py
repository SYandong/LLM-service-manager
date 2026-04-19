from openai import OpenAI

client = OpenAI(base_url="http://10.86.229.182:8000/v1", api_key="unused")

response = client.chat.completions.create(
    model="google/gemma-4-31B-it",
    messages=[{"role": "user", "content": "Who are you?"}],
)

print(response.choices[0].message.content)
