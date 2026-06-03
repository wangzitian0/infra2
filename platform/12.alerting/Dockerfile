FROM python:3.11-slim

WORKDIR /app

COPY platform/12.alerting/app.py /app/app.py
COPY libs /app/libs

ENV PYTHONPATH=/app
EXPOSE 8080

CMD ["python", "app.py"]
