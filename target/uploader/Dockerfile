FROM python:3.9-alpine

RUN apk add --no-cache wget bash curl

WORKDIR /usr/local/bin
RUN pip install --break-system-packages requests
RUN wget https://gitea.arpa.li/JustAnotherArchivist/little-things/raw/branch/master/curl-ia
RUN wget https://gitea.arpa.li/JustAnotherArchivist/little-things/raw/branch/master/ia-upload-stream
RUN wget https://gitea.arpa.li/JustAnotherArchivist/little-things/raw/branch/master/ia-wait-item-tasks
RUN chmod +x curl-ia ia-upload-stream ia-wait-item-tasks

WORKDIR /app
COPY run.sh /app
COPY to-ia.py /app
ENTRYPOINT ["/app/run.sh"]
