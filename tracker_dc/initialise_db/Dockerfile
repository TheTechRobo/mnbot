FROM python:3.11-alpine

RUN pip install --break-system-packages rethinkdb
RUN apk add --no-cache git
RUN git clone https://github.com/thetechrobo/rue /rue
RUN pip install --break-system-packages /rue
RUN apk del git

ENTRYPOINT ["python3", "/rue/util/create.py"]
CMD ["chromebot", "3"]
