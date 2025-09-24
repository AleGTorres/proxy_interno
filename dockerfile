FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "proxy_service:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "asyncio"]
