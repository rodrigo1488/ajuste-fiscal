FROM python:3.10-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY ajuste_fiscal ./ajuste_fiscal

ENV PYTHONUNBUFFERED=1
EXPOSE 8765

CMD ["python", "app.py"]
