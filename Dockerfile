FROM python:3.8

COPY . /app
RUN cd /app && pip install -e .

# To connect to GaussDB with SHA256 authentication, mount the official
# GaussDB psycopg2 driver and set GS_DRIVER_PATH:
#   docker run -e GS_DRIVER_PATH=/driver -v /path/to/gaussdb_conn:/driver ...

CMD gscli
