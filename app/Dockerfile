FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py first_login.py ./
COPY plugins ./plugins
COPY migrations ./migrations
COPY templates ./templates

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 80
CMD ["python", "main.py"]
