FROM python:3.9-alpine

RUN apk add --no-cache tini bash # I feel more comfortable with bash.

RUN pip3 install --break-system-packages rethinkdb brozzler doublethink yt-dlp

RUN mkdir /app
COPY cmd.sh /app

ENTRYPOINT ["/sbin/tini", "--"]
CMD /app/cmd.sh

