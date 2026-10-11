# Design: Docker One-Command Demo

## Architecture
1. **Container & Orchestration Pins**:
   - Explicit version pinning for postgres, redis, and python slim images.
   - Redis port bound locally to `127.0.0.1:6379:6379`.
   - `api` healthcheck polls `/demo/` endpoint.
   - Dedicated `owner` service in `docker-compose.yml` configured under the `tools` profile to run migrations, bootstrapping, and management scripts using the administrative DB role.
2. **Key Generation**:
   - `scripts/gen_dev_keys.py`: Standalone script generating RSA-2048 keys in `scratch/` using `cryptography` without Django dependencies.
3. **Bootstrapping**:
   - `demo_bootstrap`: Custom Django command invoking `create_tenant` and `seed_demo` sequentially and outputting the tenant ID, token, and demo URL.
4. **Makefile Workflow**:
   - Single entry point `make demo` runs keygen, starts services waiting for healthy state, and bootstraps data.
