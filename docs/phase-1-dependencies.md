# Phase 1 dependency inventory

Reviewed October 27, 2026 using official PyPI/upstream metadata and installed distribution metadata. Conditional-platform distributions absent on macOS were checked on PyPI. uv.lock fixes versions and artifact hashes; Python requires 3.12. Container digests are fixed in Dockerfile and compose.yaml. This includes runtime and development dependencies.

Psycopg and psycopg-binary use LGPL-3.0-only; upstream license files remain in installed wheels/container. Project source remains MIT. This inventory is separate from unapproved model/voice artifacts. The stable pydantic/httpx2 package is used by the current Starlette test client, avoiding its deprecated httpx compatibility path.

| Package | Locked version | Published distribution license | Source |
| --- | --- | --- | --- |
| alembic | 1.20.1 | MIT | [PyPI](https://pypi.org/project/alembic/1.20.1/) |
| annotated-doc | 0.0.5 | MIT | [PyPI](https://pypi.org/project/annotated-doc/0.0.5/) |
| annotated-types | 0.8.0 | MIT | [PyPI](https://pypi.org/project/annotated-types/0.8.0/) |
| anyio | 4.15.1 | MIT | [PyPI](https://pypi.org/project/anyio/4.15.1/) |
| argon2-cffi | 25.1.0 | MIT | [PyPI](https://pypi.org/project/argon2-cffi/25.1.0/) |
| argon2-cffi-bindings | 26.1.0 | MIT | [PyPI](https://pypi.org/project/argon2-cffi-bindings/26.1.0/) |
| ast-serialize | 0.13.1 | MIT | [PyPI](https://pypi.org/project/ast-serialize/0.13.1/) |
| attrs | 26.1.0 | MIT | [PyPI](https://pypi.org/project/attrs/26.1.0/) |
| cffi | 2.1.1 | MIT-0 | [PyPI](https://pypi.org/project/cffi/2.1.1/) |
| click | 8.5.0 | BSD-3-Clause | [PyPI](https://pypi.org/project/click/8.5.0/) |
| colorama | 0.4.6 | BSD License | [PyPI](https://pypi.org/project/colorama/0.4.6/) |
| dnspython | 2.9.0 | ISC | [PyPI](https://pypi.org/project/dnspython/2.9.0/) |
| email-validator | 2.3.0 | The Unlicense (Unlicense) | [PyPI](https://pypi.org/project/email-validator/2.3.0/) |
| fastapi | 0.162.0 | MIT | [PyPI](https://pypi.org/project/fastapi/0.162.0/) |
| h11 | 0.16.0 | MIT License | [PyPI](https://pypi.org/project/h11/0.16.0/) |
| httpcore2 | 2.16.0 | BSD-3-Clause | [PyPI](https://pypi.org/project/httpcore2/2.16.0/) |
| httpx2 | 2.16.0 | BSD-3-Clause | [PyPI](https://pypi.org/project/httpx2/2.16.0/) |
| httpx2-jsfetch | 1.0 | BSD-3-Clause | [PyPI](https://pypi.org/project/httpx2-jsfetch/1.0/) |
| idna | 3.20 | BSD-3-Clause | [PyPI](https://pypi.org/project/idna/3.20/) |
| iniconfig | 2.3.1 | MIT | [PyPI](https://pypi.org/project/iniconfig/2.3.1/) |
| jsonschema | 4.26.0 | MIT | [PyPI](https://pypi.org/project/jsonschema/4.26.0/) |
| jsonschema-specifications | 2025.9.1 | MIT | [PyPI](https://pypi.org/project/jsonschema-specifications/2025.9.1/) |
| librt | 0.16.0 | MIT | [PyPI](https://pypi.org/project/librt/0.16.0/) |
| mako | 1.4.3 | MIT | [PyPI](https://pypi.org/project/mako/1.4.3/) |
| markupsafe | 3.0.4 | BSD-3-Clause | [PyPI](https://pypi.org/project/markupsafe/3.0.4/) |
| mypy | 2.4.0 | MIT | [PyPI](https://pypi.org/project/mypy/2.4.0/) |
| mypy-extensions | 1.1.0 | MIT | [PyPI](https://pypi.org/project/mypy-extensions/1.1.0/) |
| opentelemetry-api | 1.45.1 | Apache-2.0 | [PyPI](https://pypi.org/project/opentelemetry-api/1.45.1/) |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | [PyPI](https://pypi.org/project/packaging/26.3/) |
| pathspec | 1.1.1 | Mozilla Public License 2.0 (MPL 2.0) | [PyPI](https://pypi.org/project/pathspec/1.1.1/) |
| phonenumbers | 9.0.42 | Apache-2.0 | [PyPI](https://pypi.org/project/phonenumbers/9.0.42/) |
| pluggy | 1.7.0 | MIT License | [PyPI](https://pypi.org/project/pluggy/1.7.0/) |
| psycopg | 3.3.6 | LGPL-3.0-only | [PyPI](https://pypi.org/project/psycopg/3.3.6/) |
| psycopg-binary | 3.3.6 | LGPL-3.0-only | [PyPI](https://pypi.org/project/psycopg-binary/3.3.6/) |
| pwdlib | 0.3.1 | MIT License | [PyPI](https://pypi.org/project/pwdlib/0.3.1/) |
| pycparser | 3.12 | BSD-3-Clause | [PyPI](https://pypi.org/project/pycparser/3.12/) |
| pydantic | 2.14.1 | MIT | [PyPI](https://pypi.org/project/pydantic/2.14.1/) |
| pydantic-core | 2.50.1 | MIT | [PyPI](https://pypi.org/project/pydantic-core/2.50.1/) |
| pydantic-settings | 2.16.1 | MIT | [PyPI](https://pypi.org/project/pydantic-settings/2.16.1/) |
| pygments | 2.21.0 | BSD-2-Clause | [PyPI](https://pypi.org/project/pygments/2.21.0/) |
| pyjwt | 2.15.1 | MIT | [PyPI](https://pypi.org/project/pyjwt/2.15.1/) |
| pytest | 9.1.1 | MIT | [PyPI](https://pypi.org/project/pytest/9.1.1/) |
| python-dotenv | 1.2.4 | BSD-3-Clause | [PyPI](https://pypi.org/project/python-dotenv/1.2.4/) |
| referencing | 0.37.0 | MIT | [PyPI](https://pypi.org/project/referencing/0.37.0/) |
| rpds-py | 2026.9.1 | MIT | [PyPI](https://pypi.org/project/rpds-py/2026.9.1/) |
| ruff | 0.17.3 | MIT | [PyPI](https://pypi.org/project/ruff/0.17.3/) |
| sqlalchemy | 2.1.6 | MIT | [PyPI](https://pypi.org/project/sqlalchemy/2.1.6/) |
| starlette | 1.10.0 | BSD-3-Clause | [PyPI](https://pypi.org/project/starlette/1.10.0/) |
| truststore | 0.10.5 | MIT | [PyPI](https://pypi.org/project/truststore/0.10.5/) |
| typing-extensions | 4.16.0 | PSF-2.0 | [PyPI](https://pypi.org/project/typing-extensions/4.16.0/) |
| typing-inspection | 0.4.4 | MIT | [PyPI](https://pypi.org/project/typing-inspection/0.4.4/) |
| tzdata | 2026.5 | Apache-2.0 | [PyPI](https://pypi.org/project/tzdata/2026.5/) |
| uvicorn | 0.54.0 | BSD-3-Clause | [PyPI](https://pypi.org/project/uvicorn/0.54.0/) |
