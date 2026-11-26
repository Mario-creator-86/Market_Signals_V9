# Software Supply-Chain Security

The production dependency path is fail-closed:

- Runtime and development dependencies use exact version pins.
- The unused `python-multipart` parser is not installed.
- `pip-audit` checks the fully resolved runtime and development graphs against
  published Python vulnerability advisories.
- Every successful audit emits a CycloneDX JSON runtime SBOM.
- GitHub Actions are pinned to immutable full commit SHAs.
- The Python container base image is pinned to an OCI digest.
- Dependabot proposes weekly Python, Actions, and Docker updates; updates still
  have to pass the complete quality gate.

## Local and CI gate

Install the pinned development dependencies, then run:

```bash
python tools/supply_chain_check.py
```

or:

```bash
make supply-chain
```

The command exits non-zero for an unpinned/direct URL dependency, a known
vulnerability, a failed advisory lookup, or a failed SBOM generation. There is
no vulnerability allowlist. CI uploads these generated artifacts even when a
later test fails:

- `output/supply-chain/dependency-audit.json`
- `output/supply-chain/market-signals-runtime.cdx.json`
- `output/supply-chain/summary.json`

## Updating dependencies

1. Change the exact version in `requirements.txt` or `requirements-dev.txt`.
2. For an indirect vulnerable package, prefer upgrading its direct parent.
   Add an explicit transitive security pin only when the parent exposes a safe
   compatible range.
3. Run `python -m pip install -r requirements.txt -r requirements-dev.txt`.
4. Run `make supply-chain`, the focused tests, and `make release-check`.
5. If the Docker base changes, update both its readable tag and immutable
   digest and let the container runtime gate rebuild it.

Do not suppress a CVE in the scanner. If a finding is demonstrably
non-exploitable, document the evidence and remediation deadline in a reviewed
security change before introducing any exception mechanism.
