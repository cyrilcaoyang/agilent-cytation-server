# Agent documentation

`GET /docs/agent` publishes documentation version **1.0.0**, independently of
the API version and status protocol version. It requires no claim and performs
no hardware I/O. Consult `/status` for live readiness and allowed actions.
Action schemas come from the application's OpenAPI document.

The guide lives in `src/agilent_cytation_server/documentation.py`.
After installing and validating the 4x lens and fluorescence imaging, update
their capability entries independently with confirmed objectives, filter/channel
combinations, remaining limitations and references to validation evidence in
BitacoraDB. Do not put measurement data in this repository or infer optical
validation from camera readiness.

Bump `DOCUMENTATION_VERSION` and `updated_at` when changing the guide: use a
minor version for new capabilities or validation changes (for example 1.1.0),
a patch for corrections, and a major version for breaking structure changes.
Update `tests/test_documentation.py` to match the reviewed capability baseline.
Review and deploy the change before expecting the live endpoint to reflect it.
