# Build from the repository root: docker build -f samples/smoother/Dockerfile .
# This installs the SDK from this checkout; outside it, install
# git+https://github.com/Newboot-io/superset-module-sdk-py@v0.1.0 instead.
FROM --platform=linux/amd64 python:3.12-alpine@sha256:5789aa194f757888f41fa3d48e14b6feaa1b57c0af0b56fcb1372d6474d3bff0 AS wheel
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM --platform=linux/amd64 python:3.12-alpine@sha256:5789aa194f757888f41fa3d48e14b6feaa1b57c0af0b56fcb1372d6474d3bff0
WORKDIR /app
COPY samples/smoother/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=wheel /wheels /wheels
RUN pip install --no-cache-dir --no-deps /wheels/*.whl
COPY samples/smoother/smoothing.py samples/smoother/smoother.py ./
# The superset runs a module as uid 10001 on a read-only file system.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8080
CMD ["python", "smoother.py"]
