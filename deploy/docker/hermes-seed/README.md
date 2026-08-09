# Hermes offline build seed

Place `hermes-agent.tar.gz` and `hermes-agent.tar.gz.sha256` in this directory
only when a deployment host cannot reliably reach the pinned upstream
repository. The archive must contain an `hermes-agent/` Git checkout at the
revision in `configs/hermes-runtime.lock`. Docker validates the checksum and
revision before installing it. Seed archives are ignored by Git, excluded from
the main context, and mounted only during the build, so they are not stored in
the image.
