FROM give-me-some.very-good-quality-co.de/public/bullseye-client AS bsc

FROM python:3.9-alpine

RUN apk add --no-cache tini

RUN pip3 install --break-system-packages warcprox
RUN pip3 install --break-system-packages --upgrade 'cryptography<=40.0.2'

COPY --from=bsc /bullseye-client /usr/local/bin/bullseye-client
RUN chmod +x /usr/local/bin/bullseye-client

RUN mkdir /app
RUN mkdir /input
COPY cmd.sh /app
COPY app.py /app

ENTRYPOINT ["/sbin/tini", "--", "/app/cmd.sh"]

