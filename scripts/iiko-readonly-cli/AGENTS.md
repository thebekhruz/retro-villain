# Agent Instructions

- Treat this iiko integration as strictly read-only.
- Use `./iiko.sh doctor` before requesting data.
- Use only `./iiko.sh organizations`, `./iiko.sh nomenclature`, `./iiko.sh yandex-menu`, `./iiko.sh stores`, `./iiko.sh revenue`, and `./iiko.sh dish-sales`.
- Never display, edit, copy, or commit `.env` or `IIKO_API_LOGIN`.
- Do not call iiko endpoints directly and do not add mutation endpoints.
- Prefer JSON output for analysis; preserve `isDeleted` records in Cloud nomenclature audits.
- For the existing restaurant connection, prefer `revenue` and `dish-sales`; they use the same read-only iikoWeb OLAP flow as `retro-protagonist`.
- Run `bash tests/test_iiko.sh` after changing the script.
