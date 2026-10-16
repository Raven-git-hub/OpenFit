FROM python:3.12-slim

WORKDIR /app

# Scheduler and sync print()s reach `docker logs` as they happen, not when
# a buffer fills.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py crypto.py derived.py metrics.py first_login.py apps.py host.py home_layout.py ./
COPY plugins ./plugins
COPY migrations ./migrations
COPY templates ./templates
COPY audio ./audio

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 80
CMD ["python", "main.py"]
