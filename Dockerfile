FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Through Docker's published ports a caller can arrive from the bridge's own
# private address, which would count as home and skip the password. So in the
# image nobody is home unless TAPE_HOME_NETWORKS says otherwise.
ENV TAPE_HOME_NETWORKS=none
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && useradd --system --uid 10001 --no-create-home tapedeck \
 && mkdir /state && chown tapedeck /state
COPY tape_deck_server.py /app/tape_deck_server.py

USER tapedeck
VOLUME ["/state"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"
ENTRYPOINT ["python", "/app/tape_deck_server.py"]
CMD ["--music", "/music", "--state", "/state"]
