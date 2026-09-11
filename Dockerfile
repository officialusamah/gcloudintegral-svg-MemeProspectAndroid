FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p app data && \
    mv __init__.py accounts.py config.py db.py live.py main.py models.py providers.py scanner.py scoring.py telegram.py ultra_early.py launch_first.py launch_age.py narrative_trend.py google_trend.py pumpportal_launch.py watch_curve_fix.py launcher.py app/
ENV PYTHONPATH=/app
CMD ["python", "-m", "app.launcher"]
