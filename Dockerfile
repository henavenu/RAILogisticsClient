# VitaLink Distribution — container image for Cloud Run (or any container host).
# The app is a client of the Trust platform: config comes from environment variables
# (RAI_* for the SDK, TRUST_* / PAP_URL / … for the app's direct calls), never from
# files baked into the image.
#
#   docker build -t vitalink .
#   docker run -p 8080:8080 --env-file cloudrun.env vitalink
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

# Dependencies first so code changes don't rebuild this layer. The RAI Trust SDK is
# not on PyPI: it resolves from the wheelhouse shipped in the repo.
COPY requirements.txt ./
COPY sdk-dist/wheelhouse/ ./wheelhouse/
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir --find-links ./wheelhouse rai-sdk-langgraph \
 && python -c "from rai_sdk_langgraph import instrument; print('RAI Trust SDK OK')"

COPY . .

# Local-dev config must not ship: the SDK would read rai.env (localhost URLs) before
# the Cloud Run environment variables. trust_gateway.py is local-only too.
RUN rm -f /app/rai.env /app/rai.toml /app/.env /app/trust_gateway.py \
 && useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8080
CMD ["python", "server.py"]
