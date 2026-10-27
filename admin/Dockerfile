FROM python:3.12-slim

# git to pull the OpenFit repo; the docker CLI and compose plugin to drive
# the user system's compose project through the mounted socket. Exact
# versions, copied from their own images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=docker:27.5.1-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker/compose-bin:v2.32.4 /docker-compose /usr/libexec/docker/cli-plugins/docker-compose

WORKDIR /app

# Log lines reach `docker logs` as they happen - the setup code among them.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# All of admin/, so the Admin can tell when the repo's admin/ has moved on
# from the code it's running (an update pulled, not rebuilt yet).
COPY . .

# Runs as root, for the docker socket; git drops to the repo's owner.
EXPOSE 8090
CMD ["python", "admin_server.py"]
