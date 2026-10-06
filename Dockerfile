ARG BUILD_FROM
FROM $BUILD_FROM

LABEL maintainer="nupsterd"
LABEL description="NUT UPS status/battery reporter for Home Assistant (Portería Virtual)"

# Zona horaria del container: nombre del archivo diario de auditoría y hora del log.
# device_ts siempre sale en UTC.
ENV TZ=America/Bogota

# python3 trae sqlite3 (depende de sqlite-libs). El cliente NUT es stdlib (socket).
RUN apk add --no-cache \
    python3 \
    py3-requests \
    tzdata && \
    cp /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone

WORKDIR /app
COPY nut_ups/ /app/nut_ups/

CMD ["python3", "-m", "nut_ups.main"]
