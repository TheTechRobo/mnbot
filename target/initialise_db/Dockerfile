FROM python:3.9-alpine

RUN pip install --break-system-packages rethinkdb
RUN apk add --no-cache git
RUN git clone https://github.com/thetechrobo/bullseye /bullseye
RUN apk del git

ENTRYPOINT ["/bullseye/create_db.py"]
