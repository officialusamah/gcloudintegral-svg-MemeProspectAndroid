FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p app data && \
    mv __init__.py config.py db.py main.py models.py providers.py scanner.py scoring.py telegram.py app/

ENV PYTHONPATH=/app

CMD ["python", "-m", "app.main"]
