FROM python:3.9-alpine

RUN apk add --no-cache tini

RUN pip3 install --break-system-packages warcprox
RUN pip3 install --break-system-packages --upgrade 'cryptography<=40.0.2'

RUN mkdir /data
WORKDIR /data

ENTRYPOINT ["/sbin/tini", "--"]
CMD /usr/local/bin/warcprox -z -p 8000 -b 0.0.0.0 \
	--rethinkdb-dedup-url $rethinkdb_dedup_url \
	--rethinkdb-stats-url $rethinkdb_stats_url \
	--rollover-idle-time=1800 --crawl-log-dir crawllogs \
	--prefix "mnbot-no_pipeline-no_item" \
	--quiet

