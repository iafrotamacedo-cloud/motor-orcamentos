FROM python:3.11-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils fonts-dejavu ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/.local/bin:$PATH
WORKDIR /home/user/app
COPY --chown=user . /home/user/app
RUN pip install --no-cache-dir -r requirements.txt
ENV GRADIO_SERVER_NAME=0.0.0.0 PORT=7860
EXPOSE 7860
CMD ["python", "app.py"]
