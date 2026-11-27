# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
FROM scratch
COPY --chmod=0444 antibody_model_[1234] nanobody_model_[1234] /weights/
COPY --chmod=0444 ATTRIBUTION /ATTRIBUTION
LABEL org.opencontainers.image.title="iki-immunebuilder-weights" \
      org.opencontainers.image.description="Unmodified ImmuneBuilder weights from Zenodo record 7258553" \
      org.opencontainers.image.licenses="CC-BY-4.0" \
      org.opencontainers.image.source="https://github.com/yogesh-dhande/iki"
