FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    httpx python-dotenv rich invoke \
    "psycopg[binary]>=3.2,<4" "boto3>=1.34,<2" \
    "infra2-sdk @ https://github.com/wangzitian0/infra2-sdk/releases/download/v2.1.1/infra2_sdk-2.1.1-py3-none-any.whl"

COPY platform/30.todo /app/

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["python", "app.py"]
