FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLMS_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --upgrade 'pip>=26.2.1' 'setuptools>=83.0.0' \
    && python -m pip install --no-cache-dir . \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin plms \
    && mkdir /data && chown plms:plms /data

USER plms
VOLUME ["/data"]
EXPOSE 8765
ENTRYPOINT ["plms-mcp"]
CMD ["serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8765"]
