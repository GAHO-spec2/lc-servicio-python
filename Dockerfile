FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY engine.py server.py ./
COPY scripts ./scripts
RUN useradd --create-home lc
USER lc
EXPOSE 8080
CMD ["python", "server.py"]
